# Mathematics-ai provider integration

This profile connects the persistent-ATP receiver to Mathematics-ai's HTTP API.
It preserves receiver admission, original run identity, scheduler leases,
artifact bytes and CommitGate ownership. Mathematical verification is a separate
exclusive replay pass.

## Deploy the companion backend

Use the companion branch `fix/persistent-atp-provider-integration`, based on
Mathematics-ai `cf97980`. A portable patch is included at
[`patches/mathematics-ai-provider.patch`](../../patches/mathematics-ai-provider.patch).
See [patch instructions](../../patches/README.md). The integration requires this
contract; pointing it at unmodified backend main fails the capability handshake.

The companion backend:

- Retains every Lean child in original order; PLN/DTS scores never create closure.
- Publishes complete local contexts and exact trace hashes.
- Enforces tactic-step and wall-clock budgets.
- Admits an identical run/request idempotently and rejects conflicting bodies.
- Stores run records with atomic replacement, fsync and one process owner.
- Measures the Lean version, project sources, import list and dependency commits.
- Replays against fresh Lean goals, compiles a complete declaration, audits its
  axioms, rejects proof placeholders and attests run/certificate/environment identity.

Configure the existing backend's model assets and Lean project as usual. For
example, in its environment:

```bash
export MATHS_AI_PROVIDER_ID=local-mathematics-ai
export MATHS_AI_FORMAL_RUNS_DIR=/absolute/path/to/experiment/formal-runs
export MATHS_AI_LEAN_PROJECT=/absolute/path/to/lean_project
export MATHS_AI_LEAN_IMPORTS=Init
export MATHS_AI_API_TOKEN='your-backend-token-at-least-16-characters'
uv run python -m maths_ai.api --port 8001
```

Use `MATHS_AI_LEAN_IMPORTS=Init,Mathlib` when the configured Lean project supplies
Mathlib. Search and replay use the same import list, included in the measured
environment fingerprint. Dependencies must have clean, pinned working trees.
The default project/toolchain in the backend is separate from the small test
project used by the integration suite; do not substitute a test fingerprint.

Backend token authentication is enabled when `MATHS_AI_API_TOKEN` is configured.
It protects capabilities and execution routes; `/health` remains public.

## Start the receiver

From persistent-ATP's project directory:

```bash
export PERSISTENT_ATP_FORMAL_URL=http://127.0.0.1:8001
export PERSISTENT_ATP_FORMAL_TOKEN='your-backend-token-at-least-16-characters'
export PERSISTENT_ATP_OMEGA_TOKEN='your-receiver-token-at-least-16-characters'
export ARTIFACT_DB_URL='postgresql://user:password@localhost/omega_experiment'

.venv/bin/python -m artifacts.schema
.venv/bin/python -m omegaclaw.cli doctor --profile real-atp \
  --provider-kind mathematics-ai-http --require
scripts/with-mork.sh -- .venv/bin/python -m omegaclaw.cli serve \
  --config src/omegaclaw/config/mathematics-ai.yaml
```

Use a dedicated artifact database with its schema applied. Set `MORK_LIBRARY`
to your provenance-compatible native library, or install it through
`scripts/with-mork.sh --install`.

The example configuration has a separate state root at
`tmp/runs/omega-mathematics-ai-service`. Existing canonical/reference roots need
an explicit migration; do not mix provider/run identities.

The receiver creates initial formal requests from committed, reviewed declaration
metadata. It does not manufacture semantic alignment or automatically elaborate
the original informal statement. A proof must already have:

- An exact reviewed claim/declaration alignment bound to source-artifact bytes.
- A closed Lean proposition in `lean_type`.
- One pinned Environment whose `environment_hash` equals the provider's measured
  fingerprint from authenticated `GET /capabilities`.

Environment/declaration pins are immutable. If the deployment changes, create
reviewed replacement inputs instead of overwriting old pins. Backend drift is
checked at search observation and replay; an old request is never silently rebound.

## Run Omega end to end

With the model and Omega/PeTTa settings from [PLAYGROUND.md](PLAYGROUND.md):

```bash
scripts/omega-playground.sh live \
  --atp-kind mathematics-ai-http \
  --proof-id existing-proof-id --claim-id existing-proof-id/c-1 \
  --max-steps 100 --wall-clock-ms 10000 \
  --provider custom --output tmp/omega-playground/maths-ai-001
```

The runner verifies the receiver's provider kind, persists exact tool intents,
observes the original operation, downloads request/result artifacts and validates
the exported chain. A found proof remains a candidate. A completed search may
instead be budget-exhausted or stagnated; neither establishes a theorem.

## Replay and optionally promote

Read the committed `run_id` from the playground's `observations.json`. Stop the
receiver so the verification command can exclusively own its native MORK state
and journal. Leave the Mathematics-ai backend running.

```bash
scripts/omega-playground.sh replay \
  --config src/omegaclaw/config/mathematics-ai.yaml \
  --run-id existing-proof-id/fr-REPORTED_RUN_NUMBER
```

This persists bound replay evidence and updates the certificate through a
replayer lease. To also propose the claim's verification promotion:

```bash
scripts/omega-playground.sh replay \
  --config src/omegaclaw/config/mathematics-ai.yaml \
  --run-id existing-proof-id/fr-REPORTED_RUN_NUMBER --promote
```

Promotion uses a different coordinator actor and fresh fenced proposals. The gate
still requires exact alignment, accepted replay, certificate bytes, environment
binding and a no-sorry verdict. Rejection preserves the claim's unverified status.
Repeating a completed verification reuses persisted evidence and does not append
duplicate proof events. Afterward, restart the receiver to observe status/events
and audit/export the replay report with the rest of the proof.

The SDK's `independent_replay` capability remains false: it does not offer an
online replay action. This trusted local command is the current verification path.

## Reproduce the integration checks

Set `MORK_LIBRARY`, a companion backend checkout and its lightweight test Python:

```bash
export MATHS_AI_BACKEND_ROOT=/absolute/path/to/companion/backend
export MATHS_AI_BACKEND_PYTHON="$MATHS_AI_BACKEND_ROOT/.venv/bin/python"
export MATHS_AI_TEST_LEAN_TOOLCHAIN=leanprover/lean4:v4.33.0
scripts/omega-playground.sh components --atp-kind mathematics-ai-http
```

The backend test environment needs pytest, FastAPI, httpx, pydantic, PyYAML,
regex and graphviz. Select an installed Lean toolchain. The suite creates its own
small Lean project and PostgreSQL cluster, uses native MORK and the actual
companion HTTP application, and checks real Lean compilation. Model/search inputs
are explicitly synthetic; it makes no paid model call and does not demonstrate
GNN-guided search with production weights.

Cases include provider-admission process death, duplicate admission, artifact
materialization, Omega tool dispatch, accepted/rejected Lean replay, and recovery
after a replay journal commit before lease release. Logs and reports are retained.
