# Coordinator

This directory contains the proof-cycle coordinator: the outer loop that runs rounds of dispatch cycles over one or more proofs, tracks their progress from committed state, and applies the stagnation policy of the architecture (Sections 10.2–10.5, 12.1). It is a LangGraph prototype of the coordinator role, built on the scheduler, dispatcher and commit gate; it replaces none of them.

## What a round does

```
plan_round --Send per active proof--> work --> assess --> plan_round | END
```

1. **`plan_round`** opens the round and fans the active proofs out with LangGraph's `Send`.
2. **`work`** runs one proof's portfolio (Section 8.6): one `run_cycle` per slot that has work. The default `PORTFOLIO` has formal search, research and critic slots. `AGENT_PORTFOLIO` also routes critic-accepted claims to the formalizer, drafts to the alignment reviewer, and statement checks to the experiment adapter. Pass it as `Coordinator(workspaces, slots=AGENT_PORTFOLIO)` when those workers and adapters are configured. Proofs run in parallel; the slots of one proof run in turn, because a proposal must be based on the head it commits onto.
3. **`assess`** joins the round. For each proof it re-reads committed state, updates the stall count, raises the escalations the stagnation policy calls for, commits them, and records milestone messages (Section 10.7).
4. The run ends when every proof is solved, when no proof can move without a replay or a human (`halted`), or when the round budget runs out.

## The stagnation policy

`policy.classify` applies the six rules of Section 10.4 in order:

| Rule | Read from committed state | Escalation |
|---|---|---|
| 1 | a critic refuted a claim that is still provisional | `repair`, at the critic's `earliest_gap` |
| 2 | `non_progress_rounds` rounds without verified gain | `five-mechanisms` (Section 10.5) |
| 3 | one obstruction kind repeated on a declaration's runs | `diversify-formal` |
| 4 | a stagnated run missing a lemma, or several runs at one residual | `bridge-lemma` (or a representation change) |
| 5 | a reviewed alignment or an obstruction doubting the statement | `alignment-review`, which holds rules 3 and 4 for that declaration |
| 6 | an empty frontier, or `global_stall_rounds` rounds stalled | `global-audit` |

Progress means verified gain only: a settled claim, a closed formal state or a replayed declaration. A certificate waiting for replay pauses the stall rules instead of counting as stagnation (Section 10.3). Thresholds are `StagnationPolicy` configuration.

## Authority

The coordinator decides *that* something must happen, never the mathematics of it. Each escalation becomes a queued `ResearchMove` under the coordinator's own `ResearchState`, committed through the gate under a proof-wide lease; a worker takes the move in a later round like any other. The move targets the escalation's claim or the proof's unique `HAS_TARGET` claim, which the research context compiler requires. Without a unique target, escalation creation fails before committing an unworkable move. The gate trusts the `coordinator` worker class with any label, so the limit is kept in `escalate.py` and checked by the tests: the coordinator writes no claim status, formal state, certificate or obstruction (Section 3.2, invariant 7).

`mechanisms.py` fixes what a worker's five-mechanism answer must state and checks it is complete and diverse (one technique tag per mechanism) before it becomes queued moves.

## The journal is the authority

The graph state is a cache (Section 2.3, invariant 6). It holds round counters, stall counts, reports and messages — never proof state. Every decision is re-read from the journal, and an escalation already on record is not raised again (once per target for the local rules, once per progress level for the stall rules), so a coordinator restarted on the same journal with an empty view makes the same decisions. `ProofWorkspace` accepts either `MemoryView` or `MorkView`; for the latter, projection uses the gate's `catch_up()`. A LangGraph checkpointer may be passed to resume a run; losing its checkpoints loses no proof work.

## File Summary

- **`workspace.py`**: `ProofWorkspace` (a proof's store factory, view, dispatcher and adapters) and `project`, which catches the view up to the journal head. Each caller opens its own store connection.
- **`progress.py`**: Reads a proof's position from committed state: progress counts, residuals, misaligned declarations, refuted reviews and escalations on record.
- **`policy.py`**: The stagnation policy as an ordered rule table.
- **`escalate.py`**: Turns escalations into the coordinator's one kind of proposal.
- **`portfolio.py`**: The exploit/explore/critic slots and `SlotScheduler`, which pins a worker class to its frontier category.
- **`rounds.py`**: One round for one proof — `work` and `assess` — without LangGraph.
- **`mechanisms.py`**: The five-mechanism answer contract.
- **`graph.py`**: The LangGraph `StateGraph` and `Coordinator`.

## Running

The core needs only the commit gate and the scheduler. The graph and CLI run command need the optional extra:

```
uv sync --extra coordinator
.venv/bin/python -m coordinator init --journal ./proof.sqlite --proof-id p1 \
  --statement 'For all n : Nat, n + 0 = n' \
  --first-task 'Find a testable induction step'
.venv/bin/python -m coordinator status --journal ./proof.sqlite --proof-id p1
.venv/bin/python -m coordinator run --journal ./proof.sqlite --proof-id p1 --rounds 3
```

The run command reads `GEMINI_API_KEY` from the environment or `.env`. It runs the real research and critic workers. Pass `--lean-project /path/to/lean/project` to also run the formalizer, Lean statement checker and LLM alignment reviewer. To use live MORK projection, run the command through `scripts/with-mork.sh` and add `--backend mork`:

```
scripts/with-mork.sh .venv/bin/python -m coordinator run \
  --journal ./proof.sqlite --proof-id p1 --backend mork --rounds 3
```

`init` accepts repeatable `--assumption` arguments and commits the literal theorem, assumptions and first research task through the gate. `status` reads and verifies the SQLite journal without an LLM or MORK. `run` prints the outcome, round reports, escalations, milestones and journal revision as JSON. It does not run formal ATP or independent replay, so its outcome is a workflow status, not a verified proof. The design's formal-run and replay CLI demo is still follow-up work.

Run `python -m pytest src/coordinator/tests` for tests; graph tests are skipped when LangGraph is not installed, and live MORK tests are skipped when the library is unavailable.

## Not yet covered

- Two cycles on the same proof cannot overlap: the gate requires a proposal's base revision to equal the head, so a second commit on one proof would be rejected as stale. Parallelism is across proofs until the gate can rebase a disjoint proposal.
- Tests run the research, critic, formalizer, and LLM alignment worker implementations with fixed LLM replies. Live model calls and a live Lean checker are not part of the coordinator tests. The five-mechanism response still needs its dedicated contract enforced in the research worker, and `alignment-review` escalations still route through research moves.
- Replay is not driven by the coordinator; a proof whose certificate awaits replay is parked.
- Rules 3 and 4 read obstructions directly; they do not yet use per-run routing decisions.
