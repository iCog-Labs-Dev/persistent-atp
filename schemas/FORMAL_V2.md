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

## Remaining integration gates

This client does not synthesize an independent replay actor, alignment, lease,
or claim promotion. Commit-gate ingestion still needs independently authorized
replay records, original declaration/environment association, collision-safe
graph identifiers, and current revision/fencing checks. Search occurrence hashes
must not be interpreted as native exact-state serializations for transpositions.
Provider receipts do not yet include an explicit `sorry_detected` field; do not
invent that evidence when constructing gate proposals.

The optional provider-router integration test uses the real HTTP router,
scheduler and worker process with a scripted backend. It checks interoperability,
not trained-model quality or native Lean proof acceptance. Native both-backend,
packaged deployment and full gate acceptance remain separate checks.
