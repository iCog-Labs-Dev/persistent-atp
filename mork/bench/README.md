# MORK backend benchmark

What the live MORK space costs, measured rather than guessed.

The question that prompted this: MORK keeps **one space per process**, behind a
Rust `OnceLock`, holding every proof at once, with no command to empty it. If a
`match` costs something proportional to *everything in the space* rather than to
the subset it matches, then unrelated proofs slow each other down and a
long-lived gate degrades as it runs.

The short answer is in [E1](#e1--do-unrelated-proofs-slow-this-one-down): **they
don't.** Growing the space 500× with atoms belonging to other proofs moved this
proof's `node()` from 250.0 µs to 249.6 µs. The concern was unfounded — but the
same sweep run *inside* one proof found a real problem the concern hadn't named
([E2](#e2--what-does-one-proof-cost-as-it-grows)).

---

## Running it

```bash
scripts/with-mork.sh .venv/bin/python -m mork.bench --quick          # all seven, small sizes, ~3 s
scripts/with-mork.sh .venv/bin/python -m mork.bench                  # the full sweep, ~30 s
scripts/with-mork.sh .venv/bin/python -m mork.bench --experiment E1  # just one
scripts/with-mork.sh .venv/bin/python -m mork.bench --json out.json  # rows as JSON too
```

The wrapper is not optional. `libmork_ffi.so` must be in `LD_PRELOAD` before the
process starts — the dynamic loader reads that variable before Python exists, so
no amount of `ctypes` work inside the program can substitute for it. Without the
wrapper the load fails with *cannot allocate memory in static TLS block*, and the
run exits 69 (`EX_UNAVAILABLE`) so a CI step can tell "no library" from "the
benchmark broke."

This package is deliberately absent from `testpaths` in `pyproject.toml`. Thirty
seconds is not much, but a benchmark that runs on every `pytest` invocation stops
being a benchmark and becomes a flaky test.

---

## Code analysis

Five files, ~800 lines. Each does one thing.

| file | responsibility |
|---|---|
| `workload.py` | Seeded synthetic proof graphs and gate-legal proposals |
| `harness.py` | Timing loop, median/p95, RSS, the `Row` record, table and JSON output |
| `experiments.py` | E1–E7, one function each, plus the `EXPERIMENTS` registry |
| `__main__.py` | CLI; re-executes itself once per experiment |
| `__init__.py` | Docstring and the run instruction |

### The constraint that shapes everything: one experiment per process

MORK's space is process-wide and **cannot be emptied**. There is no `clear`, no
second space, no reset. So an experiment that ran second would be measuring
whatever the first one left behind — a benchmark that silently reports the wrong
numbers, which is worse than one that fails.

`__main__.py` handles this by re-executing itself:

```python
command = [sys.executable, "-m", "mork.bench", "--in-process", name, "--json", "-"]
```

The parent is launched once under `scripts/with-mork.sh`; children inherit
`LD_PRELOAD` and `MORK_LIBRARY` through the environment and need no re-wrapping.
They must be *processes*, not threads — the `OnceLock` is per-process, and
threads would share the same contaminated space.

The isolation is verifiable rather than assumed: every row carries the atom count
the space held when the timing was taken. A child whose first row shows a
non-zero count where it expects an empty space has been contaminated, and its
numbers should be thrown away.

Two rows are that check passing. E4's first row reads `atoms: 0` — a space nobody
has written to. And E1 run alone reports exactly the counts it reports in
sequence, 249 through 124,249, so its child inherited nothing from the six
experiments that would otherwise have run before it.

### Why every row has both a `scale` and an `atoms` column

`scale` is the independent variable in the experiment's own units: nodes for
E1–E4 and E7, proposals for E5, journal events for E6. `atoms` is what the space
actually held when the timing was taken.

They are separate columns because they are not the same number and only the
second one explains a timing. A node costs four atoms plus, for every node but
the first, one edge back into the graph — so 25,000 background nodes is 124,000
atoms, not 100,000. MORK also deduplicates, so re-adding an atom grows the space
by nothing. Reporting one derived figure would have hidden both facts.

That is not hypothetical. An earlier version of E1 restarted its background proof
names (`bg0`, `bg1`, …) each round, so later rounds re-added atoms the space
already held and it grew by less than the round claimed. `background_atoms` now
takes a `first` index and continues the numbering:

```python
for index in range(first, first + proofs):
    yield from proof_atoms(f"bg{index}", nodes_each, rng)
```

### Why `unit` is a field and not a header

Most rows are microseconds per call, but E6 reports milliseconds per event and E7
reports MiB. A column header saying `us` would have made two experiments' rows
wrong in the JSON, where nobody reads the note. `Row.unit` carries it, so the
data file is self-describing.

One-shot measurements — a replay, an RSS reading — put the same value in `median`
and `p95` and set `iterations` to 1. There is no distribution to report, and
inventing one would be worse than repeating the number.

### Three method decisions

**Median and p95, never a mean.** A mean over FFI calls is dominated by whichever
iteration happened to hit an allocation. The median says what a typical call
costs; p95 says what the tail looks like. Both are reported, because a backend
can be fast and still have a bad tail.

**Warm-up discarded.** The first call into a fresh space pays lazy initialisation
that no later call pays. Averaging it in measures startup, not steady state.

**The atom count travels with every timing.** Given a space that cannot be
emptied, a duration is meaningless without knowing what was in the space when it
was taken.

### How the load is built

`workload.py` writes s-expressions **straight into the space**, bypassing the
gate and the view:

```python
yield f"(node {p} {local} {encode(_LABEL)})"
yield f"(field {p} {local} {encode('status')} {encode('open')})"
```

Filling a space through `MorkView` would cost several FFI crossings per node — at
E3's measured 371 µs per `add_node()`, E1's 25,000 background nodes alone would
take about nine seconds. Batched raw `add` costs one crossing per 2,000 atoms.
The shapes match `MorkView`'s exactly — ids local, every slot JSON-encoded — so
what is measured afterwards runs against atoms indistinguishable from committed
ones.

`proof_atoms` takes a `start`, so a sweep over 100 / 500 / 2,500 nodes adds only
the new ones each round instead of rebuilding the proof.

The gate-legal half (`proposals`) had to be discovered empirically, because
`validate_proposal` rejects more than it looks like it will. The rules baked into
the generator, each one learned from a rejection:

| rule | what happens otherwise |
|---|---|
| every proposal carries `base_revision` | `MISSING_CONCURRENCY_TOKEN` |
| status writes carry `lease_id` + `fencing_token` | `missing-concurrency-token` |
| `FormalState.status` goes `open → expanded` | `unknown-status-value`, `illegal-status-transition` |
| annotation fields are the ones in `ANNOTATION_FIELDS` (`depth`, not `heuristic_score`) | `missing-prior-value` |
| `UpsertNode` argument order is `(label, node_id, fields)` | `NAMESPACE_MISMATCH` |

### Why E3 measures `set_field` twice

Writing a *new* key is one match that finds nothing, then one add. *Overwriting*
is the ask-erase-write path in `MorkView._replace`
([view.py:272](../backend/view.py#L272)): match, then one remove per hit, then
add. The extra work exists because MORK's `remove-atoms` reports success whether
or not anything matched, so the code has to read the exact bytes back before it
can safely remove them. Overwrite is the hot path for status updates, and it was
the most likely place for a surprise. It turned out to cost 23 µs — but that is a
result, not an assumption.

---

## Results

Intel i7-8665U @ 1.90 GHz, 8 CPUs, Linux 6.12, Python 3.13.5. One full sweep,
61 rows. **Absolute microseconds will differ on your machine; the shapes are the
finding.** All timings are medians in µs unless the table says otherwise.

### E1 — do unrelated proofs slow this one down?

The target proof is built once at 50 nodes and never touched again. Background
proofs are piled in around it, and the same four queries re-run after each round.
Only the background changes, so any movement is the background's doing.

| background proofs | atoms in space | `node()` | `edges_from()` | `edges_to()` | `projected_revision()` |
|---:|---:|---:|---:|---:|---:|
| 0 | 249 | 250.0 | 247.5 | 129.3 | 106.8 |
| 20 | 2,729 | 252.4 | 254.8 | 130.1 | 106.2 |
| 100 | 12,649 | 249.1 | 251.9 | 131.9 | 106.7 |
| 400 | 49,849 | 249.5 | 247.1 | 129.2 | 106.3 |
| 1,000 | 124,249 | 249.6 | 246.8 | 129.2 | 106.2 |

**Flat — not approximately, exactly.** A 500× increase in unrelated atoms moved
`node()` by 0.4 µs and `edges_to()` by 0.1 µs. Both are well inside the noise of
the column's own p95. MORK discriminates on the leading slots, so a query scoped
to one proof does not pay for the 999 proofs beside it.

**What this decides.** Ben's technical design calls for one process per active
theorem, and *"at scale, shard by theorem."* On this evidence that is an
**optimisation, not a requirement** — proofs may share a process safely, and the
shared space in our own test suite (~50 proofs at once) is not contaminating its
results.

### E2 — what does one proof cost as it grows?

Same four probes, but now the growth is *inside* the proof being queried.

| nodes in proof | atoms | `node()` | `edges_from()` | `edges_to()` | `projected_revision()` |
|---:|---:|---:|---:|---:|---:|
| 100 | 499 | 246.6 | 253.6 | 271.8 | 104.6 |
| 500 | 2,499 | 245.8 | 329.8 | 316.0 | 104.8 |
| 2,500 | 12,499 | 246.5 | 692.0 | 1,262.3 | 104.5 |
| 10,000 | 49,999 | 246.4 | 2,207.2 | 4,803.7 | 104.6 |

**Node lookups are flat; edge traversals are linear.** `node()` and
`projected_revision()` do not move across a 100× growth. `edges_from()` grows
8.7×, `edges_to()` 17.7×.

Read against E1 the diagnosis is unambiguous: MORK *can* discriminate — E1 proves
it does, at 500× the scale. So the growth here is not MORK's indexing, it is the
query.

**Cause, in our code.** [`MorkView._edges`](../backend/view.py#L283) leaves the
edge id unbound:

```python
rows = self._space.match(
    f"(edge {encode(proof_id)} $eid {encode(rel_type)} {src} {dst})",
    f"($eid {src} {dst})",
)
```

`$eid` sits in the second slot with nothing to constrain it. The proof id in slot
one is the only discriminator, so the pattern matches every edge in the proof and
filters afterwards. `edges_to()` is worse than `edges_from()` because the node id
lands in the last slot instead of the second-to-last, leaving more of the pattern
free.

**Why it matters.** This is on the commit path — validators traverse edges. A
10,000-node proof pays ~5 ms per `edges_to()` call. An
`(edge-from <proof> <src> <eid> …)` companion atom would put the source in a
leading slot and turn the scan into a lookup. Worth an issue.

### E3 — which operation dominates?

One proof, 2,000 nodes, 9,999 atoms.

| operation | median | p95 | what it does |
|---|---:|---:|---|
| `edges_from()` | 600.5 | 617.1 | scans the proof's edges — see E2 |
| `add_node()` | 371.1 | 382.4 | replaces before it writes: 3 matches + 2 adds |
| `record_projected()` | 260.3 | 275.6 | match, remove, add |
| `node()` | 255.3 | 260.9 | one match for the label, one for the fields |
| `set_field()` overwrite | 152.7 | 167.3 | ask-erase-write |
| `add_edge()` | 129.9 | 147.3 | one match, one add |
| `set_field()` new key | 129.4 | 133.8 | match finds nothing, then add |
| `projected_revision()` | 106.6 | 109.7 | one match |

**Everything is a few hundred microseconds, except edge traversal**, and every
row is explained by counting crossings against E4's floor of 103 µs per read and
10.5 µs per add:

- `projected_revision()` — one read. 103 predicted, 106.6 measured.
- `set_field()` new key — one read, one add. 113 predicted, 129.4 measured.
- `node()` — two reads. 206 predicted, 255.3 measured.
- `add_node()` — three reads, two adds. 330 predicted, 371.1 measured.

Nothing is mysteriously slow. The ask-erase-write penalty is 23 µs, so the safety
it buys is nearly free, and `add_node()` is the most expensive write only because
it clears the old label and any dropped fields before writing the new ones.

### E4 — what does one FFI crossing cost?

A match that cannot succeed, against spaces of increasing size. This is the floor
every read pays, which is what makes E3's numbers attributable.

| atoms in space | `match`, no results | `add`, one atom |
|---:|---:|---:|
| 0 | 101.7 | 10.9 |
| 2,500 | 102.6 | 10.4 |
| 12,500 | 102.8 | 10.3 |
| 50,000 | 102.9 | 10.5 |

**Reads cost ~103 µs before they do any work; adds cost ~10.5 µs.** Reads are
about 10× adds, and the read floor is flat in space size — 1.2 µs across a
50,000-atom range, which is E1's finding again from below.

The consequence for the backend: **cost is dominated by how many times you cross
the boundary, not by how much data is on the other side.** Batching reads would
buy more than optimising any single one.

### E5 — what does MORK add to a commit?

The same 300 proposals committed twice, through backends that differ only in
where state lands. Journal, validators and ops are identical.

| backend | median | p95 |
|---|---:|---:|
| `MemoryView` (dicts) | 156.5 | 171.5 |
| `MorkView` (atoms) | 3,313.9 | 3,836.8 |

**21×, or about 3.3 ms per commit.** All 300 were accepted in both runs, so this
is like-for-like and not a difference in what got rejected. At ~103 µs per read
that is roughly 30 crossings per commit, which is what a validator doing several
`node()` and `edges_*()` lookups costs.

Whether 3.3 ms is acceptable is not something this benchmark can say — **no
latency budget has been written down anywhere**. The only named metric in Ben's
technical design is *"taint propagation latency"*, with no figure attached. This
establishes a baseline; it does not pass or fail against a spec.

### E6 — how long is crash recovery?

The journal is filled through a gate that does not project, so the graph starts
genuinely empty — the state a process finds itself in after a crash. Then
`catch_up` replays it.

| events | total | per event |
|---:|---:|---:|
| 100 | 170 ms | 1.7 ms |
| 1,000 | 1,703 ms | 1.7 ms |
| 5,000 | 8,579 ms | 1.7 ms |

**Flat per event, linear in total.** Replay does not slow down as it goes, which
is the property that matters: recovery time is predictable from journal length,
and a proof that has been worked on for months does not become unrecoverable.

The absolute figure is the cost of holding no durable graph state. 1.7 ms per
event means a 5,000-event proof takes 8.6 seconds to come back — a startup cost,
paid once per process, not per commit.

### E7 — does a long-lived gate grow without bound?

| atoms | peak RSS | per atom |
|---:|---:|---:|
| 4,999 | 36.8 MiB | 7.54 KiB |
| 24,999 | 43.3 MiB | 1.77 KiB |
| 99,999 | 74.5 MiB | 0.76 KiB |
| 0 — after removing all 99,999 | 77.2 MiB | — |

**No leak.** Per-atom cost *falls* as the space grows: the 36 MiB floor is the
interpreter and the library, and 100,000 atoms of real data fit in the 38 MiB
above it. Removing all 99,999 atoms took 1,263 ms and left the space genuinely
empty (`0 left`), with peak RSS moving only 74.5 → 77.2 MiB.

`ru_maxrss` is a high-water mark and never falls, so the second measurement
cannot show memory returning to the OS. What it *can* show is whether removal
forced further growth, and 2.7 MiB says it did not. A gate that writes and
retracts over a long life does not grow by construction.

---

## Verdicts

| | question | answer |
|---|---|---|
| **E1** | Do unrelated proofs slow this one down? | **No.** Flat across 500× growth, to 124k atoms. Sharding by theorem is an optimisation. |
| **E2** | What does one proof cost as it grows? | Node reads flat; **edge traversal linear**, up to 17.7×. The cause is our query, not MORK. |
| **E3** | Which operation dominates? | Edge traversal. Everything else is 100–400 µs and equals its crossing count. |
| **E4** | What does one crossing cost? | ~103 µs read, ~10.5 µs add, flat in space size. Crossings are the cost, not data volume. |
| **E5** | What does MORK add to a commit? | 21× over in-memory, ~3.3 ms. No budget exists to judge it against. |
| **E6** | How long is recovery? | 1.7 ms per event, flat. 5,000 events ≈ 8.6 s, paid once per process. |
| **E7** | Does it leak? | No. 100k atoms in 38 MiB above a 36 MiB floor; removal reclaims. |

**One action item.** E2's edge traversal is the only measured defect, it is on the
commit path, and the fix is ours to make.

## Caveats

- **One machine, one run.** An earlier sweep on this same machine under different
  load produced numbers roughly 2× higher across every experiment, with
  identical shapes and identical conclusions. Trust the ratios, not the µs.
- **No `.mm2` query benchmark.** All six `.mm2` files in `mork/`, plus
  `proof_atoms.metta` and `unified_proof_atoms.metta`, are one-line comment
  stubs. There are no queries to time, and so no taint-propagation measurement.
- **E5 and E6 use `JournalStore(":memory:")`.** A disk-backed journal adds fsync
  cost to both, equally, on both backends — so E5's ratio holds but its absolute
  figure is a floor.
- **E3's `add_node()` grows the space while being measured**, by 100 atoms over
  50 iterations against a 9,999-atom space. E4 shows that does not change the
  cost of a read.
