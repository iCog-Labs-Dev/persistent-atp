# Persistent Knowledge Mining

**A durable, race-safe metagraph memory system for long-horizon automated theorem proving.**

---

## Description

This prototype introduces persistence into the theorem-proving search loop. It extends the existing symbolic knowledge infrastructure into a **MORK-backed PeTTa metagraph** that maintains research strategy, formal proof states, statement alignment, and provenance across long-running proof search sessions — rather than losing this context between runs, as the baseline did.

Writes to the knowledge base go through a **deterministic commit gate** and an **append-only journal**, ensuring every modification is traceable, reproducible, and protected from unauthorized direct mutation.

For this early prototype phase, the system is built on a contemporary off-the-shelf database to keep development pace steady, with a path toward the full MORK/PeTTa-native backend as the design matures.

## Context compilation

The context compilation subsystem builds deterministic, budgeted context
packets for formal ATP, research, and obstruction-analysis tasks. See the
[context compiler guide](src/context_compiler/README.md) for its architecture,
end-to-end usage, selection behavior, outputs, and current integration
boundaries.

## Omega integration

The Omega plugin exposes nine proof tools through the standalone
`persistent-atp-client` SDK. `OmegaAdapter` owns SDK calls and request recovery;
`OmegaState` stores serializable workflow checkpoints. The bridge parks network
work on worker threads and delivers results to Omega history on heartbeat.
Dispatch receipts and receiver admission are separate from execution results
and mathematical verification.

Use Python 3.11 or later, SWI-Prolog with `janus_swi`, and these exact runtimes:

- Omega: `ba185cd359d9fe231b904110b988af39fafa5014`.
- PeTTa: `7037f4c2ad378c52fc328004fe216d5118b674f0`.

Install the project and its editable SDK with `uv sync --extra dev`. Copy the
trusted receiver settings from `.env.example` into your environment, then run:

```bash
uv run persistent-atp-omega serve --config reference
```

Install the plugin into a derived Omega checkout under your PeTTa runtime's
`repos/Omega` directory:

```bash
uv run python scripts/omegaclaw-install-omega.py --omega-root /path/to/PeTTa/repos/Omega
```

The installer preserves the plugin catalogue and patches the launcher's import.
For bare PeTTa, run that generated launcher from the PeTTa directory. The added
import shim checks the provisioned checkout's exact Git revision and never
fetches another runtime. The installer checks the Git revision and unchanged
core sources, then writes a SHA-256 build manifest covering the runtime and
installed overlay. The launcher validates those bytes even when `.git` is
removed. The immutable image/build remains the trust anchor; the manifest is
not a signature.

To export a container overlay, supply `--bundle-dir /path/to/context` and
`--base-image registry/omega@sha256:<digest>` to the installer. The generated
Dockerfile uses the verified upstream entrypoint with a narrow bridge-variable
allowlist and quoted environment forwarding. It checks the base's PeTTa
revision and Python dependencies, relocates plugin registrations to the image's
paths, and verifies the sealed overlay and patched launcher. Use an approved
Omega base image pinned by its full digest. The cache defaults to
`/PeTTa/repos/Omega/memory/persistent_atp/workflow.json` inside Omega's persistent
memory volume, which its sandbox permits the `nobody` agent account to write.
The sealed proxy assets are installed into `/opt/nginx/`; provider keys stay
with that proxy. Only the providers and channels you select need credentials.
Configure a receiver URL reachable from the container. A custom cache path
must also be writable under the configured filesystem policy.

The plugin is disabled by default. Set `OMEGA_persistentAtpEnabled=enabled`
and configure the same workspace alias in `OMEGA_persistentAtpWorkspace` and
`PERSISTENT_ATP_OMEGA_WORKSPACE`. Receiver credentials belong only in the trusted
environment. The reporting policy remains loaded while workflows are disabled.

`proof-create` accepts JSON containing `theorem`, `definitions`, `assumptions`,
`quantifier_domains`, `allowed_background`, `targets`, and `verification_policy`.
The complete specification is retained as a hashed artifact and included in
worker context. Lean source arguments are authorized artifact hashes;
export destinations are registered aliases.

Keep `PERSISTENT_ATP_OMEGA_STATE` across agent restarts. Prepared requests,
operation references, event cursors, and unacknowledged notifications are
reconciled with the receiver. This cache grants no proof or admission authority.
Heartbeat resumes waiting work after capacity returns and drains remaining
event pages after execution finishes. Uncertain delivery uses SDK request
lookup before any resubmission. `PERSISTENT_ATP_OMEGA_RETRY_LIMIT` bounds attempts;
`PERSISTENT_ATP_OMEGA_RETRY_BASE_SECONDS` sets exponential backoff, capped at
30 seconds. Exhausted or permanent errors stop automatic dispatch and preserve
the request for review. Stale-state errors include refreshed authoritative
status; they do not silently change a prepared request or theorem semantics.
`PERSISTENT_ATP_OMEGA_MAX_CYCLES` bounds coordinator cycle attempts per proof;
the receiver remains responsible for execution budgets and leases.

Run the reference and pinned-runtime checks without paid model calls:

```bash
bash scripts/omegaclaw-test.sh --tier reference
bash scripts/omegaclaw-test.sh --tier omega
```

The full container test builds from a locally pulled official image and uses a
local model stub, an isolated proxy port, a reference receiver, and an Omega
restart. It makes no paid provider calls:

```bash
export OMEGA_CONTAINER_BASE_IMAGE=singularitynet/omega@sha256:<digest>
bash scripts/omegaclaw-test.sh --tier container
```

Receiver configuration accepts `profile: reference`, `persistence`, or
`production`. Persistent profiles require the PostgreSQL artifact schema and
the native MORK library loaded with `scripts/with-mork.sh`. The persistence
profile retains explicitly labeled scripted research. Production allows
creation, status, audit, and export, and enables formal search when its
configured formal provider passes capability checks. Unavailable worker
actions are rejected. Both recover the MORK projection from the journal at startup.

```bash
scripts/with-mork.sh --install bash scripts/omegaclaw-test.sh --tier persistence
```

Set `ARTIFACT_TEST_DB_URL` to a dedicated test database for that check.
`OMEGA_JANUS_MODE=swipl` checks the official container's SWI-to-Python bridge;
`python` selects the host `janus_swi` arrangement. The default is `auto`.

HTTP forwards the shared cancellation, budget, and decision DTOs only when
the receiver advertises support. The formal provider integration supports
cancellation; budget amendment, decision responses, and frontier continuation
remain unsupported.

## Formal prover integration

The [formal v2 worked example](examples/formal_v2/README.md) connects trained
GNN/MCTS search to independently authorized replay and claim admission through
the existing journal and commit gate. It includes the lease-bound integration
pattern, native acceptance commands and links to independently checkable
mathematical proofs in the paired provider repository.

The Omega receiver uses that client with the paired provider revision pinned
in `pyproject.toml`. Configure `PERSISTENT_ATP_FORMAL_*` from `.env.example`,
`ARTIFACT_DB_URL`, and the native MORK library, then run:

```bash
uv run persistent-atp-omega doctor --profile formal-v2 --require
scripts/with-mork.sh uv run persistent-atp-omega serve --config formal
```

Formal search requires a committed Lean declaration with its original name,
type, source artifact, accepted alignment, and matching provider environment.
Trusted formalization and review must supply these; Omega cannot grant itself
alignment authority. The receiver compiles the leased context, uploads exact
source bytes, and persists the original request before search admission. It
imports settled evidence through the commit gate, requests independent replay
under a separate actor, and promotes the claim only after the gate accepts
replay and alignment evidence. Cancellation stays pending until backend work
stops. Restart reuses saved requests and receipts; an uncertain replay response
stops for reconciliation rather than issuing another replay automatically.

With the paired provider installed or checked out at
`PERSISTENT_ATP_FORMAL_SOURCE`, run the contract and recovery checks:

```bash
bash scripts/omegaclaw-test.sh --tier formal-contract
```

These checks use scripted search and, when installed, a fresh Lean process for
the controlled replay fixture. They do not establish trained GNN/MCTS readiness;
that requires the provider's pinned native environment and model assets.
