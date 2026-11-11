# persistent-atp-client

This integration checkout currently serves receiver contracts `2.0` and `2.1`.
Client contract `2.2` types remain available, but the receiver does not advertise
atomic coordinator escalations. See [the receiver guide](../../src/omegaclaw/README.md).

`persistent-atp-client` is the framework-independent Python interface to the persistent-ATP input service. OmegaClaw's nine proof skills use this package to turn string tool arguments into typed requests. The receiver owns admission, execution, artifacts and proof state; accepted proof changes still pass through `CommitGate`.

```text
Omega/PeTTa skill → Omega bridge → persistent_atp_client → ReceiverPort
                                          ↓ HTTP or trusted local adapter
                           persistent-ATP receiver → service → commit gate
```

The core client has no runtime, database, Omega, or LangGraph dependency. Python 3.11 or later is required. HTTP transport uses the optional `[http]` extra (`httpx`). The package version is `0.3.0`; the original nine actions retain contract `2.0`, existing coordination actions retain `2.1`, and atomic coordinator batches opt into contract `2.2`.

Contract `2.2` adds `CoordinatorEscalation` and `QueueCoordinatorEscalations`:
one to 100 items in one receiver-owned gate proposal. Each item retains its
rule, target, optional claim link and detail. The request also carries a stable
intent key, three committed progress counts, a round number and a mandatory
expected revision. The receiver checks the basis against committed state and
deduplicates existing policy identities across coordinator restarts.

The client requires the `atomic-coordinator-escalations` capability before
submitting this action. Selected cycles prepared with
`contract_version="2.2"` require both an expected revision and the
`cycle-execution-revision` capability. Older receivers fail explicitly; the
client does not split a batch, encode metadata in prose or downgrade contracts.
Contract `2.0` and `2.1` request bytes and default method behavior are unchanged.

Receivers must apply semantic validation after structural JSON Schema checks,
using `request_from_json` or equivalent checks in another implementation.
Escalation detail must be nonblank and no larger than 4096 UTF-8 bytes; JSON
Schema's `maxLength` counts characters and cannot enforce this byte limit.
Proof ownership and duplicate policy identities also require semantic checks.

```python
from persistent_atp_client import CoordinatorEscalation, Preconditions

handle = await client.queue_coordinator_escalations(
    "existing-proof", "run-001-round-2",
    (CoordinatorEscalation("repair", "existing-proof/at-1", "Repair the base case",
                           claim_id="existing-proof/c-1"),),
    basis=(0, 0, 0), round_number=2, workspace_id="reference",
    preconditions=Preconditions(expected_revision=7),
    request_id="run-001-round-2-escalations",
)
operation = await handle.wait(timeout=10)
```

Use actual committed basis/revision values from a complete coordination read.
Persist prepared requests and operation references across ambiguous delivery.
Operation completion remains distinct from formal verification. The reference
receiver uses the existing proof-wide escalation lease, which may supersede
active dispatch leases; this action does not introduce a non-preempting queue.

The receiver and Omega commands below require `feat/omegaclaw-integration`; this branch supplies the standalone library and its contract tests.

## Install and start the reference receiver

From the persistent-atp repository root:

```bash
.venv/bin/python -m ensurepip --upgrade
UV_CACHE_DIR=tmp/uv-cache uv pip install -e 'packages/persistent-atp-client[http]'
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

## Client reliability and migration from 0.1

Install `persistent-atp-client[http]` wherever `HTTPReceiverAdapter` runs, including an external plugin's Python environment. A source copy of the client alone does not install the HTTP extra. Missing `httpx` raises an explicit installation error; there is no synchronous transport fallback. Local/test adapters and offline request preparation remain dependency-free.

The HTTP adapter now performs cooperative asynchronous I/O, with a total per-request timeout covering connection and response-body consumption. Each HTTP request closes its own connections, allowing `SyncClient` reuse across separate event loops. This sacrifices persistent connection pooling. Environment proxy settings and redirects are disabled; embedded URL credentials are rejected. Credentials remain in authorization headers and are not included in response diagnostics.

`handle.wait(timeout=...)` stops observation at the caller deadline without requesting remote cancellation. A prepared request's optional absolute `deadline` also bounds capability negotiation, submission and reconciliation together, using a local monotonic budget. Expiry before submission raises `StalePrecondition`; expiry after delivery may raise `SubmissionUncertain`, retaining the original request. Do not create a new request ID in response to uncertainty. Reconcile or resubmit the saved request deliberately. A trusted local port must itself be cooperative; the SDK cannot interrupt synchronous blocking service code.

Malformed acknowledgments, invalid admission identities and transport failures trigger lookup under the same request identity. A successful authoritative lookup may recover an operation. A failed or malformed lookup remains uncertain, preserving the prepared request rather than manufacturing a new operation.

Read responses are validated for DTO type, requested workspace/proof identity, and requested revision where applicable. Capability flags require actual booleans, and response metadata and nested references are checked. Malformed or inconsistent responses raise `ReceiverProtocolError` from `persistent_atp_client.errors` (also a `ValueError` for compatibility). HTTP JSON rejects duplicate keys and nonfinite values. Local implementations of `ReceiverPort.read_status` must return `ProjectDigest`, rather than an unchecked dictionary.

Event streams deduplicate notifications, verify sequence/scope, wait between empty or duplicate-only pages, and finish after observing a terminal operation with no new events. Retaining a cursor on an empty page no longer causes a busy loop. Cancelling a consumer leaves receiver execution untouched.

Artifact transfer remains whole-byte and is limited to 16 MiB before base64 encoding. HTTP responses have a 24 MiB decoded-envelope limit, accommodating a maximum-size artifact's JSON/base64 representation. The receiver can impose a smaller request limit; negotiated limits, byte streaming and server retention guarantees are not implemented here. HTTP controls remain explicitly unsupported. This release changes client behavior and installation requirements, not the mathematical authority of the receiver.

## SDK-only verification

```bash
UV_CACHE_DIR=tmp/uv-cache uv pip install -e 'packages/persistent-atp-client[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  packages/persistent-atp-client/tests
```

HTTP tests require the `[http]` or `[dev]` extra. They exercise the real async adapter/codec with scripted transport faults; these tests do not establish proof or replay readiness.

The optional reference-receiver test requires a running receiver from the companion integration branch:

```bash
export PERSISTENT_ATP_CLIENT_TEST_URL=http://127.0.0.1:8765
export PERSISTENT_ATP_CLIENT_TEST_TOKEN="$PERSISTENT_ATP_OMEGA_TOKEN"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  packages/persistent-atp-client/tests/test_reference_receiver.py
```

That test verifies reference artifact upload/download, idempotent admission, a scripted critic cycle, terminal event consumption, guidance, consistent pagination and operation reattachment. It skips explicitly when no receiver URL is configured. Durable service guarantees still belong to the receiver, and service changes are outside this client-only branch.

Recorded verification for 0.2.0 on Python 3.13.5 with httpx 0.28.1:

- Package baseline: 22 passed. After fixes: 67 passed and one unconfigured reference-receiver test skipped.
- With a reference receiver configured, the package suite passed all 68 tests from outside the repository working directory.
- Reopening the existing reference receiver against the same state preserved artifact bytes, journal revision/hash, all three operation records, and projected graph records. This was a connection/service-state reopen check, not a process-kill recovery test.
- A fresh non-editable core wheel installation outside the checkout prepared requests and loaded all three schema resources without httpx installed. HTTP construction failed explicitly until the `[http]` extra was installed; installing that extra also succeeded.
- Full branch baseline: 526 passed, 15 failed, 78 skipped, 116 subtests passed. After fixes: 571 passed, 15 failed, 79 skipped, 116 subtests passed. The additional skip is the opt-in receiver test. All 15 failures remain PostgreSQL artifact authentication failures for the configured `user` at `127.0.0.1:5432`.

The live verification used the existing receiver from `feat/omegaclaw-integration` without modifying it: real commit gate and SQLite journal/operation records, reference filesystem artifact storage, and a scripted critic. It did not exercise production PostgreSQL/MORK persistence, Omega/PeTTa processes, real ATP execution or independent Lean replay. Those results must not be inferred from SDK conformance tests.
