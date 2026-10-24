"""Real reference gate path through the independent typed client package."""
import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from persistent_atp_client import ArtifactRef, AsyncClient, Preconditions, ProofCreate, ProofCycle, ProofStatus
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
        with pytest.raises(ArtifactUnavailable):
            await remote.get_artifact(ArtifactRef("sha256:" + "0" * 64), workspace_id="reference")
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


def test_cli_serve_exposes_typed_receiver_and_rejects_duplicate_json(tmp_path):
    config = tmp_path / "receiver-config.json"
    config.write_text(json.dumps({"root": str(tmp_path / "service"), "workspace": "reference",
                                  "host": "127.0.0.1", "port": 0,
                                  "token_env": "PERSISTENT_ATP_OMEGA_TOKEN"}))
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PERSISTENT_ATP_OMEGA_TOKEN"] = "long-reference-transport-token"
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "packages/persistent-atp-client/src")))
    child = subprocess.Popen([sys.executable, "-m", "omegaclaw.cli", "serve", "--config", str(config)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, child.poll()
        started = json.loads(child.stdout.readline())
        assert started["receiver_mode"] == "reference-scripted"
        endpoint = f"http://127.0.0.1:{started['port']}"
        async def scenario():
            client = AsyncClient(HTTPReceiverAdapter(endpoint, env["PERSISTENT_ATP_OMEGA_TOKEN"]))
            spec = b'{"proof_id":"cliport","title":"CLI","literal_target":"True"}'
            ref = await client.put_artifact(spec, workspace_id="reference", media_type="application/json")
            request = client.prepare(ProofCreate(ref), workspace_id="reference", request_id="cli-stable")
            from persistent_atp_client.codecs import canonical_json
            duplicate = canonical_json(request).replace(b'"request_id":"cli-stable"',
                b'"request_id":"cli-stable","request_id":"forged"')
            raw = urllib.request.Request(endpoint + "/v2/requests", data=duplicate,
                headers={"Authorization": "Bearer " + env["PERSISTENT_ATP_OMEGA_TOKEN"],
                         "Content-Type": "application/json"}, method="POST")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(raw, timeout=5)
            assert exc.value.code == 400
            result = await (await client.submit(request)).wait(timeout=5)
            assert result.lifecycle == "succeeded" and result.project_revision == 1
        asyncio.run(scenario())
    finally:
        child.terminate()
        child.communicate(timeout=5)


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


@pytest.mark.parametrize("stage", ("before-commit", "after-commit"))
def test_reference_receiver_recovers_after_process_kill(tmp_path, stage):
    """Kill on either side of the gate append and reconcile one operation."""
    source = r'''
import asyncio, json, pathlib, sys, time
from omegaclaw.service import OperationService
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, ProofCreate
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.codecs import to_dict

async def main(root):
    service = OperationService(root)
    original = service.submit
    def delayed(request):
        if sys.argv[2] == "before-commit":
            pathlib.Path(root, "kill-marker").write_text("before")
            time.sleep(60)
            return original(request)
        outcome = original(request)
        pathlib.Path(root, "kill-marker").write_text("after")
        time.sleep(60)
        return outcome
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
    child = subprocess.Popen([sys.executable, "-c", source, str(tmp_path), stage],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, child.poll()
        admitted = json.loads(child.stdout.readline())
        marker = tmp_path / "kill-marker"
        deadline = time.monotonic() + 10
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_text() == ("before" if stage == "before-commit" else "after")
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
