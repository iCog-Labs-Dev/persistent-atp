# MORK

MORK is the rebuildable proof-graph projection and query layer. The commit gate
and SQLite journal remain the transactional authority; the formal adapter and
independent replay remain the proof authority.

## Native setup and tests

```bash
scripts/with-mork.sh --install -- .venv/bin/pytest -q mork/tests \
  src/commit_gate/tests/test_gate_projection.py
```

The installer builds the pinned fork's in-tree `ffi/` crate with
`mork/fork-patches/ffi-query-lifetime.patch`. It requires Git, Cargo, the pinned
nightly Rust compiler and network access for a fresh clone/dependencies.
`MORK_FORK_ROOT` selects an existing clean checkout at the pinned commit;
`MORK_LIBRARY` selects a previously built, provenance-compatible library.

The wrapper establishes `LD_PRELOAD` and `MORK_LIBRARY`, verifies source, patch
and compiler identity against `FFI_PROVENANCE.json`, and emits the loaded
library's path and SHA256. It never updates the committed record during a
normal run. After reviewing an intentional build change, use
`--record-provenance` explicitly. Set `MORK_PYTHON` when the probe should use a
Python other than `.venv/bin/python` (or the system fallback).

For the same no-skip gate as native CI:

```bash
MORK_REQUIRE_NATIVE=1 MORK_TEST_FORK=1 scripts/with-mork.sh --install -- \
  .venv/bin/pytest -q mork/tests src/commit_gate/tests/test_gate_projection.py
```

Ordinary `pytest` can run without the native dependency and reports those cases
as skipped. `MORK_REQUIRE_NATIVE=1` makes any skip in the selected suite fail.

## Projection and failure recovery

`MorkView.apply_journal_event(store, proof, revision)` reads the exact event
from the journal, applies it under the process-wide lock, then publishes
`(projected <proof> <revision> <schema_version>)`. Complete compound queries use
the same lock. Failed projection invalidates reads across all wrappers until
`CommitGate.catch_up()` repairs it; it never rolls back the committed journal.
A native panic or poisoned Space requires host restart and full replay.

A second `MorkSpace` object shares the same native Space. For independent
validation or a clean test Space, start a new process.

## Snapshot bundles

```python
from mork.snapshot import create_backup, restore_backup

# space, view and store belong to the same host/journal.
gate.catch_up()
create_backup(space, view, store, "snapshots/checkpoint-001")

# Startup/recovery, before declaring the host ready:
report = restore_backup(space, view, store, "snapshots/checkpoint-001")
gate.catch_up()  # every proof, including proofs created after this snapshot
ready = all(view.projected_revision(p) == store.head(p)[0]
            for p in store.proof_ids())
```

A bundle contains the native tree and symbol mapping, schema/features,
per-proof journal-prefix hashes and watermarks, full file checksums and an atom
digest. Creation requires caught-up, valid projections and publishes a new
directory. Restore stages and validates a fresh child Space before replacing
the live one. Corrupt bundles, rewound journals and unrelated journal histories
are rejected. Lagging restored proofs remain unreadable until catch-up.

## Queries, derived state and context

`nodes`, `edges`, point and adjacency reads implement the shared `ReadView`.
The fifteen spec operations live in `backend/queries.py`, with additional
reverse-dependency, structural stale-impact and scheduler-discovery queries.
Discovery never changes authoritative status. `context_compiler.sources` uses
the same read contract and is exercised against a revisioned MORK snapshot.

Dependency closure uses a bounded Python index maintained from canonical graph
mutations. Relevant fields/parallel edges are tracked exactly; writes invalidate
results for the affected proof across wrappers, and restore discards derived
state. Unknown data is hydrated from native atoms. Up to 16 proof indexes and
16 results are retained, with eviction requiring rehydration. Neither cache is
persisted or authoritative. E8 reports 20 cold queries after structural writes
separately from cached queries; both cold and warm targets pass in the recorded
workload.

Witness queries consume the certificate's seven-field adapter record and check
selected tactics, membership, indexed children and accepted zero-goal leaves.
Shared legacy selection flags do not create current witnesses. Fake and injected
real adapters emit the same contract. `mathproof.projection.result_operations`
prepares a new run's gate-owned ingestion; it does not implement a resume state
machine or dispatch work.

New exact-state hashes bind canonical serialization version, goal, ordered local
context and environment. `get_exact_state` validates those fields before reuse;
legacy/mismatched states remain readable through `node()` without silent
migration. Identity fields and certificate witnesses are immutable.

`rules/*.metta` are reference specifications. The project uses Python as the
live query engine and defers the `.mm2` modules. Further MORK kernel/FFI/fork
improvements are outside the current project scope.

## Layout

- `backend/`: FFI bridge, graph view and queries.
- `atoms.py`: canonical atom builders and long-value codec.
- `snapshot.py`, `supervisor.py`: snapshot validation and host restart support.
- `projector/`: batch journal projection; comparison excludes operational watermarks explicitly.
- `bench/`: opt-in benchmarks and reproducible result files.
- `tests/`: native projection, acceptance, persistence and recovery tests.

## Executable PoC recovery host

```bash
scripts/with-mork.sh -- .venv/bin/python -m mork.host \
  --journal proof.sqlite --ready host-ready.json --supervise
```

The child verifies the journal, optionally restores `--snapshot <bundle>`, replays
all proofs and publishes readiness. It follows newly committed events and new
proofs. An unusable native Space withdraws readiness and exits so supervision
can restart it. SIGTERM stops both processes cleanly. The host does not submit
worker proposals, execute ATP or provide a new scheduler/service API.

To check startup and create a new snapshot, use `--once --snapshot-out <new-dir>`
without `--supervise`. Once-mode removes its readiness file on exit. Invalid
snapshots fail closed; omit a snapshot to rebuild from the journal.

`mork/FFI_BUILD_CONFIG.json` records default native features, wasm exclusion,
release profile and flags. This is evidence from pinned installer inputs/source
manifests; the fixed library does not export flags/features itself. No native
source or binary change is needed for the current PoC features.
