# Formal protocol v2: one adapter and one contract package

The public Mathematics-ai-atp adapter is **`MathsAIHTTPFormalATP`**, also exposed
through `maths_ai_http_atp`. The parallel `FormalV2HTTPATP` implementation has
been removed. `mathproof.formal_v2` now contains only contract/serialization and
artifact-interface helpers.

The authoritative provider package is `contracts/formal-v2`, based on PR94's
durable-attempt work at `947a98a` with the frozen GNN/default-selection extensions
in provider PR95. Its nine runtime schemas and manifest are vendored under
`src/mathproof/contracts/formal-v2/`. `MANIFEST_HASH` pins exact package bytes;
capability schema hashes must match before work is submitted.

## Existing adapter boundary

```python
from mathproof import MathsAIHTTPFormalATP

client = MathsAIHTTPFormalATP(endpoint, artifacts=artifact_store, timeout=90)
result = client.formal_search_start(request)
result = client.formal_search_status(request["run_id"])
```

These methods retain canonical `FormalATPAdapter` result shapes. Active runs
return `disposition: searching`; provider admission/accounting is retained under
`result["provider"]`. `client.result(run_id)` returns `None` while active, or
settled canonical evidence. `run_evidence(run_id)` supplies the validated raw
snapshot and its canonical projection together when leased admission needs both.

The caller supplies the existing durable artifact repository. Source uploads
resolve local bytes; certificates, admitted inputs, source and advertised
evidence are downloaded, re-hashed and persisted before a certificate is returned.
`CommitGate(view, journal, artifacts=artifact_store)` rechecks stored references
at commit time. The same store must be provided to the adapter and gate.

## Request compilation

Use `FormalRuntimeConfig(protocol_version=2)` for provider-default selection, or
the explicit `FormalV2Selection` configuration introduced by PR84. Both paths
delegate to the same v2 request builder; conflicting selections are rejected.
Backend/profile/deployment selection is optional and bound once at admission.

Source mode requires actual raw UTF-8 Lean bytes, the original `lean_name`, and
`starting_state: binders-introduced`. A declaration-record hash is not a source
artifact. Goal input requires its ordered hypotheses and universe declarations;
source failure is never silently converted into goal input.

The wire request is included in the context packet digest. The old local policy
step cap is folded into the single `budget.max_steps` field. Environment graph
nodes may supply `provider_environment_id` separately from their proof-scoped ID.

## Evidence, independent replay and promotion

```python
from mathproof.formal_v2_admission import admit_search, admit_replay, admit_promotion

searched = admit_search(client, run_id, search_lease, gate, artifact_store)
# The coordinator releases the search lease and issues an independent replayer lease.
checked = admit_replay(independent_client, replay_lease, gate, artifact_store,
    certificate_id=certificate_id, max_steps=128, wall_clock_ms=60_000)
# A later coordinator lease may promote only independently replayed, aligned evidence.
promoted = admit_promotion(coordinator_lease, gate,
    claim_id=claim_id, certificate_id=certificate_id)
```

The committed run must bind its producing actor and pinned environment before
dispatch, uniquely search the selected declaration, and retain original source
association. Source/input/request digests and replay bindings are immutable on
their evidence records. Reviewed alignment, live revision/fencing and independent
actors remain commit-gate requirements; search/replay alone do not promote a claim.

`formal_replay(certificate, environment_hash)` retains the canonical verdict shape
and stores its full attestation by hash. `replay_attestation(..., max_steps=...,
wall_clock_ms=...)` exposes the same checked full receipt to a separately leased
replayer. Explicit native `verification_cost.sorry_detected: false` is required
for successful v2 replay admission. Inconclusive verification is neither proof
rejection nor promotion.

Provider IDs include run and physical attempt. The adapter maps them once to
collision-safe persistent graph IDs. Their occurrence hashes are not native
state serializations and do not authorize transpositions.

## Restart and bounded retry

The adapter stores a canonical binding artifact with every candidate certificate.
A new client can replay that certificate using the existing artifact repository;
`restore_run(original_request)` also reattaches observation after restart. No
additional journal is created.

`formal_search_retry` explicitly restarts bound initial input with remaining
allowance. The legacy `formal_search_resume` call retains bounded retry semantics
and always advertises `frontier_resume: false`; it does not resume a saved frontier.
Retry/replay POSTs are not automatically reissued after a lost transport response.
The provider accounts for physical attempts and cumulative time across retries.
Use a fresh formal-service state directory when moving from the older store
contract, preserving its previous records for inspection.

## Updating the package

After regenerating the provider contract, run:

```bash
python scripts/sync-formal-v2-contract.py --provider /path/to/provider
```

The script verifies the provider manifest, copies runtime schemas and recorded
fixtures, and prints the new manifest digest to pin in `mathproof.formal_v2`.
Run contract/adapter/gate tests and both trained native acceptance cases before
deploying the pair. The native gate test uses real models, Lean, journal leases
and the commit gate, with a self-contained implementation of the artifact-store
interface; database/MORK integration remains separately testable.
