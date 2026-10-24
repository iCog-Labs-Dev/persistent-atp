"""Real reference gate path through the independent typed client package."""
import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading

import pytest

from persistent_atp_client import AsyncClient, Preconditions, ProofCreate, ProofCycle, ProofStatus
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.errors import ArtifactUnavailable, IdempotencyConflict, StalePrecondition, UnsupportedCapability
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from omegaclaw.api import serve


def test_reference_receiver_durable_admission_gate_and_restart(tmp_path):
    async def scenario():
        service = OperationService(tmp_path)
        receiver = ReferenceReceiver(service)
        client = AsyncClient(LocalReceiverAdapter(receiver))
        spec = json.dumps({"proof_id": "v2proof", "title": "Unicode target",
                           "literal_target": "∀ n : Nat, n = n"}, ensure_ascii=False).encode()
        artifact = await client.put_artifact(spec, workspace_id="reference", media_type="application/json")
        prepared = client.prepare(ProofCreate(artifact), workspace_id="reference", request_id="create-stable")
        handle = await client.submit(prepared)
        assert (await client.submit(prepared)).ref == handle.ref
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded"
        assert result.project_revision == 1
        assert result.result["formal_verified"] is False
        assert service.store.verify_chain("v2proof") == 1
        digest = await client.status(workspace_id="reference", proof_id="v2proof")
        assert digest.revision == 1 and digest.claim_status == "provisional"
        assert digest.literal_target == "∀ n : Nat, n = n"
        assert await client.get_artifact(digest.target_artifact, workspace_id="reference") == "∀ n : Nat, n = n".encode()
        cycle = client.prepare(ProofCycle("v2proof"), workspace_id="reference", request_id="cycle-stable")
        cycle_result = await (await client.submit(cycle)).wait(timeout=5)
        assert cycle_result.lifecycle == "succeeded"
        assert cycle_result.result["formal_verified"] is False
        assert service.store.verify_chain("v2proof") == 2
        receipt = await client.lookup_request(workspace_id="reference", request_id="cycle-stable")
        assert receipt.operation_ref == cycle_result.ref
        events, cursor = await receiver.read_events(cycle_result.ref)
        assert [event.event_type for event in events] == ["queued", "running", "succeeded"]
        assert cursor == events[-1].cursor
        receiver.close()
        reopened_service = OperationService(tmp_path)
        reopened = ReferenceReceiver(reopened_service)
        restored = AsyncClient(LocalReceiverAdapter(reopened))
        assert (await restored.attach(cycle_result.ref).snapshot()).lifecycle == "succeeded"
        assert (await restored.submit(cycle)).ref == cycle_result.ref
        assert reopened_service.store.verify_chain("v2proof") == 2
        reopened.close()
    asyncio.run(scenario())


def test_reference_http_port_matches_local_contract(tmp_path):
    service = OperationService(tmp_path)
    receiver = ReferenceReceiver(service)
    server = serve(service, "long-reference-transport-token", port=0, receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        url = f"http://127.0.0.1:{server.server_port}"
        remote = AsyncClient(HTTPReceiverAdapter(url, "long-reference-transport-token"))
        local = AsyncClient(LocalReceiverAdapter(receiver))
        assert (await remote.capabilities()) == (await local.capabilities())
        spec = json.dumps({"proof_id": "remotep", "title": "Remote",
                           "literal_target": "∀ x, x = x"}, ensure_ascii=False).encode()
        artifact = await remote.put_artifact(spec, workspace_id="reference", media_type="application/json")
        assert await remote.get_artifact(artifact, workspace_id="reference") == spec
        request = remote.prepare(ProofCreate(artifact), workspace_id="reference", request_id="remote-stable")
        handle = await remote.submit(request)
        assert (await remote.lookup_request(workspace_id="reference", request_id="remote-stable")).operation_ref == handle.ref
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded"
        assert result.project_revision == 1
        assert (await local.status(workspace_id="reference", proof_id="remotep")) == (
            await remote.status(workspace_id="reference", proof_id="remotep"))
        assert service.store.verify_chain("remotep") == 1
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        receiver.close()


def test_reference_receiver_rejects_unowned_artifact_and_reused_id(tmp_path):
    async def scenario():
        service = OperationService(tmp_path)
        receiver = ReferenceReceiver(service)
        client = AsyncClient(LocalReceiverAdapter(receiver))
        raw = b'{"proof_id":"a","title":"A","literal_target":"True"}'
        hash_only = service.artifacts.put(raw)
        from persistent_atp_client import ArtifactRef
        with pytest.raises(ArtifactUnavailable):
            await client.submit(client.prepare(ProofCreate(ArtifactRef(hash_only)),
                                               workspace_id="reference", request_id="same"))
        ref = await client.put_artifact(raw, workspace_id="reference", media_type="application/json")
        await client.submit(client.prepare(ProofCreate(ref), workspace_id="reference", request_id="same"))
        with pytest.raises(IdempotencyConflict):
            await client.submit(client.prepare(ProofStatus("a"), workspace_id="reference", request_id="same"))
        receiver.close()
    asyncio.run(scenario())


def test_reference_receiver_checks_explicit_preconditions(tmp_path):
    async def scenario():
        service = OperationService(tmp_path)
        receiver = ReferenceReceiver(service)
        client = AsyncClient(LocalReceiverAdapter(receiver))
        body = b'{"proof_id":"pins","title":"Pins","literal_target":"True"}'
        ref = await client.put_artifact(body, workspace_id="reference", media_type="application/json")
        create = client.prepare(ProofCreate(ref), workspace_id="reference", request_id="pin-create",
                                preconditions=Preconditions(expected_revision=0))
        await (await client.submit(create)).wait(timeout=5)
        with pytest.raises(StalePrecondition):
            await client.submit(client.prepare(ProofCycle("pins"), workspace_id="reference",
                preconditions=Preconditions(expected_revision=0)))
        with pytest.raises(UnsupportedCapability):
            await client.submit(client.prepare(ProofCycle("pins"), workspace_id="reference",
                preconditions=Preconditions(environment_hash="sha256:" + "a" * 64)))
        assert service.store.verify_chain("pins") == 1
        receiver.close()
    asyncio.run(scenario())


def test_reference_receiver_recovers_after_process_kill(tmp_path):
    """Kill after durable admission while the executor has not committed."""
    source = r'''
import asyncio, json, sys, time
from omegaclaw.service import OperationService
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, ProofCreate
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.codecs import to_dict

async def main(root):
    service = OperationService(root)
    original = service.submit
    def delayed(request):
        time.sleep(60)
        return original(request)
    service.submit = delayed
    receiver = ReferenceReceiver(service)
    client = AsyncClient(LocalReceiverAdapter(receiver))
    body = b'{"proof_id":"killcase","title":"Restart","literal_target":"True"}'
    ref = await client.put_artifact(body, workspace_id="reference", media_type="application/json")
    request = client.prepare(ProofCreate(ref), workspace_id="reference", request_id="kill-stable")
    handle = await client.submit(request)
    print(json.dumps({"operation_id": handle.ref.operation_id, "artifact": to_dict(ref)}), flush=True)
    time.sleep(60)
asyncio.run(main(sys.argv[1]))
'''
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "packages/persistent-atp-client/src")))
    child = subprocess.Popen([sys.executable, "-c", source, str(tmp_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, child.poll()
        admitted = json.loads(child.stdout.readline())
    finally:
        child.kill()
        child.communicate(timeout=5)

    async def recover():
        service = OperationService(tmp_path)
        receiver = ReferenceReceiver(service)
        client = AsyncClient(LocalReceiverAdapter(receiver))
        from persistent_atp_client import ArtifactRef, OperationRef
        handle = client.attach(OperationRef("reference", admitted["operation_id"]))
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded"
        assert result.project_revision == 1
        assert service.store.verify_chain("killcase") == 1
        request = client.prepare(ProofCreate(ArtifactRef(**admitted["artifact"])),
                                 workspace_id="reference", request_id="kill-stable")
        assert (await client.submit(request)).ref == handle.ref
        assert service.store.verify_chain("killcase") == 1
        receiver.close()
    asyncio.run(recover())
