# Omega receiver experiment

This checkout integrates the standalone client, Omega/PeTTa plugin, journal-backed
receiver and native MORK recovery work. The production profile uses PostgreSQL
artifacts and a configured canonical or Mathematics-ai HTTP formal executor. Reference execution
remains explicitly labeled and separate.

```text
Omega tool call -> persisted client intent -> SDK -> receiver admission ledger
    -> scheduler lease -> persisted canonical request/run binding -> HTTP ATP
    -> result artifacts -> CommitGate -> SQLite journal -> MORK projection
```

Operation completion is separate from mathematical verification. The receiver's
search path does not promote a target to `lean-verified`. The Mathematics-ai
profile provides a separate exclusive replay/promotion command; see
[Mathematics-ai integration](MATHEMATICS_AI.md).

For runnable live, local and production-recovery scenarios with saved logs and
reports, see [the end-to-end playground](PLAYGROUND.md).

## Install

From this experiment worktree, with Python 3.11 or later and uv:

```bash
uv sync --locked --extra dev --extra provider-sdk --extra omega
```

The `omega` extra requires a working SWI-Prolog installation to build/load
`janus-swi`. The tested runtime is Python 3.13 with SWI-Prolog 10.0.2.

Keep the upstream source checkouts pinned:

| Component | Commit |
| --- | --- |
| Omega | `63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5` |
| PeTTa | `7037f4c2ad378c52fc328004fe216d5118b674f0` |

Set `OMEGA_PINNED_ROOT`, `PETTA_PINNED_ROOT` and `PETTA_PYTHON` to the absolute
paths of those checkouts and this worktree's `.venv/bin/python`, respectively.

```bash
.venv/bin/python -m omegaclaw.cli doctor --profile omega --require
```

## Reference baseline

Set `PERSISTENT_ATP_OMEGA_TOKEN` to a local receiver token of at least 16 characters:

```bash
.venv/bin/python -m omegaclaw.cli serve --config src/omegaclaw/config/reference.yaml
```

The reference profile uses durable SQLite admissions, a journal, disk-backed
reference bytes, an in-memory graph and a scripted critic. Formal execution is
unavailable. Its capability metadata identifies these limits.

## Configure production storage and execution

Use a dedicated experiment database. Set `ARTIFACT_DB_URL`, apply the artifact
schema deliberately, then configure the formal provider URL and token:

```bash
.venv/bin/python -m artifacts.schema
.venv/bin/python -m omegaclaw.cli doctor --profile real-atp --require
scripts/with-mork.sh --install -- .venv/bin/python -m omegaclaw.cli serve \
  --config src/omegaclaw/config/production.yaml
```

The production configuration reads these host environment variables:

| Variable | Purpose |
| --- | --- |
| `ARTIFACT_DB_URL` | PostgreSQL artifact database with the schema applied |
| `PERSISTENT_ATP_FORMAL_URL` | Canonical formal provider base URL |
| `PERSISTENT_ATP_FORMAL_TOKEN` | Provider credential required by the example config |
| `PERSISTENT_ATP_OMEGA_TOKEN` | Receiver credential |
| `MORK_LIBRARY` | Native library selected and checked by `with-mork.sh` |

The service verifies the journal and loaded MORK provenance, optionally restores
an absolute `snapshot` bundle path, and catches up every proof before startup.
`GET /health` reports projection readiness and returns 503 while projection is
behind or unavailable. Operation records remain observable during projection lag.

One production receiver owns a state root and native Space per process. Changing
the authoritative journal requires a fresh process. A separate `mork.host`
process does not share its native Space with the receiver.
The state root is bound to its mode/workspace. Opening reference data as production
requires an explicit migration; pointing a new configuration at it is rejected.

### Formal provider contract

In addition to the canonical formal search/status routes, the provider must expose
`GET /capabilities` with a stable identity and these explicit guarantees:

```json
{
  "provider_id": "operator-assigned-stable-provider-identity",
  "backend_mode": "real",
  "idempotent_start": true,
  "durable_runs": true,
  "complete_local_context": true,
  "certificate_witnesses": true
}
```

Identical run IDs and exact request bodies must be idempotent, including a
delayed original start racing a retry after a 404 lookup. Status responses must
satisfy the canonical schema and preserve proof, declaration, run and environment
identity. States need versioned exact identities and complete local contexts;
certificates need the seven-field witness contract.

Artifact pointers must already exist in the configured store or be downloadable
through `GET /artifacts/{percent-encoded-sha256}` returning:

```json
{"content_base64": "ZXhhbXBsZQ=="}
```

Downloads are bounded to 16 MiB of bytes, verified by hash and persisted before
the gate commits. HTTP requests have a total deadline, a 24 MiB decoded response
limit, strict JSON parsing and no redirects or ambient proxies.

These advertised guarantees are a trusted provider deployment contract, not
independent evidence of a Lean proof. The Mathematics-ai profile uses a separate
provider-specific contract with complete contexts, durable request restoration,
materialized artifacts and bound replay evidence; see [its guide](MATHEMATICS_AI.md).

### Supported operations

The receiver negotiates contracts `2.0` and `2.1`. SDK `2.2` DTOs are available,
but this receiver does not advertise atomic coordinator escalation batches.

- Creation, status, audit, export and speculative guidance use the configured stores.
- Formal search and selected `formal-run` cycles use the configured HTTP executor.
- Search requires an eligible declaration with reviewed exact alignment, an exact
  source-artifact binding, an immutable Lean proposition and one pinned environment.
  Creating a proof does not manufacture this reviewed input.
- Initial declarations must be closed propositions; nonempty local contexts need
  an elaboration integration and are rejected. Child contexts from the provider
  must still be complete. The provider needs authorized access to the source
  artifact bytes through its configured store or artifact retrieval integration.
- Formal resume, cancellation, budget changes, statement alignment, lemma bridging,
  and research/critic execution are rejected when their executor is unavailable.

The trusted host currently compiles initial formal requests from those committed
reviewed declaration/environment fields. It does not yet consume the full
research/formal/obstruction context portfolio or derive Lean goals from source.
Searching snapshots are durable artifacts; incremental graph ingestion and
checkpoint continuation remain separate work.

### Recovery behavior

Admission persists a request ID, exact body, fingerprint, operation ID and events.
Production dispatch uses an operation-derived lease identity and persists the
canonical request, run ID, executor identity and result references before commit.
Restart first reconciles the original run; it never invents a replacement run or
changes the original request's lease, revision, deadline or budget.

The gate is the sole proof writer. A committed journal event survives a failed
projection or a missing terminal operation update. Recovery catches up MORK and
reconstructs the original commit receipt. Stale/expired leases cannot commit;
their result bytes remain available for audit. Such work needs explicit fresh
assessment, rather than automatic rebasing. Pending observations use bounded
backoff. Receiver startup requires its configured storage and provider handshake.

## Bounded Omega experiment

Install into a derived Omega copy, leaving the pinned source intact:

```bash
cp -a "$OMEGA_PINNED_ROOT" tmp/Omega-experiment
.venv/bin/python scripts/omegaclaw-install-omega.py --omega-root tmp/Omega-experiment
```

Set receiver credentials and the two durable-intent settings:

```bash
export PERSISTENT_ATP_OMEGA_URL=http://127.0.0.1:8765
export PERSISTENT_ATP_OMEGA_INTENTS_DB="$PWD/tmp/omega-client/intents.sqlite3"
export PERSISTENT_ATP_OMEGA_RECEIVER_ID=local-production-principal
```

Keep that receiver ID bound to the same endpoint/principal. The client intent file
must be separate from the receiver and proof databases. Configure one existing
provider profile (`custom`, `deepseek`, `openai` or `openrouter`); the custom profile
uses `OMEGACLAW_CUSTOM_BASE_URL`, `OMEGACLAW_CUSTOM_API_KEY` and `OMEGACLAW_CUSTOM_MODEL`.

```bash
.venv/bin/python -m omegaclaw.experiment \
  --omega-root tmp/Omega-experiment --petta-root "$PETTA_PINNED_ROOT" \
  --state tmp/omega-client/run-001.json --provider custom --turns 3 \
  --prompt 'Create a new proof named omega_demo for the target True, then read its status and audit. Use workspace production.'
```

The driver loads Omega's actual provider registry and MeTTa plugin in an isolated
derived runtime. It saves model tool calls and host-generated intent identities
before execution. Repeating the command resumes that checkpoint; choose a new
state file and proof ID for a deliberate new experiment. Observation uncertainty
stops the driver with exit code 2, preserving the intent for reconciliation.
Use a new state/runtime directory after changing Omega or plugin code.

Production mutations require this trusted intent path. Calling the original
string-only mutation skills directly returns `INTENT_REQUIRED`; automatic intent
propagation through upstream Omega's unrestricted main loop is not implemented.
Read-only proof status and coordination reads remain available directly.

## Verification

For a disposable PostgreSQL cluster and native MORK, set `MORK_LIBRARY` to the
provenance-compatible library, then run:

```bash
scripts/test-omega-production.sh
scripts/test-omega-production.sh --all
```

The script needs PostgreSQL server binaries; override `OMEGACLAW_TEST_POSTGRES_BIN`
if they are not under `/usr/lib/postgresql/18/bin`. It uses a new temporary cluster
and stops it on exit. It never uses the existing artifact database. Full Omega
process coverage also needs the pinned source/interpreter environment above.
For the legacy process harness's SDK variant, set `OMEGACLAW_PROVIDER_SDK_PATH`
to this environment's site-packages directory (for the tested Python 3.13 venv,
`$PWD/.venv/lib/python3.13/site-packages`).

Tests exercise native MORK, PostgreSQL, gate commits, duplicate identity, process
death after admission/lease issuance/provider start/gate commit, projection repair,
wrong-root rejection, strict transport deadlines and the actual Omega/PeTTa driver.
The generic ATP and LLM endpoints are synthetic fixtures. The Mathematics-ai
selection additionally uses its actual API and real Lean replay compilation,
with synthetic search/model inputs. No production model-guided search or paid
model call is established by those results. The bounded-driver restart and provider-SDK tests also passed
against Omega `5489ac02f54e3059d642bf86d3b76ada8c994273`, the upstream tip
checked on November 10, 2026. This checks the experiment path, not every upstream
Omega feature.
