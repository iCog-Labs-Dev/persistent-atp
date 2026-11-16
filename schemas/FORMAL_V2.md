# Formal protocol v2

Provider authority: `maths_ai/formal/contract.py`, Mathematics-ai-atp branch
`formal-v2-release-integration`, commit `1f5a339` (PR94 base `516b8a1`).
`formal-v2.schema.json` vendors its seven published schemas with deduplicated
definitions. Each original schema is reconstructed and its canonical SHA-256
checked when the client loads. `/formal-capabilities.schema_hashes` must match
before work is submitted. This replaces the earlier provisional issue-93
`caba333d…` contract; that incompatible protocol is not negotiated as v2.

## Coordinator dispatch

Set `FormalRuntimeConfig(protocol_version=2, ...)` to compile a real scheduler
lease into a v2 packet. Backend/profile and deployment are optional; provider
resolves its default once at admission. An explicit `backend` mapping and
`deployment_id` override that choice. Environment identity remains pinned.

The compiler defaults to a source input identifying `lean_source_artifact` and
`lean_name`, with `starting_state: binders-introduced`. The source artifact must
contain raw UTF-8 Lean, not the old JSON declaration package. Upload exact bytes
through `client.put_artifact(source_bytes)` before starting. Do not use the
declaration metadata artifact as a substitute for the Lean file.

`FormalRequestPayload.to_schema_dict(protocol_version=2, formal_input=...)`
also supports explicit goal inputs. Ordered hypotheses and universe levels are
required; the pretty-printed root alone is not a faithful request. The compile
entry point accepts the same options and hashes the selected wire request into
its packet. The old local policy step cap is folded into `budget.max_steps`;
`search_policy.max_tactic_steps` is never sent on v2.

## Lifecycle and evidence

```python
client = FormalV2HTTPATP(endpoint, timeout=60)
ack = client.formal_search_start(dispatch.worker_input)
# Journal this alongside the existing coordinator lease/run record.
binding = client.binding(ack["run_id"])

# After coordinator restart:
client.restore(binding)
snapshot = client.formal_search_status(ack["run_id"])
result = client.result(ack["run_id"])  # None while active
```

Lifecycle calls return admissions/snapshots, not legacy result-shaped polling
responses. `result()` returns the settled canonical evidence envelope. It checks
coordinator revision/lease/fencing, cumulative allowance, pinned deployment,
source/input artifacts, certificate bindings, the existing canonical graph
schema, and complete AND/OR obligations. Search success stays
`proved-pending-replay`, certificate `replay-pending`.

Replay is a synchronous operation with a separate explicit allowance:

```python
receipt = client.formal_replay(
    result["certificate"], result["environment_hash"],
    max_steps=100, wall_clock_ms=30_000,
)
```

Retain the full receipt. It binds run, certificate, input, source, actual
environment and backend. Transport failure is not logical rejection and does
not prove remote work stopped. The client does not automatically repeat replay
or retry POSTs: current provider has no operation/replay idempotency key.
Reconcile run status and recorded replay summaries before authorizing more work.
Use a transport timeout longer than the replay wall-clock allowance.

`formal_search_cancel` is asynchronous; poll until terminal. `formal_search_retry`
restarts bound initial input with remaining admitted steps. Frontier resume is
unsupported. Durable snapshots and receipts belong in the existing coordinator
journal, not a new client store.

## Commit-gate admission

`mathproof.formal_v2_admission` implements the existing journal/gate path:

```python
from mathproof.formal_v2_admission import admit_search, admit_replay, admit_promotion

searched = admit_search(client, run_id, search_lease, gate, artifacts)
# The coordinator releases the search lease and issues a separate replayer lease.
checked = admit_replay(independent_client, replay_lease, gate, artifacts,
    certificate_id=certificate_id, max_steps=100, wall_clock_ms=30_000)
# A later coordinator lease may request promotion after reviewed alignment.
promoted = admit_promotion(coordinator_lease, gate,
    claim_id=claim_id, certificate_id=certificate_id)
```

These return the gate's normal `CommitResult`; callers must inspect `accepted`
and handle stale/rejected proposals through their scheduler. They reuse
`LeaseSnapshotProvider` and the existing artifact store's `put/get` interface.
No second journal or alternate promotion authority is introduced.

Before dispatch, the committed `FormalRun` must carry its producing worker's
`actor` and pinned `environment_hash`, and uniquely `SEARCHES` the selected
declaration. The declaration must bind the original `lean_name` and raw
`lean_source_artifact`, with one `PINNED_ENVIRONMENT`. These identities cannot
be filled by overwriting immutable fields after search.

An Environment graph node may carry `provider_environment_id` (for example,
`mathlib-4.10-rc1`) separately from its proof-scoped node ID (`p1/env-1`). The v2
request compiler uses that provider ID on the wire while retaining the graph ID
for evidence associations.

Search ingestion validates full obligations, stores evidence/bindings through
the coordinator artifact store, allocates collision-safe run/attempt graph IDs,
and creates a replay-pending certificate. The replayer restores that binding and
calls fresh verification itself under its own lease. Explicit no-sorry evidence
comes from `receipt.verification_cost.sorry_detected`; a verified HTTP status
without that evidence cannot be admitted as successful replay. Inconclusive
verification is neither promotion nor logical rejection.

V2 gate checks bind certificate/replay request, input and source digests to the
producing run and current declaration. Claim promotion still requires reviewed
alignment, independent producer/replayer/coordinator identities, current revision
and lease fencing. Search and replay alone do not promote the claim.

Goal-mode gate ingestion additionally requires a formalizer-bound generated
source artifact and matching complete `goal_input` on the declaration. It cannot
substitute a text goal for a previously committed source declaration.

Search occurrence hashes are marked `run-occurrence` and not transposition
eligible; they are not native exact-state serializations. Existing graph state
IDs are never merged on pretty-printed goal text or semantic signatures.

The provider-router contract test uses a scripted backend. Separate native
acceptance tests use both published trained backends, real Lean, independent
replay and real journal/gate leases. The MCTS case uses actual Uvicorn HTTP;
the frozen case also tests installed-wheel execution. CPU/GPU shared-provider
acceptance is recorded in the provider's `formal_release_integration.md`.
