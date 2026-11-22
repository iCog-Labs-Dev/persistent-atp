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

The loop overlay initializes its wake timer during startup, including when a
restored managed task suppresses the first model turn. Reinstallation upgrades
the exact previous coordinator overlay; unrelated source edits still fail
digest validation.

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

For standalone Omega capability tests with a real model, configure its provider
key in `.env` and run the playground. It starts an isolated container with the
ATP plugin disabled, retains a dedicated memory volume, and recreates the
container to check persistence:

```bash
uv run --extra omega-playground persistent-atp-omega-playground --list
uv run --extra omega-playground persistent-atp-omega-playground --build-image
```

`--cases shell,file_append,query` selects cases and their prerequisites;
`--provider ASIOne` uses `ASIONE_API_KEY`, `ASIONE_MODEL`, and
`ASIONE_BASE_URL` from `.env`; `--model` overrides the configured model.
The playground exercises WebSocket only and excludes other communication-channel cases.
Each run writes `.runs/omega-capabilities/<run>/pairs.json` as
`{"query": "actual response"}` and a `report.json` with tool, filesystem,
memory, and recovery checks. Unavailable or unexecuted cases have null responses
and explicit reasons. Logs and reports remain outside Git. Live model calls
use the configured provider; passing generation checks do not establish
mathematical verification.

For model-driven ATP acceptance, use a production receiver configuration with
`formal_provider: true`, a compatible formal provider, PostgreSQL artifacts, and
preloaded native MORK. The runner owns the receiver HTTP service and recreates
it in the host process against the same durable storage during recovery checks:

```bash
scripts/with-mork.sh .venv/bin/python -m omegaclaw.playground \
  --atp --atp-config formal --atp-input /absolute/path/to/reviewed-input.json \
  --build-image --provider DeepSeek --output .runs/omega-atp
```

The trusted host input contains `spec` (the complete theorem specification),
`declaration` (`source`, `lean_name`, `lean_type`, `toolchain`), and `review`.
The source path is relative to the input file. The review must include
`reviewer`, `rationale`, `verdict: "aligned"`, `claim_text`, `lean_name`,
`lean_type`, `toolchain`, `source_hash`, `spec_hash`, and `environment_hash`.
Hashes use `sha256:<hex>`; `spec_hash` binds the canonical JSON specification.
The review must match every supplied value and the actual provider environment.
This file authorizes host-side declaration intake; it is never a model tool.

Omega calls creation, formal search, status, audit, and export through its real
tools. Acceptance independently checks native replay receipts, journal hashes,
artifact bytes, MORK projection, and recovery after Omega container and receiver
service recreation. Each run saves actual query/response pairs and the verified export.
This mode tests a prepared declaration.

Add `--atp-cycle` to exercise Omega's `proof-cycle` tool instead of explicit
`formal-search`. The receiver scheduler selects an aligned declaration and
uses the same provider, independent replay, and commit-gate admission path.
Cycle execution bounds come from the trusted `PERSISTENT_ATP_FORMAL_*`
configuration. An unformalized theorem reports blocked; unavailable research
and critic workers do not trigger scripted fallback. Existing provider runs
remain associated with their original operation and require reconciliation
rather than an implicit new search.

For automatic intake, set `PERSISTENT_ATP_INTAKE_PROVIDER` to `DeepSeek` or
`ASIOne`, and set `PERSISTENT_ATP_INTAKE_TOOLCHAIN` and
`PERSISTENT_ATP_INTAKE_IMPORTS` to match the configured formal deployment.
The receiver uses the corresponding provider credentials from `.env`.
On an unformalized proof, `proof-cycle` compiles the complete committed theorem
context, runs the existing formalizer, commits a draft, and separately reviews
exact statement alignment. Only an accepted review makes the declaration
available to formal search. Ambiguous reviews remain pending; alignment alone
does not verify a proof.

When a candidate supplies a nonempty proposition but omits its declaration
name, or returns a null or blank name, intake assigns a stable name derived
from the declaration ID. It saves the effective candidate and naming origin
before generating source or requesting alignment review, and retains the raw
model response. Supplied names still undergo strict validation. Missing
propositions and explicit refusals continue to require clarification; completed
clarification outcomes are preserved when an operation is retried.

Add `--atp-intake` alongside `--atp --atp-cycle` to test this path. Its input
file contains only `{"spec": <complete theorem specification>}`. The runner
supplies no declaration or host alignment review. Initial intake supports one
proposition with trusted imports; custom universe declarations and additional
source commands require explicit preparation. Saved draft/review output is
reused after interruption; expired leases require reconciliation.

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
creation, status, audit, and export, and enables formal search and formal cycles when its
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

## Durable coordinator tasks

The pinned WebSocket profile accepts an explicit task envelope alongside the
ordinary message text. This keeps task identity and execution limits out of
model memory. For example:

```json
{
  "type": "user_message",
  "seq": 1,
  "text": "Prove this target and report its evidence.",
  "persistent_atp_task": {
    "spec": {
      "theorem": "True",
      "definitions": {},
      "assumptions": [],
      "quantifier_domains": {},
      "allowed_background": [],
      "targets": ["True"],
      "verification_policy": {"independent_lean_replay": true}
    },
    "workspace": "reference",
    "max_cycles": 2,
    "max_research": 1,
    "timeout_seconds": 240,
    "reply_prefix": "MY_TASK_FINAL"
  }
}
```

Text-only clients can send `PERSISTENT_ATP_TASK ` followed by the task object's
JSON. Ordinary free-form messages retain Omega's existing tool workflow; their
prose is not silently interpreted as permission to increase execution limits.

Task intake is persisted before the channel consumes the message. The server
must preserve its URL/session namespace and monotonically increasing `seq`
values across reconnects and restarts. The channel credential fingerprint also
binds the task's destination. Replaying the same message recovers the same task;
reusing its identity with changed task fields is rejected. A distinct sequence
number creates a distinct task, even for an identical theorem.

To continue an existing proof, send a new managed task with the same complete
`spec`, a `proof_id`, `expected_revision`, and `expected_theorem_hash` from
authoritative proof status, and explicit `max_cycles` and `max_research`
allowances. The theorem hash is `target_artifact.hash`, not the journal hash.
For example, add these fields to the task object above (using actual status):

```json
{
  "proof_id": "the-existing-proof-id",
  "expected_revision": 7,
  "expected_theorem_hash": "sha256:<actual target artifact digest>",
  "max_cycles": 1,
  "max_research": 0
}
```

Attachment retrieves and checks the full committed specification, reconciles
active operations and unresolved mathematical requests, and atomically claims
workflow ownership. It creates no new proof and does not modify the earlier
task or replenish an existing provider run. A changed revision or target
requires a new reviewed intent. A finished task does not imply that its backend
stopped; conflicting work must settle or be reconciled first. Independent
proof tasks can progress concurrently within one coordinator process. Status,
audit and export remain accessible after a managed task finishes; mathematical
follow-up work requires a new managed task with its own limits.
The final response distinguishes the proof's observed verification status from
verification of an attachment that has not passed its exact-specification checks.

Handoff checks and mathematical dispatch share a proof-scoped lock, including
when the same cache is reopened. A durable `proof_owners` record remains after
reporting or delivery acknowledgment. Ownership transfers only after fresh
receiver observations establish that earlier work settled; an unrelated proof
can continue while these checks wait on I/O. This assumes one coordinator and
its retained cache for a workspace, rather than independent competing caches.
Reporting, attachment and mathematical dispatch use the same settlement check.
A `succeeded` or `cancelled` lifecycle with missing, `null` or `false`
`execution_settled` does not permit ownership transfer or another mathematical
operation. Dispatch checks fresh observations again after attachment, including
after restart. Unsettled results cannot trigger follow-up research; status,
audit and export remain available.

Terminal formal searches with unresolved execution have an explicit trusted
recovery path. This includes failed searches and legacy succeeded/cancelled
operations without settlement evidence. With the receiver running, inspect the
original operation's current observation version, then run:

```bash
.venv/bin/python -m omegaclaw.cli reconcile-search --config formal \
  --operation-id <receiver-operation-id> \
  --expected-observation-version <current-version> \
  --request-id <stable-reconciliation-control-id>
```

The capability-gated SDK equivalent is
`handle.reconcile_search(SearchReconciliationRequest(control_id, handle.ref, version))`.
It performs GET observations of the original provider run and validates the
original request, environment and result artifacts. New evidence still needs
the original valid lease and fencing authority at the commit gate. A successful
reconciliation appends a separate content-addressed execution receipt to the
receiver's operational ledger and advances the operation's observation version.
The original lifecycle, result, error and request remain intact. The coordinator
checks that receipt before permitting a new, explicitly pinned continuation.
The original service job must also be terminal. Already confirmed settled
successes need no reconciliation job. Terminal status alone does not establish
that the underlying formal workflow is search-only: replay phases remain
blocked by this control, regardless of their receiver lifecycle.

Provider HTTP exchanges have a total monotonic deadline covering DNS,
connection setup, headers and complete response bodies, including errors and
binary artifacts. Each exchange runs in a disposable standard-library helper
process. Expiry kills and reaps that process, releasing local network resources;
request credentials pass through stdin, not process arguments. An expired POST
can still have reached the provider. Its original identity remains necessary
for search reconciliation, and a lost replay receipt remains unresolved.

General capability and submission handlers read a provider/configuration-scoped
readiness cache instead of contacting the provider. One background worker
refreshes it using a five-second HTTP deadline; cached observations expire after
five seconds and are not reused on restart. Unknown or stale readiness defers
new formal admission with a retryable error. Status, audit, export and recovery
stay accessible. Readiness schedules work; it never establishes proof truth.

Reconciliation runs as a durable background job, with two workers and at most
32 queued or running jobs. Admission and control-scoped capability negotiation
perform no provider reads on the HTTP handler. Status, other proofs and job
polling remain accessible while provider observation waits. A job retains its
original control ID, body and 120-second observation deadline through restart;
at most three worker attempts are allowed across restarts. New reads and evidence
admission stop when that deadline expires. An already persisted completion
receipt finishes the job without another provider read.

The SDK call waits by polling the job, with a default 60-second client timeout.
Use `wait=False` or the CLI's `--no-wait` to return its `SearchReconciliationJob`
immediately; the CLI's `--timeout` controls client observation time. After timeout
or cancellation, observe the same job with
`handle.wait_search_reconciliation(control_id, timeout=60)`, or repeat the exact
original control to reattach. Neither action creates another job or refreshes
its allowance. A terminal `blocked` job retains its diagnostic; another provider
observation needs a new control ID and the current operation version. The typed
job is also available at `GET /v2/reconciliations/{workspace}/{control_id}`.

This control starts no search, provider retry or independent replay. Active or
missing runs, changed bindings, expired authority and uncertain replay attempts
remain blocked. The pinned provider exposes only replay verdict summaries, so a
lost full replay receipt cannot be recovered through this search-only control.
Reconciled search certificates remain pending independent replay and do not
establish `formal_verified`. The CLI uses the live receiver through the public
SDK; it does not open another service against the workspace database.

Heartbeat workers drive creation, one automatic proof cycle, an optional
lemma-bridge against an actual returned obstruction, status, audit, export and
final reporting through the existing SDK. They continue with zero model turns.
Managed input suspends model polling; a fresh ordinary message restores the
existing agent workflow.
The cycle includes configured intake, alignment, search and independent replay;
verification remains a receiver/gate decision. Cycle allowances are ceilings:
an exhausted search does not justify an identical second search. Candidate
selection, refinement and true frontier continuation require further explicit
work; speculative research does not verify a lemma or its parent.

The version-2 workflow store preserves task/step identities, prepared requests,
operation references, limits and final responses across process/container
restart. Existing version-1 request/cursor/outbox records migrate without
changing their identities. Retain the configured memory volume. Complete loss
of task storage cannot reconstruct the user's reporting contract from active
receiver operations alone.

The task deadline is bound into immutable SDK requests and rechecked at receiver
admission after validation/lock waits. Expiry stops new mathematical admission,
not already admitted service work; lookup still recovers original admissions.
Finalization has a separate deadline 30 seconds after mathematical work ends,
or after the task deadline, whichever occurs first. Its requests use that fixed
deadline across retries. Prepared legacy requests without a bound deadline are
looked up rather than silently rewritten or resubmitted. The
cutoffs govern new admission; an in-flight read may finish at its transport
timeout. The
response records unresolved operations, unavailable evidence and mismatched
audit/export revisions explicitly. Large target previews are labeled incomplete
and retain the target artifact pointer.

Final responses use a persisted `client_seq` and body across at most four
delivery attempts. Socket writes remain `delivery-uncertain` until the server
returns an acknowledgment. Each reply is claimed immediately before its send;
a failed write leaves later, unsent replies' retry allowances untouched. A flush
attempts at most four replies. The acknowledgment frame is
`{"type":"ack","client_seq":"<same ID>"}`. The server must deduplicate
that ID; an ACK confirms server acceptance, not that a human read the message.
Unacknowledged responses remain available in workflow state after automatic
retries stop. Other communication channels do not acquire this guarantee.

Final reports preserve original error codes, bounded diagnostics, run IDs and
operation references. `operation_execution` records each operation's lifecycle,
observed version and separate `execution_settled` value: `true` for confirmed
settlement, `false` for observed active work, and `null` when settlement is
unknown. `execution_may_continue` remains true for active or unknown execution.
A failed operation or a legacy response without settlement evidence cannot
establish that remote work stopped. A validated reconciliation receipt can
establish settlement while preserving the original failure and mathematical
status. Uncertain failures do not trigger a further research operation.
An uncertain creation with a consistent receiver-returned proof ID retains that
ID and collects status, audit and export within the original finalization
window. It starts no cycle or replacement creation. Proof IDs must agree with
the original request/operation binding; missing or conflicting IDs remain
unresolved. A cached, validated ID is retained even after the evidence window
expires, with unavailable evidence reported explicitly. Pending drafts from an
older coordinator can recover that correlation without extending their deadline.
Already persisted final messages retain their original ID and body; later
reconciliation is observed through current receiver state.

Run the required coordinator tier with the provisioned pinned Omega/PeTTa
runtime and Janus bridge:

```bash
bash scripts/omegaclaw-test.sh --tier coordinator
```

Missing runtime prerequisites fail this tier. CI runs the same task and actual
MeTTa-loop checks inside a digest-pinned official Omega image, with scripted
receiver/model fixtures and no paid inference. The tests include zero-turn
completion, fresh-process recovery of active, delivery-pending and completed
managed tasks without model calls, intake metadata and recovery checks without
the optional formal provider, lost admission replies and real WebSocket reconnection
after a lost final acknowledgment. Live trained-search acceptance remains a
separate check.
