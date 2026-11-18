# Formal ATP backends

## Current Mathematics-ai-atp v2 integration

Use `MathsAIHTTPFormalATP` for the consolidated strict v2 provider, together with
`FormalRuntimeConfig(protocol_version=2)` and the lease-bound helpers in
`mathproof.formal_v2_admission`. Its five-call boundary returns canonical results;
provider admission/status metadata is retained under the `provider` field.
Replay has an explicit separate allowance and frontier resume is unsupported.
See [`schemas/FORMAL_V2.md`](../../schemas/FORMAL_V2.md) for request compilation,
durable recovery and commit-gate admission.

The parallel public `FormalV2HTTPATP` has been removed. Contract helpers remain
in `mathproof.formal_v2`; there is one Mathematics-ai-atp HTTP adapter.

The coordinator uses the five methods on `FormalATPAdapter`. Its canonical
request and result formats remain the JSON schemas under `schemas/`; provider
APIs are translated at the adapter boundary.

There are two HTTP integration styles:

- `HTTPFormalATP` calls any provider that natively implements the canonical
  five-route persistent-atp JSON contract.
- A provider adapter translates a different API while still implementing the
  same Python boundary. `MathsAIHTTPFormalATP` is the first such adapter.

The coordinator depends on `FormalATPAdapter`; each service adapter validates
its wire contract before returning canonical graph evidence.

## Canonical HTTP provider

`HTTPFormalATP("https://provider.example/api")` expects:

| Route | Canonical response |
| --- | --- |
| `POST /formal-search/start` | `formal-search-result` |
| `POST /formal-search/{run_id}/resume` | `formal-search-result` |
| `GET /formal-search/{run_id}` | `formal-search-result` |
| `POST /formal-search/{run_id}/cancel` | Cancelled run acknowledgement |
| `POST /formal-replay` | Environment-bound replay verdict |

The adapter validates response schemas, identities, graph soundness, and the
pinned environment before returning provider data to the coordinator.

## Adding another provider

A provider with a different API implements the same five methods and keeps its
wire translation inside its own module:

```python
from mathproof import FormalATPAdapter

class OtherFormalATP(FormalATPAdapter):
    def formal_search_start(self, request): ...
    def formal_search_resume(self, run_id, budget=None): ...
    def formal_search_status(self, run_id): ...
    def formal_search_cancel(self, run_id): ...
    def formal_replay(self, certificate, environment_hash): ...
```

The implementation must return canonical schema-shaped results and fail
closed on identity, environment, graph, or replay mismatches. Provider route
names, authentication, polling, and native status vocabularies do not escape
the adapter.

## Mathematics-ai-atp protocol v2

Use `MathsAIHTTPFormalATP` with a Mathematics-ai-atp service implementing formal
protocol version 2.
This is a coordinated upgrade: the old maths-AI v1 wire format is refused.
The in-process legacy reasoner adapter does not implement this service contract.

The nine runtime schemas and consolidated provider manifest are vendored under
`mathproof/contracts/formal-v2/`:

- Upstream provider base: `947a98a30eb30b869009d130e5d7bf7fecbb7644`, with PR95's
  frozen GNN and optional default-selection extensions. The exact manifest
  fingerprint is `MANIFEST_HASH` in `mathproof.formal_v2`.
- The manifest and schema hashes are verified before validation. The manifest
  also lists upstream files that are intentionally omitted from the runtime.
- All 30 recorded examples are kept as separate files under
  `fixtures/formal_v2/examples/`; tests verify their original hashes and index.
  These scripted examples establish wire shapes, rather than native Lean or
  deployment readiness.
- [Resolution rules](https://github.com/Leul-Negash/Mathematics-ai-atp/blob/947a98a30eb30b869009d130e5d7bf7fecbb7644/contracts/formal-v2/rules.md)
  and [backend capabilities](https://github.com/Leul-Negash/Mathematics-ai-atp/blob/947a98a30eb30b869009d130e5d7bf7fecbb7644/contracts/formal-v2/capabilities.md)
  remain in the pinned upstream package.

### Configuration and requests

```python
from artifacts.store import ArtifactStore
from mathproof import maths_ai_http_atp
from commit_gate.gate import CommitGate
from context_compiler import FormalV2Selection

artifacts = ArtifactStore()  # ARTIFACT_DB_URL; apply artifacts.schema beforehand
atp = maths_ai_http_atp("http://localhost:8000", artifacts=artifacts)
gate = CommitGate(view, journal, artifacts=artifacts)

selection = FormalV2Selection(
    deployment_id="mcts-prod", backend="mcts-actor-critic", profile="puct",
    input_kind="source", provider_environment_id="mathlib-4.10-rc1",
)
# FormalRuntimeConfig(..., v2_selection=selection) produces dispatch.worker_input
# in v2 form, and the same request is covered by the packet digest.
# For a previously compiled payload:
request = payload.to_v2_schema_dict(
    deployment_id=selection.deployment_id, backend=selection.backend,
    profile=selection.profile, input_kind=selection.input_kind,
    provider_environment_id=selection.provider_environment_id,
)
result = atp.formal_search_start(request)
```

Select deployment/profile from `atp.formal_capabilities()`. The graph's pinned
`environment_hash` must already equal the deployment's published runtime hash.
`provider_environment_id` is the provider's name; the graph Environment node id
can remain a proof-scoped id. The adapter never replaces a pinned hash with a
new one from capabilities.

Source mode requires `lean_name` and a real `lean_source_artifact` on the selected
FormalDeclaration. Store its exact UTF-8 Lean bytes using `artifacts.put()`.
A hash of the declaration's graph record cannot substitute for source bytes.
The adapter uploads those bytes first, verifies the returned address and sends
`input={kind:source, artifact_hash, declaration, starting_state:binders-introduced}`.
A missing or rejected source never becomes a text request.

Goal mode explicitly sends `goal_text`, ordered `hypotheses` and `universe_levels`.
The compiler preserves hypotheses from the root state. MCTS supports both input
kinds and universe-polymorphic goals. `accepted-gnn` supports goal inputs without
universe levels. Runtime capabilities may restrict these choices further.

Only `budget.max_steps` is sent as a tactic allowance. An older compiler policy
limit is consolidated into this budget with `min`, and never sent as the removed
`search_policy.max_tactic_steps` field. Requested/admitted limits, cumulative
charges, exactness and remaining steps are retained in `result.provider`.

### Lifecycle and graph identity

Start validates capabilities, request identity, admission digest, deployment,
environment and admitted ceilings. Status/cancel/retry responses are validated
against the pinned schemas and the original binding. Lost start replies cause a
lookup; the client never starts duplicate work while admission is uncertain.
Identical repeated starts use the provider's idempotent start contract.

Poll `formal_search_status(run_id)` until disposition differs from `searching`.
A `cancelling` response stays `searching` until work actually stops.
`formal_search_resume(run_id)` requests the provider's retry for an eligible
terminal run, with its remaining allowance. A changed budget is refused.
The result explicitly reports `frontier_resume: false`; no MCTS/PUCT frontier
continuation is claimed and no synthetic checkpoint is produced.

Persist v2 run updates with the provider's acknowledged accounting:

```python
from mathproof.formal_run import formal_run_ops

ops = formal_run_ops(result, view.node(result["run_id"]), actor=worker_id)
```

Include these ops in the normal leased proposal alongside its search graph,
certificates and any required obstruction. An unchanged poll returns no run
ops. The helper stores `provider` metadata and an append-only `attempt_history`
on the FormalRun; history records cumulative consumed and remaining steps at
each finished attempt. It also handles a retry that finishes before its HTTP
response arrives.

The gate permits an eligible terminal attempt to return to `searching` only
with the next attempt number, remaining tactic allowance, unchanged admitted
input/environment/profile/budget, and preserved prior outcomes. It rejects
refunds and history rewrites even when another op restores the old value in
the same proposal. Proved runs, counterexamples, exhausted tactic allowances
and rejected inputs remain closed. A legacy terminal run without v2 accounting
cannot reopen merely by changing its status.

The provider scopes exported states and tactics to the run and attempt, for
example `proof/fr-1/attempt-2/fs-1`. The adapter checks that every id belongs
to the result's run and attempt before mapping it to a positive decimal id
derived from the run id, attempt and provider id.
This preserves the existing `proof/fs-N` and `proof/ta-N` grammar while avoiding
collisions between attempts. The original graph is retained in the binding
artifact; certificate bytes and hashes are copied unchanged.

After a client restart, call `restore_run(original_v2_request)` to reattach by
GET and verify its digest. The coordinator must retain that original request.
Replay can restart directly from the certificate's `binding_artifact_hash`.

### Durable evidence and replay

A proved result must pass the existing child completeness, executor, heuristic
separation and environment checks. Before returning it, the adapter downloads
all advertised artifacts, checks both their raw SHA-256 and `X-Content-Hash`,
and stores the exact bytes in the caller's artifact repository. It also checks
canonical JSON and the run/input/source/environment bindings of the certificate
and admitted-input manifest. A storage failure emits no candidate certificate.

The local binding artifact stores the original v2 request and validated provider
search snapshot. Mutable replay history is excluded from its hash; each replay
attestation is stored separately. This keeps the binding stable when a new
client restores the run after replay. Persist `binding_artifact_hash` alongside
the Certificate's `artifact_hash`, `producer_run_id` and `environment_hash` in
the journal proposal.
The binding field is immutable. Configure the same artifact repository on
CommitGate: it verifies certificate, binding, input and source bytes before
commit, and checks replay attestation availability when promoting a claim.
A v2 certificate with a binding requires a configured repository. Legacy
providers without bindings retain their prior behavior if no store is supplied.

Store artifacts before journalling references. These are separate databases:
a rejected proposal can leave an unused blob, which is safe to collect later;
a committed reference must always have its bytes. Graph rebuilding restores
references from the journal; the artifact database needs its own durable storage
and backup.

`formal_replay(certificate, environment_hash)` loads durable local evidence and
sends only the stored run, certificate/input/source hashes, pinned environment,
and a separate verification budget. No text or replacement trace is sent.
The provider independently rechecks its stored bytes in a fresh native session.
Every attested identity is checked before accepting the verdict; the complete
attestation is also saved as an artifact.

Before a verified v2 replay can support claim promotion, the gate requires
its stored attestation, validates the pinned schema, and matches its run,
certificate, admitted input, source, source origin, backend and environment
against the durable candidate binding. The candidate and admitted-input bytes
must also agree with that binding. Omitting the binding field does not turn a
v2 certificate into legacy evidence. These checks use the graph after all
proposed changes, including evidence submitted alongside promotion.

- `verified`: certificate becomes `replay-accepted` in the adapter result.
- `rejected`: certificate becomes `replay-rejected`.
- `inconclusive`: certificate stays `replay-pending`, with the reason retained.
  A changed or unavailable current environment may be reported here.

These are worker results, not graph writes. The replayer/coordinator still
constructs its normal leased proposal and must preserve
`attestation_artifact_hash` on the LeanReplay record. The gate's existing
independent-actor, alignment, declaration-chain, environment and fencing rules
still control promotion. An inconclusive replay cannot promote a claim.
HTTP errors remain typed service failures, rather than negative proof verdicts.

### Verification

`pytest src/mathproof/tests/test_maths_ai_http.py` checks the pinned responses,
actual local HTTP transport, source and goal inputs, both certificate formats,
all replay outcomes, corruption, binding failures, restart and retry accounting.
`src/commit_gate/tests/test_artifact_refs.py` commits an adapter certificate,
rebuilds its journal references and replays with a fresh client; it also checks
missing/corrupt evidence and promotion/fencing rejection.

`src/commit_gate/tests/test_v2_replay_promotion.py` submits adversarial proposals
with missing, malformed, swapped and jointly rewritten evidence; it checks
atomic rejection, cold projections, fencing and reconstruction. The retry
tests in `src/commit_gate/tests/test_formal_run_retry.py` cover refunds, modified
ceilings, hidden status cycles, history rewrites, duplicate/stale submissions,
ineligible outcomes and a second attempt completing before the retry reply.
`mork/tests/test_formal_v2_gate.py` repeats rejection/commit/reconstruction in
separate processes with the native library when configured. These use scripted
provider evidence and do not establish native Lean/model acceptance.

The PostgreSQL integration test now calls `CommitGate.commit()` and rebuilds
references before retrieving the original bytes. It requires a dedicated test
database via `ARTIFACT_TEST_DB_URL`. The normal suite skips it without that
setting. Production `ArtifactStore.get()` re-hashes stored bytes on every read.

With the protocol v2 service running and the artifact schema applied, run:

```bash
PYTHONPATH=src python scripts/formal-v2-smoke.py \
  --base-url http://localhost:8000 --request /path/to/v2-request.json
```

For source input, store the exact bytes first or supply `--source /path/to/file.lean`;
its content hash must match the request. The probe starts, polls and replays using
the service and stores evidence locally. It does not commit graph mutations.
Native Lean and deployment acceptance require this live service check before
coordinated rollout.
