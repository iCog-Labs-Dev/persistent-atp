# End-to-End Agent Experiment

This guide demonstrates the system implemented on
`feat/context-agent-integration`. It uses an ordinary-language theorem and
keeps the run inspectable through a SQLite journal and a JSONL event log.

## What this demo proves

The experiment shows that these real components work together:

- Gemini researcher, critic, formalizer, and alignment reviewer;
- proof-scoped scheduling and context compilation;
- proposal validation through the commit gate;
- durable events in the SQLite journal;
- live projection into the real MORK FFI backend;
- Lean elaboration of the generated proposition.

The codebase includes a real Mathematics-AI HTTP adapter for formal search and
independent replay. This particular experiment still uses `--test-mode` for
those two stages; it is a deterministic integration demonstration, not a test
of the external ATP service. Therefore the final outcome is `test-verified`,
which means the integration path completed. It does **not** mean Lean verified
a proof.

## Workflow

```text
Ordinary-language theorem
        |
        v
Researcher proposes an argument
        |
        v
Critic checks the argument
        |
        v
Formalizer produces a Lean proposition
        |
        +--> Lean checks that the proposition elaborates
        |
        v
Alignment reviewer compares informal and Lean statements
        |
        v
Fake ATP certificate --> fake replay acceptance
        |
        v
Explicitly labelled test-verified result
```

At every stage, workers return results but do not write graph state directly.
Dispatch converts a result into a proposal, the commit gate validates it, the
SQLite journal records accepted events, and MORK receives the live graph
projection.

## Where the workflow is implemented

- `src/coordinator/`: round orchestration, portfolio slots, CLI, and logging.
- `src/mathproof/scheduler.py`: proof-scoped frontier selection and leases.
- `src/context_compiler/`: deterministic context packets for worker tasks.
- `src/mathproof/workers/`: researcher, critic, and formalizer agents.
- `src/alignment/`: alignment worker and Gemini reviewer.
- `src/mathproof/lean_statement.py`: real Lean proposition elaboration.
- `src/mathproof/maths_ai_http.py`: real Mathematics-AI search/replay adapter.
- `src/commit_gate/`: proposal validation and authoritative journal writes.
- `mork/backend/`: live MORK FFI projection and journal catch-up.
- `scripts/all-agent-exp.py`: guarded end-to-end experiment described here.
- `scripts/with-mork.sh`: MORK discovery, pinned installation, and preload.

## Prerequisites

- Python 3.11 or newer;
- `uv` for the Python environment;
- a Gemini API key;
- a Lean project in which `lake env lean` works;
- Git, Cargo, and a nightly Rust toolchain if MORK must be built locally.

Install the Python dependencies:

```bash
uv sync --extra coordinator
```

Add a Gemini key to `.env`:

```text
GEMINI_API_KEY=your-key
```

If necessary, also select a Gemini model available to the account:

```text
GEMINI_MODEL=gemini-3.5-flash-lite
```

Install or locate the pinned MORK FFI library once:

```bash
scripts/with-mork.sh --install true
```

The setup script reuses an existing `libmork_ffi.so` when possible. Otherwise,
`--install` clones the pinned MORK, PathMap, and `mork_ffi` revisions and builds
the library.

## Run the recommended demo

From the repository root, run:

```bash
.venv/bin/python scripts/all-agent-exp.py --run-id add-zero-demo
```

The default input is intentionally ordinary language:

```text
If you add zero to any natural number, you get the same number back.
```

The script does not provide the agents with a formal statement or a proof
hint. It initializes the first task only as:

```text
Investigate the user's statement and take the next justified step.
```

To send the formalized proposition to a running Mathematics-AI API, pass its
URL. The coordinator uses the merged HTTP adapter and records the provider's
actual search result:

Real ATP also requires a PostgreSQL artifact database. Create an empty database,
set `ARTIFACT_DB_URL`, then apply the schema once with
`PYTHONPATH=src .venv/bin/python -m artifacts.schema`. Lean source and candidate
proof traces are stored there by content hash before the journal references
them. Keep this database when continuing a proof after restarting the CLI.

```bash
.venv/bin/python scripts/all-agent-exp.py \
  --formal-atp-url http://127.0.0.1:8001 \
  --rounds 6
```

If search exhausts, the run reports that failure and retains its journal and
log. Replay runs only when the provider returns a candidate proof trace.
Real verification requires replay to attest the run, certificate, and Lean
environment; a bare `verified: true` response is insufficient.

The script automatically finds a sibling `Pantograph` project or
`OmegaClaw-Core/lean`. If neither exists, pass a Lean project explicitly:

```bash
.venv/bin/python scripts/all-agent-exp.py \
  --run-id add-zero-demo \
  --lean-project /path/to/lean-project
```

Run IDs must be fresh. If the artifact directory already exists, the script
stops instead of overwriting evidence. Omitting `--run-id` creates a unique
timestamp-and-UUID name.

## Expected example

In a successful observed run, the agents produced this progression:

1. The researcher selected structural induction.
2. It produced a candidate argument with a base case and successor step.
3. The critic returned `supported`.
4. The formalizer produced `∀ (n : Nat), n + 0 = n` with import `Init`.
5. Lean successfully elaborated that proposition.
6. The alignment reviewer returned `aligned` with relation `exact`.
7. Test-mode ATP created a fake certificate.
8. Test-mode replay accepted it, producing `test-verified`.

Model output is nondeterministic, so the exact reasoning and number of
research moves can differ. The experiment fails clearly if any expected agent
does not run or if the terminal workflow is not reached within the configured
rounds.

The final console summary has this shape:

```json
{
  "mork_used": true,
  "agents_ran": [
    "llm-research",
    "critic",
    "formalizer",
    "alignment-reviewer",
    "formal-atp",
    "replayer"
  ],
  "outcome": "test-verified",
  "verification_mode": "test",
  "verification_notice": "TEST VERIFICATION ONLY: ATP and replay were faked; this is not a real Lean proof.",
  "proof_id": "nat-add-zero-...",
  "journal": ".../proof.sqlite",
  "log": ".../events.jsonl"
}
```

## Inspect the artifacts

Each run creates one directory:

```text
runs/all-agent/<run-id>/
├── proof.sqlite
└── events.jsonl
```

`proof.sqlite` is the authoritative journal. It contains the accepted proof
history and can rebuild the graph projection. `events.jsonl` is the readable
runtime trace. It includes concise normalized agent responses when the
experiment enables agent-response logging, but excludes prompts, compiled
context packets, API keys, and HTTP payloads.

Read the durable proof status using the `proof_id` printed by the experiment:

```bash
.venv/bin/python -m coordinator status \
  --journal runs/all-agent/add-zero-demo/proof.sqlite \
  --proof-id <printed-proof-id>
```

Show only agent responses:

```bash
jq 'select(.event == "agent_response") | {
  round, slot, worker_class, validation_status, response
}' runs/all-agent/add-zero-demo/events.jsonl
```

Show MORK projection updates and the final result:

```bash
jq 'select(
  .event == "projection_updated" or .event == "run_completed"
)' runs/all-agent/add-zero-demo/events.jsonl
```

MORK is the live in-process read model, so this experiment does not create a
separate MORK database file. The journal is the durable source of truth, and
the `projection_updated` events show the revisions projected into `MorkView`.

## Run the rejection safety demo

The success workflow is complemented by a deterministic failure experiment:

```bash
.venv/bin/python scripts/rejection-demo.py --run-id rejected-write-demo
```

This script uses a real SQLite journal, commit gate, and MORK FFI backend. It
first commits valid seed state, then gives a `formal-atp` worker a valid lease
and submits an incomplete `TacticApplication`. The proposal is intentionally
invalid, so the expected behavior is:

- the commit gate rejects it with typed reasons;
- the rejection is appended to the audit trail;
- the accepted journal revision does not advance;
- the MORK projected revision and atoms remain unchanged;
- the accepted journal hash chain still verifies.

The run creates:

```text
runs/rejection-demo/<run-id>/
├── proof.sqlite
├── result.json
└── mork-state.metta
```

`result.json` records every invariant check. `mork-state.metta` is one
inspectable snapshot of the unchanged live graph after rejection. The
rejected proposal itself is available in the SQLite rejection audit, not in
the accepted event chain.

## Try another statement

The same workflow accepts another ordinary-language theorem:

```bash
.venv/bin/python scripts/all-agent-exp.py \
  --statement "The sum of two even natural numbers is even."
```

This is exploratory: a harder theorem may need more rounds, may expose a real
agent or integration failure, or may not reach the terminal stages. Increase
the budget with `--rounds`, but do not interpret `budget-exhausted` as a
mathematical refutation.

## Run the stages manually

The experiment script is the recommended demo because it creates fresh
artifacts and checks every expected stage. For debugging, the same system can
be driven through the CLI:

```bash
.venv/bin/python -m coordinator init \
  --journal runs/manual-demo.sqlite \
  --proof-id manual-demo \
  --statement "If you add zero to any natural number, you get the same number back."

scripts/with-mork.sh \
  .venv/bin/python -m coordinator run \
  --journal runs/manual-demo.sqlite \
  --proof-id manual-demo \
  --backend mork \
  --lean-project /path/to/lean-project \
  --test-mode \
  --rounds 4 \
  --verbose \
  --log-file runs/manual-demo.jsonl \
  --log-agent-responses
```

The manual command is useful for continuing an existing journal. The
experiment script is better for a reproducible presentation because every run
uses a new proof ID, checks that all six worker classes ran, and clearly labels
the fake terminal stages.

## Common failures

- `no libmork_ffi.so found`: run `scripts/with-mork.sh --install true`, or set
  `MORK_LIBRARY` to the real library.
- `no Lean project found`: pass `--lean-project` explicitly.
- Gemini authentication or model error: verify `GEMINI_API_KEY` and select a
  model available to the account with `GEMINI_MODEL`.
- `experiment artifacts already exist`: choose a new `--run-id` or omit it.
- Missing expected agent: inspect `events.jsonl`; retained artifacts show the
  last valid response and workflow stage.
