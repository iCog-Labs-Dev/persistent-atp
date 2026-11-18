# Formal v2: leased proof admission

This example guide connects the shared trained prover service to persistent-atp's
existing artifact store, journal, scheduler leases and commit gate.

The matching provider includes organized mathematical examples at
[`Mathematics-ai-atp/examples/formal_v2`](https://github.com/iCog-Labs-Dev/Mathematics-ai-atp/tree/formal-v2-release-integration/examples/formal_v2):

| Example | Content | Evidence status |
| --- | --- | --- |
| Lagrange's identity | Six-variable polynomial identity | Both trained models found `ring`; independently verified in Lean |
| Cauchy–Schwarz | Three-dimensional real inequality | Complete reference proof; model search is a challenge |
| Chinese remainder problem | Three congruences with a unique bounded solution | Complete reference proof; bounded MCTS search was unsuccessful in the recorded trial |

Reference proofs and model-discovered results are documented separately. A
reference proof body is never provided as a neural search hint.

## Architecture

```text
Scheduler-issued search lease
    │
    ├── FormalRuntimeConfig(protocol_version=2) / lease compiler
    │       └── source hash, declaration, environment, revision and fencing
    │
    ├── FormalV2HTTPATP → bounded GNN or MCTS search
    │       └── proved-pending-replay + stored certificate
    │
    ├── admit_search → existing artifact store and CommitGate
    │       └── replay-pending certificate; claim remains provisional
    │
Independent replayer lease
    ├── admit_replay → fresh native verification and bound receipt
    │       └── replay-accepted certificate; claim remains provisional
    │
Coordinator lease + reviewed alignment
    └── admit_promotion → CommitGate
            └── lean-verified claim only if all authority/binding checks pass
```

## Integration pattern

Use real leases issued by the application's scheduler/journal. `gate` and
`artifacts` below are the application's existing `CommitGate` and artifact store;
the helpers do not introduce another journal or fabricate worker identities.

```python
from mathproof.formal_v2 import FormalV2HTTPATP
from mathproof.formal_v2_admission import (
    admit_search,
    admit_replay,
    admit_promotion,
)

client = FormalV2HTTPATP("http://127.0.0.1:39743", timeout=90)

# dispatch comes from the lease compiler with protocol_version=2.
request = dispatch.worker_input
source_bytes = artifacts.get(request["input"]["artifact_hash"])
assert client.put_artifact(source_bytes) == request["input"]["artifact_hash"]
client.formal_search_start(request)

# Poll with the coordinator's own observation/deadline policy until settled.
# Persist client.binding(request["run_id"]) through the existing run record.
searched = admit_search(client, request["run_id"], search_lease, gate, artifacts)
assert searched.accepted, searched.rejections

# Resolve the Certificate from the committed PRODUCED_CERTIFICATE relation.
# Release the search lease; issue a distinct replayer lease for that certificate.
independent_client = FormalV2HTTPATP("http://127.0.0.1:39743", timeout=90)
checked = admit_replay(
    independent_client, replay_lease, gate, artifacts,
    certificate_id=certificate_id, max_steps=128, wall_clock_ms=60_000,
)
assert checked.accepted, checked.rejections

# Release the replayer lease; issue a coordinator lease for the aligned claim.
# A committed replay can also record rejection. Proceed only when the committed
# certificate is replay-accepted; admit_promotion checks this lifecycle state.
promoted = admit_promotion(
    coordinator_lease, gate,
    claim_id=request["claim_id"], certificate_id=certificate_id,
)
assert promoted.accepted, promoted.rejections
```

Before dispatch, the run must already bind its immutable producing actor and
environment; its `SEARCHES` relation and the declaration's source/environment
association must be present. An Environment may use `provider_environment_id`
for the provider's environment identifier while retaining its proof-scoped graph
ID. See the [full contract guide](../../schemas/FORMAL_V2.md).

These stages are intentionally not a single automatic promotion call. A verified
HTTP response is evidence for an independently authorized replayer; it is not
authority to modify the claim. Stale revisions/fences, changed source or
environment, incomplete obligations, missing alignment and self-verification
remain rejectable by the existing gate.

## Run native acceptance yourself

Install the matching provider and this client in the same test environment.
Stop any separately running model service first; the test launches its own
Uvicorn server. Set paths to provisioned, verified assets:

```bash
export MATHS_AI_MCTS_RL_CONFIG=/absolute/path/to/relocated/mcts-rl.json
export MATHS_AI_GNN_BUNDLE=/absolute/path/to/accepted-gnn-bundle
export MATHS_AI_FROZEN_PANTOGRAPH=/absolute/path/to/frozen/Pantograph
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2

python -m pytest --pyargs mathproof.tests.test_formal_v2_gate -vv
```

Both backend cases use actual trained models and Lean. The test creates real
journal leases and checks successful admission/promotion and negative cases for
revision, fencing, source binding, proof obligations, independent actors,
no-sorry evidence and alignment. The MCTS case uses real Uvicorn HTTP.

The native test uses an in-memory implementation of the existing artifact
`put/get` interface to remain self-contained. It does not claim PostgreSQL or
MORK integration coverage; those backends retain their own test suites. Model
startup and local native dependencies are not replaced by fixtures.

## Contract compatibility and recovery

This branch pins the provider schemas vendored in `schemas/formal-v2.schema.json`.
The client verifies every schema digest before submitting work. Coordinate
provider/client deployment when the contract changes; do not infer compatibility
from the numeric version alone.

The run binding and downloaded certificate/input/source evidence belong in the
existing coordinator store. Replay restores that binding before observing the
provider and verifying the stored certificate. An inconclusive replay cannot
promote or logically reject the claim. Lost-response retry/replay operations are
not automatically repeated with a new allowance.
