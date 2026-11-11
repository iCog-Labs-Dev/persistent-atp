# Omega and Mathematics-ai integration review

Branch: `experiment/omega-test-playground`, based on `origin-team/dev` at
`3211997`, in the main project directory. The original experiment worktree and
pre-existing untracked project files are preserved. No `docs/` directory is part
of the intended commits.

## Included foundations

The branch includes prerequisite implementations that were not present on dev:

| Component | Source |
| --- | --- |
| Receiver/Omega plugin | `feat/omegaclaw-coordinator-escalations` at `914e0b3` |
| Native MORK recovery and witness contracts | `feat/mork-implementation-expt` at `ce4c25c` |
| Standalone SDK | `feat/intermediate-layer-corrected` at `1a2cbae` |

Receiver/MORK overlays use changes since `5565f36`; existing dev obstruction
routing and fail-closed replay policy are retained. The receiver negotiates
contracts 2.0/2.1; atomic coordinator escalation batches are not advertised.

## Review the implementation

1. `factory.py`, `production.py`, `execution.py`: explicit real provider
   selection, PostgreSQL artifacts, durable operation/run/request identity,
   bounded transport, fenced dispatch and gate-only proof writes.
2. `../mathproof/maths_ai_http.py`: strict native API translation, complete
   child contexts, actual environment/budget checks, run-scoped identities,
   certificate witnesses and downloadable content-addressed bytes.
3. `verify.py`: exclusive replay pass with separate replayer and promotion
   actors. It persists exact evidence before graph commits and recovers
   completed stages without duplicate proof mutations.
4. `experiment.py`, `omega_plugin/omega_intents.py`, `playground.py`: persisted
   host intent and model responses, trusted exact tool plans, checkpoint
   recovery, status/events/audit/export assertions and saved reports.
5. `../commit_gate/store.py`, `../mathproof/scheduler.py`: operation-derived
   dispatch identity and eligible formal selection, preserving fencing.

The standalone SDK owns its own package files. The main checkout's editable
installation points to that local package. Namespace discovery ignores preserved
legacy placeholder directories in the project root.

## Companion backend

The backend branch `fix/persistent-atp-provider-integration` starts at upstream
`cf97980`. Its portable patch is included under `patches/`, excluding `docs/`.
It retains every Lean child in original order, removes heuristic closure,
exports full contexts, enforces step budgets, admits identical runs idempotently,
and uses atomic durable run records with one process owner. Replay compiles a
complete declaration and audits its axioms; the response binds the exact run,
trace bytes and measured Lean environment. Configured search/replay imports
are identical and included in the environment fingerprint.

The HTTP integration requires this companion contract. The optional direct
Python backend dependency in uv.lock remains at its earlier published revision;
no unpublished local commit is inserted into the lock file.

## Verification

```bash
scripts/omega-playground.sh local
scripts/omega-playground.sh components
scripts/omega-playground.sh components --atp-kind mathematics-ai-http
scripts/test-omega-production.sh --all
```

The Mathematics-ai selection uses the actual companion API, native MORK,
PostgreSQL and actual Lean compilation. Search/model inputs are explicit fixtures.
It covers accepted/rejected replay, receiver process death after provider
admission, recovery after a replay journal commit, duplicate identity, artifact
hashes, Omega dispatch and verification promotion through CommitGate. Backend
API/search tests additionally exercise environment drift, token authentication,
atomic replacement failures, mandatory subgoals and step budgets.

The complete integration passed **1,225 tests, with one optional external-receiver
skip and 167 subtests passed**. The companion backend API/search selection passed
**46 tests**, including real Lean compilation. The strict native Mathematics-ai
selection passed **15 tests without skips**.

## Boundaries

- No production-weight GNN search or paid model call is established by fixture
  runs. A live deployment needs its actual model assets and pinned Lean project.
- Initial declarations still require competent, exact alignment review and a
  measured environment pin. The receiver does not manufacture that review.
- Online SDK replay remains unavailable. Stop the receiver and use the separate
  replay command; its ownership lock prevents concurrent journal/native owners.
- Resume, cancellation, budget amendment, research/critic execution and escalation
  batches are rejected when no executor is configured.
- Searching snapshots are durable artifacts; incremental graph ingestion and
  search-frontier continuation remain separate work.

See [PLAYGROUND.md](PLAYGROUND.md) and [MATHEMATICS_AI.md](MATHEMATICS_AI.md) for
live commands and the exact test/deployment distinction.
