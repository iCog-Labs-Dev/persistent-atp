# OmegaClaw integration

This checkout has a **reference integration**, with Omega as a client of a
loopback operation service. The service owns the disk SQLite proof journal,
operation ledger, and reference byte artifacts. `CommitGate` is the only writer
of committed proof graph changes. The reference projection is `MemoryView`,
rebuilt from the journal on restart. The reference critic is scripted and
executes in a child process with a fixed two-second deadline, then creates a
pending attempt; it supplies no formal proof. Malformed output and timeout
retain exact output/diagnostic artifacts without a graph commit. No default replay can
verify a claim.

## Setup and reproducible runs

Use Python 3.11 or newer. From the repository root:

```bash
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache uv sync --extra dev
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache uv run persistent-atp-omega doctor --profile reference --require
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache bash scripts/omegaclaw-test.sh --tier reference
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache uv run persistent-atp-omega smoke --profile reference --output .runs/omega-reference
```

The smoke command creates a unique disk workspace and writes its full result
to `smoke-result.json` there. It exits nonzero if creation or the one scripted
cycle fails. `formal_verified` remains false.

For a local service, set a private token of at least 16 characters and start:

```bash
export PERSISTENT_ATP_OMEGA_TOKEN='replace-with-a-private-16-character-token'
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache uv run persistent-atp-omega serve --config omegclaw/config/reference.yaml
```

The JSON-formatted config file is named `.yaml` for the intended profile path.
It fixes workspace `reference` and loopback port 8765. The service requires a
Bearer token on `POST /v1/operations` and `GET /v1/operations/{id}`; `GET
/health` is public. Operation requests have exactly `schema_version`,
`request_id`, `workspace`, `operation`, and `arguments`. All skill arguments
are bounded strings. There is no raw proposal, lease, actor, filesystem, or
MORK write endpoint.

Omega's plugin is under `omegclaw/omega-plugin/`. A derived runtime must copy
it to `/opt/persistent-atp/omega-plugin` and append
`omegclaw/config/omega-plugins.overlay.yaml` to the actual Omega
`config/plugins.yaml`, preserving its existing entries. `PERSISTENT_ATP_OMEGA_URL`
and `PERSISTENT_ATP_OMEGA_TOKEN` are the only service settings the Omega bridge
uses. Keep the proof journal and artifact directory out of Omega's mounts.

For the pinned process test, set `OMEGA_PINNED_ROOT` to Omega revision
`63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5` and `PETTA_PINNED_ROOT` to
PeTTa v1.0.4 revision `7037f4c2ad378c52fc328004fe216d5118b674f0`.
The test needs SWI-Prolog, `janus_swi` importable by `PETTA_PYTHON` (default
`/usr/bin/python3`), and a Python 3.11+ project environment. Run:

```bash
git clone https://github.com/singnet/Omega.git /tmp/persistent-atp-omega-pinned
git -C /tmp/persistent-atp-omega-pinned checkout 63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5
git clone https://github.com/trueagi-io/PeTTa.git /tmp/persistent-atp-petta-v1.0.4
git -C /tmp/persistent-atp-petta-v1.0.4 checkout 7037f4c2ad378c52fc328004fe216d5118b674f0
OMEGA_PINNED_ROOT=/tmp/persistent-atp-omega-pinned \
  PETTA_PINNED_ROOT=/tmp/persistent-atp-petta-v1.0.4 \
  UV_CACHE_DIR=/tmp/persistent-atp-uv-cache bash scripts/omegaclaw-test.sh --tier omega
```

The process test copies both sources into a test-owned temporary directory.
It loads the generated plugin catalogue, loads the plugin twice, checks the
nine registrations, calls `proof-create` through MeTTa and HTTP, and checks
that the subsequent Omega provider request contains the returned journal
digest and target artifact pointer. It does not run an autonomous model loop.

## Operation contract

| Omega skill | Service action in this checkout |
| --- | --- |
| `proof-create(spec, workspace)` | Strict spec, target bytes, trusted initialization lease and gate commit |
| `proof-cycle(proof, workspace)` | One routed lease and bounded scripted critic attempt; other routes return explicit blockers; busy and pending-review work stays waiting |
| `proof-status(proof, workspace)` | Journal-derived claim status and revision |
| `proof-audit(proof, workspace)` | Journal chain and referenced byte checks |
| `proof-export(proof, destination, workspace)` | `default` alias; hash-checked journal/artifact bundle |
| `formal-search(proof, claim, budget, workspace)` | Validates and clamps budget; reports alignment/backend blocker |
| `formal-resume(proof, run, budget, workspace)` | Reports unsupported checkpoint continuation |
| `statement-align(proof, claim, lean_source, workspace)` | Resolves exact source bytes; heuristic review stays ambiguous/review-needed |
| `lemma-bridge(proof, obstruction, workspace)` | Requires typed, linked stagnation; commits a speculative reference candidate with provenance |

The `lean_source` value is an existing `sha256:` artifact reference in the
trusted artifact store. Trusted local operators can upload exact input bytes
before an Omega operation:

```bash
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache uv run persistent-atp-omega artifact-put \
  --config omegclaw/config/reference.yaml --file path/to/declaration.lean
```

This command prints the content address for `statement-align`; it runs on the
trusted side and is not an Omega skill. A `proof-cycle` result
of `pending-review`, `waiting-on-review`, `waiting-on-lease`, `blocked`, or
`idle` is never proof completion. The
operation ledger is separate from the mathematical journal. Duplicate
`request_id` with identical bytes returns the same result; changed content is
rejected.

## Real-tier status

Run `bash scripts/omegaclaw-test.sh --tier {persistence|lean|real-atp}` to
get fail-closed doctor output. This branch lacks `mork/backend/view.py` and
`scripts/with-mork.sh` from the spec's dev baseline, so real MORK/Postgres
projection is untested. Independent Lean replay, durable ATP checkpoint
continuation, actual ThreadKeeper/Hyperon transport, derived deployment
image, and full autonomous cycles remain open. The installed Lean executable
alone does not establish formal readiness. See
`docs/omegaclaw-implementation-map.md` and `docs/omegaclaw-test-report.md`
for the exact tested scope and blockers.
