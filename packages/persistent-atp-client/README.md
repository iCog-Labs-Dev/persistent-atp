# persistent-atp-client

`persistent-atp-client` is the framework-independent Python interface to the persistent-ATP input service. OmegaClaw's nine proof skills use this package to turn string tool arguments into typed requests. The receiver owns admission, execution, artifacts and proof state; accepted proof changes still pass through `CommitGate`.

```text
Omega/PeTTa skill → Omega bridge → persistent_atp_client → ReceiverPort
                                          ↓ HTTP or trusted local adapter
                           persistent-ATP receiver → service → commit gate
```

The client has no database, Omega, or LangGraph dependency. Python 3.11 or later is required. The original nine actions retain contract `2.0`; coordinator reads and actions use capability-gated contract `2.1`.

## Install and start the reference receiver

From the persistent-atp repository root:

```bash
.venv/bin/python -m ensurepip --upgrade
UV_CACHE_DIR=tmp/uv-cache uv pip install -e .

export PERSISTENT_ATP_OMEGA_TOKEN='replace-with-a-secret-at-least-16-characters'
.venv/bin/python -m omegaclaw.cli serve --config src/omegaclaw/config/reference.yaml
```

The last command runs in the foreground on `127.0.0.1:8765` by default. In another terminal, set the same token and check the receiver:

```bash
export PERSISTENT_ATP_OMEGA_TOKEN='replace-with-a-secret-at-least-16-characters'
curl http://127.0.0.1:8765/health
curl -H "Authorization: Bearer $PERSISTENT_ATP_OMEGA_TOKEN" \
  http://127.0.0.1:8765/v2/capabilities
```

The reference receiver reports `receiver_mode=reference-scripted`, `backend_mode=unavailable`, and `independent_replay=false`. Its operation ledger and proof journal are durable SQLite files, but its graph view is in memory, its byte store is a labeled reference store, and its critic is scripted. This profile cannot certify a theorem through Lean.

Check coordinator capabilities separately:

```bash
curl -H "Authorization: Bearer $PERSISTENT_ATP_OMEGA_TOKEN" \
  'http://127.0.0.1:8765/v2/capabilities?contract_version=2.1'
```

## Use the same client outside OmegaClaw

Save the following as `tmp/client_example.py` and run it with `.venv/bin/python tmp/client_example.py` while the service is running. The example creates an exact target, observes one bounded cycle, and reads receiver status. It persists the prepared creation request before submission so rerunning after a lost response retains the same request identity.

```python
import asyncio
import json
import os
import uuid
from pathlib import Path

from persistent_atp_client import ActionRequest, AsyncClient, ProofCreate
from persistent_atp_client.adapters.http import HTTPReceiverAdapter

WORKSPACE = "reference"
PROOF_ID = "readme_demo"
CHECKPOINT = Path("tmp/readme-create-request.json")


async def main() -> None:
    port = HTTPReceiverAdapter(
        os.environ.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765"),
        os.environ["PERSISTENT_ATP_OMEGA_TOKEN"],
    )
    async with AsyncClient(port) as client:
        if CHECKPOINT.exists():
            request = ActionRequest.from_dict(json.loads(CHECKPOINT.read_text()))
        else:
            spec = {
                "proof_id": PROOF_ID,
                "title": "README demonstration",
                "literal_target": "∀ n : Nat, n = n",
            }
            artifact = await client.put_artifact(
                json.dumps(spec, ensure_ascii=False).encode("utf-8"),
                workspace_id=WORKSPACE,
                media_type="application/json",
            )
            request = client.prepare(
                ProofCreate(artifact),
                workspace_id=WORKSPACE,
                request_id="readme-create-" + uuid.uuid4().hex,
            )
            CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
            CHECKPOINT.write_text(json.dumps(request.to_dict(), ensure_ascii=False))

        created = await (await client.submit(request)).wait(timeout=10)
        print("creation:", created.lifecycle, created.project_revision)

        cycle = await client.proof_cycle(PROOF_ID, workspace_id=WORKSPACE)
        cycle_result = await cycle.wait(timeout=10)
        print("cycle:", cycle_result.mathematical_disposition)

        digest = await client.proof_status(PROOF_ID, workspace_id=WORKSPACE)
        print("target:", digest.literal_target)
        print("revision:", digest.revision, "formally verified:", digest.formal_verified)


asyncio.run(main())
```

Any coordinator can use the same client to inspect every committed node and edge of one proof, including the full scheduler frontier, and to queue typed speculative guidance. The receiver pins each page to a journal revision. A later commit invalidates a cursor; restart with a fresh first page. Guidance requires an expected revision and an intent key, so a retry cannot create duplicate moves. The receiver constructs graph operations and sends them through `CommitGate`.

```python
from persistent_atp_client import Preconditions, ResearchGuidance

page = await client.coordination_page(PROOF_ID, workspace_id=WORKSPACE, page_size=100)
items = list(page.items)
while page.next_cursor:
    page = await client.coordination_page(
        PROOF_ID, workspace_id=WORKSPACE, cursor=page.next_cursor, page_size=100
    )
    items.extend(page.items)

revision = page.revision
handle = await client.queue_research_moves(
    PROOF_ID, "audit-round-1",
    (ResearchGuidance("bridge-lemma", "Investigate an intermediate equality", f"{PROOF_ID}/c-1"),),
    workspace_id=WORKSPACE,
    preconditions=Preconditions(expected_revision=revision),
)
queued = await handle.wait(timeout=10)
cycle = await client.proof_cycle(PROOF_ID, workspace_id=WORKSPACE, category="research-move")
result = await cycle.wait(timeout=10)
print(queued.result["research_move_ids"], result.mathematical_disposition)
```

Persist a prepared `QueueResearchMoves` request with its request ID before submission when retry identity must survive a coordinator restart. Category values are `formal-run`, `research-move`, and `critic-task`. The selected cycle still uses the trusted scheduler and lease; a category with no available worker or backend returns an explicit blocked disposition.

For a later intentional creation, choose a new proof ID and a new checkpoint file. For a retry of the same intent, reuse the saved `ActionRequest`. On ambiguous delivery, `SubmissionUncertain.request` also retains that exact request. An operation can be reattached with `client.attach(OperationRef.from_dict(saved_ref))`; handles are observations and `wait()` timing out does not cancel server work.

## Connect OmegaClaw

The repo-local integration uses the pinned Omega and PeTTa checkouts under `tmp/repos/` by default. Check prerequisites and install into a **derived copy** of Omega, leaving the pinned source checkout intact:

```bash
.venv/bin/python -m omegaclaw.cli doctor --profile omega --require
cp -a tmp/repos/Omega tmp/repos/Omega-intermediate-demo
.venv/bin/python scripts/omegaclaw-install-omega.py \
  --omega-root tmp/repos/Omega-intermediate-demo

export PERSISTENT_ATP_OMEGA_URL=http://127.0.0.1:8765
export PERSISTENT_ATP_OMEGA_TOKEN='replace-with-a-secret-at-least-16-characters'
```

The installer registers the `persistent_atp` plugin in that Omega copy, installs the bridge and client package alongside it, and preserves other plugin catalogue entries. Start Omega from the derived copy using its normal runtime configuration. The bridge reads the two `PERSISTENT_ATP_OMEGA_*` variables from its host environment; they are not model-visible tool arguments.

| Omega skill | Typed client action |
| --- | --- |
| `proof-create(spec, workspace)` | Upload the JSON spec as an artifact, then `ProofCreate` |
| `proof-cycle(proof, workspace)` | `ProofCycle` |
| `proof-status(proof, workspace)` | Immediate `client.status()` read |
| `proof-audit(proof, workspace)` | `ProofAudit` |
| `proof-export(proof, destination, workspace)` | `ProofExport` with a registered destination alias |
| `formal-search(proof, claim, budget, workspace)` | `FormalSearch` with parsed bounded JSON budget |
| `formal-resume(proof, run, budget, workspace)` | `FormalResume` with parsed bounded JSON budget |
| `statement-align(proof, claim, lean_source, workspace)` | `StatementAlign` with a content-addressed Lean source artifact |
| `lemma-bridge(proof, obstruction, workspace)` | `LemmaBridge` |
| `coordination-page(proof, cursor, page_size, workspace)` | Read one pinned `CoordinationPage`; use an empty cursor for the first page |
| `proof-cycle-selected(proof, category, workspace)` | `SelectedProofCycle`, routed by the trusted scheduler |
| `queue-research-moves(proof, intent_key, moves, expected_revision, workspace)` | `QueueResearchMoves` with a JSON list of typed guidance |

For `statement-align`, upload the Lean source through a trusted facility first. The local CLI command is:

```bash
.venv/bin/python -m omegaclaw.cli artifact-put \
  --config src/omegaclaw/config/reference.yaml --file /path/to/statement.lean
```

It returns an `artifact_ref` hash to pass as `lean_source`. The bridge does not open model-selected files or URLs. The Omega bridge blocks formal search and resume when the receiver advertises an unavailable backend; direct SDK calls receive explicit blocked outcomes from the reference service. A model score or review never becomes formal proof.

## Verify the actual Omega path

```bash
.venv/bin/python -m pytest -q packages/persistent-atp-client/tests
.venv/bin/python -m pytest -q src/omegaclaw/tests/test_receiver_v2.py
.venv/bin/python -m pytest -q tests/omegaclaw_runtime/test_omega_process.py
```

The last test starts an actual pinned Omega/PeTTa process, loads the nine proof skills and three coordination skills, sends a tool call through the client, reference receiver, service and commit gate, then checks that the next model request contains the committed digest. It also queues guidance and selects its research frontier category through MeTTa. Its DeepSeek endpoint is a local mock; it is not a live model call. The test requires the pinned checkouts and `janus_swi`; it skips explicitly when those are absent. Check `.venv/bin/python -m omegaclaw.cli doctor --profile deepseek --require` before attempting a separate live DeepSeek run.

The receiver and client report operation lifecycle separately from mathematical disposition and `formal_verified`. `succeeded` means an operation returned a valid result; it does not mean that the literal target was proved. Production readiness additionally requires the real artifact/MORK path, a real ATP executor and independent Lean replay.
