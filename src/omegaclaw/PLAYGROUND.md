# End-to-end test playground

Run from the project directory on `experiment/omega-test-playground`. The playground
uses this checkout's `.venv/bin/python`. It saves evidence and leaves it in
`tmp/omega-playground/`. Running a scenario does not commit or push files.

```bash
scripts/omega-playground.sh --help
```

For the Mathematics-ai ATP, provider setup, live search and separate Lean replay,
follow [MATHEMATICS_AI.md](MATHEMATICS_AI.md). Require that profile with
`live --atp-kind mathematics-ai-http`; run its full component checks with
`components --atp-kind mathematics-ai-http`.

## Live end-to-end run

Use your running production receiver, configured ATP backend and model provider.
Choose an existing proof with a reviewed exact alignment, declaration source and
pinned environment. The receiver remains the authority for deciding whether
that input is eligible for execution.

Set the existing deployment's connection details in your shell:

```bash
export PERSISTENT_ATP_OMEGA_URL=http://127.0.0.1:8765
export PERSISTENT_ATP_OMEGA_TOKEN='your-existing-receiver-token'
export PERSISTENT_ATP_OMEGA_RECEIVER_ID='your-stable-receiver-principal'

export OMEGACLAW_CUSTOM_BASE_URL='https://your-model-provider.example/v1'
export OMEGACLAW_CUSTOM_API_KEY='your-model-key'
export OMEGACLAW_CUSTOM_MODEL='your-model'

scripts/omega-playground.sh live \
  --proof-id existing-proof-id \
  --claim-id existing-proof-id/c-1 \
  --provider custom \
  --max-steps 100 --wall-clock-ms 10000 \
  --output tmp/omega-playground/live-001
```

The existing `openai`, `deepseek` and `openrouter` profiles are also selectable
with `--provider`; their credential/model environment variables are documented
in [the setup guide](README.md). No paid call is made by local or component mode.
Live mode makes calls to your selected model and can dispatch one real formal run.

The live scenario runs:

```text
Omega model → PeTTa tool plugin → persisted intent → standalone SDK → HTTP
  → production admission → scheduler/executor → CommitGate → journal → MORK
  → status/events → artifact download → audit → independently checked export
```

It creates a private derived Omega copy and an exact tool plan: one formal search,
then status and audit. The trusted driver rejects a different proof, claim,
workspace, budget or action before it reaches the receiver. An operation passes
only when its authoritative receipt has a run ID and commit hash, its saved
request/result artifacts can be downloaded and hash-checked, the projection is
caught up, and the journal export validates. Budget exhaustion can be a successful
search operation; it is not a proof of the target.

Repeat the identical command with the same output directory to resume an
uncertain observation. The model response and prepared request remain saved;
the runner reattaches to the original operation. A completed checkpoint does not
make another model call or start another formal run. A new output directory is a
deliberate new experiment and may dispatch another run.

Live mode attaches to your receiver. It does not start, stop or restart that
deployment. It rejects reference receivers instead of silently falling back.

## Repeatable local playground

```bash
scripts/omega-playground.sh local --output tmp/omega-playground/local-001
```

This owns a loopback reference receiver, a deterministic model fixture, actual
Omega/PeTTa processes, the HTTP SDK, SQLite admissions/journal and reference
artifact bytes. It needs no model credentials, PostgreSQL or native MORK.

The fixture calls creation, a selected critic cycle, audit, status and coordination
reads. The runner verifies duplicate admission, terminal events, artifact hashes
and a self-contained export. It then restarts its receiver and resumes the same
Omega checkpoint, checking that proof revision/hash and model-call count do not
change. Processes started by this scenario are stopped on completion or failure.

Expected: `PASS` lines, report status `passed`, revision `2`, and
`formal_verified: false`. Use a fresh output directory for each local run.

## Production component and recovery playground

With your provenance-compatible `MORK_LIBRARY` set:

```bash
scripts/omega-playground.sh components \
  --output tmp/omega-playground/components-001
```

This launches a disposable PostgreSQL cluster and the native MORK production
integration suite with a strict zero-skip policy. The HTTP ATP endpoint is an
explicit synthetic test fixture. Cases cover duplicate admission, process death
after admission/lease/provider start/gate commit, projection repair, wrong-root
rejection and unavailable-backend rejection. No existing artifact database is used.

PostgreSQL binaries default to `/usr/lib/postgresql/18/bin`; set
`OMEGACLAW_TEST_POSTGRES_BIN` for another installed version. The temporary cluster
is stopped on exit. Logs and test files are retained for inspection.

## Evidence to inspect

| File | What it shows |
| --- | --- |
| `report.md`, `report.json` | Outcome, passed checks and failure stage |
| `session.json`, `plan.json` | Receiver/proof identity and authorized tool sequence |
| `driver.log` | Omega tool results and runtime errors |
| `experiment.json` | Model/tool history, pending intents and turn checkpoint |
| `intents.sqlite3` | Exact prepared requests and operation associations |
| `observations.json` | Status, coordination records, operation snapshots/events, audit and export check |
| `target.txt`, `proof-export.json` | Downloaded target and verified journal/artifact bundle |
| `receiver.log`, `receiver/` | Locally owned reference service only |
| `components.log`, `components.xml`, `pytest/` | Production integration results in component mode |

The console prints the absolute report location. The default output directory
contains a unique timestamp if `--output` is omitted. An output directory is
locked while running. Credentials are read from the environment and excluded
from the report/session manifests. Tool/proof content is retained as evidence.

Exit `0` means all selected checks passed. Exit `2` means failure, timeout or
reconciliation is required; inspect the report and logs. The live output directory
retains the original request identity for another observation attempt.

## Runtime selection

The runner finds Omega/PeTTa under this checkout's or the original worktree's
`tmp/repos/`. To select existing checkouts explicitly:

```bash
export OMEGA_PINNED_ROOT=/absolute/path/to/Omega
export PETTA_PINNED_ROOT=/absolute/path/to/PeTTa
```

Or pass `--omega-root` and `--petta-root`. The source checkouts are copied and
left untouched. Python must have the `provider-sdk` and `omega` extras installed,
and SWI-Prolog must be available. Use a new output after changing source/plugin
code; existing derived runtimes are intentionally retained for recovery.

The live search report preserves the receiver's verification status without
treating operation success as verification. The Mathematics-ai profile adds
`scripts/omega-playground.sh replay --config ... --run-id ... [--promote]` as a
separate exclusive verification pass; stop the receiver first. See its guide.
