"""Receiver-produced research artifacts retain principal/workspace ownership."""
import asyncio
import json
import threading

import pytest

from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.tests.test_research_recovery import research
from persistent_atp_client import AsyncClient, ArtifactRef, LemmaBridge
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import ArtifactUnavailable, Forbidden


async def finished(receiver):
    async with asyncio.timeout(5):
        while receiver._pending:
            await asyncio.sleep(.01)


@pytest.mark.parametrize("transport", ["local", "http"])
def test_research_grants_are_available_before_snapshot_and_scoped(research, transport):
    service = research.service
    receiver = ReferenceReceiver(service, principal="owner")
    server = thread = None
    if transport == "http":
        server = serve(service, "result-artifact-test-token", receiver=receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = HTTPReceiverAdapter(f"http://127.0.0.1:{server.server_port}", "result-artifact-test-token")
    else:
        port = LocalReceiverAdapter(receiver)
    async def scenario():
        client = AsyncClient(port)
        handle = await client.lemma_bridge("p1", "p1/obs-1", workspace_id="reference", request_id="research-grants")
        await finished(receiver)
        row = receiver._db.execute("SELECT * FROM requests WHERE operation_id=?", (handle.ref.operation_id,)).fetchone()
        assert row["lifecycle"] == "succeeded"
        result = json.loads(row["result"])
        pointers = {result["packet_artifact"], result["result_artifact"]}
        grants = receiver._db.execute("SELECT hash FROM artifact_grants WHERE principal='owner' AND workspace_id='reference'").fetchall()
        assert pointers <= {row["hash"] for row in grants}  # _finish(), before snapshot repair.
        revision = service.store.head("p1")[0]
        for pointer in pointers:
            assert await client.get_artifact(ArtifactRef(pointer), workspace_id="reference") == service.artifacts.get(pointer)
            with pytest.raises(Forbidden):
                await receiver.get_artifact(ArtifactRef(pointer), workspace_id="foreign-workspace")
        other = ReferenceReceiver(service, principal="other")
        try:
            for pointer in pointers:
                with pytest.raises(ArtifactUnavailable):
                    await other.get_artifact(ArtifactRef(pointer), workspace_id="reference")
        finally:
            other.close()
        # A second logical workspace shares neither these grants nor their scope.
        service.workspace = "another-workspace"
        elsewhere = ReferenceReceiver(service, principal="owner")
        try:
            for pointer in pointers:
                with pytest.raises(ArtifactUnavailable):
                    await elsewhere.get_artifact(ArtifactRef(pointer), workspace_id="another-workspace")
        finally:
            elsewhere.close()
            service.workspace = "reference"
        snapshot = await handle.snapshot()
        receiver._db.executemany("DELETE FROM artifact_grants WHERE principal='owner' AND workspace_id='reference' AND hash=?",
                                 [(pointer,) for pointer in pointers])
        repaired = await handle.snapshot()
        assert repaired.observation_version == snapshot.observation_version
        for pointer in pointers:
            assert await client.get_artifact(ArtifactRef(pointer), workspace_id="reference") == service.artifacts.get(pointer)
        assert service.store.head("p1")[0] == revision
        await client.aclose()
    try:
        asyncio.run(scenario())
    finally:
        if server:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        receiver.close()


def test_unbound_result_hashes_do_not_authorize_private_artifacts(research):
    service = research.service
    receiver = ReferenceReceiver(service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.lemma_bridge("p1", "p1/obs-1", workspace_id="reference")
        result = (await handle.wait(timeout=5, poll_interval=.01)).result
        private = service.artifacts.put(b"unrelated private artifact")
        fake = {**result, "packet_artifact": private, "result_artifact": private,
                "untrusted_text": {"artifact_pointers": [private]}}
        receiver._finish(handle.ref.operation_id, "succeeded", 0, fake)
        await handle.snapshot()  # The historical-grant repair must reject it too.
        with pytest.raises(ArtifactUnavailable):
            await client.get_artifact(ArtifactRef(private), workspace_id="reference")
        assert service.artifacts.exists(private)
    try:
        asyncio.run(scenario())
    finally:
        receiver.close()


def test_restart_under_another_principal_grants_only_original_owner(research, monkeypatch):
    service = research.service
    original = ReferenceReceiver(service, principal="owner")
    async def queue():
        client = AsyncClient(LocalReceiverAdapter(original))
        return await client.lemma_bridge("p1", "p1/obs-1", workspace_id="reference", request_id="recover-owner")
    with monkeypatch.context() as patch:
        patch.setattr(original, "_schedule", lambda operation: None)
        handle = asyncio.run(queue())
    original.close()
    replacement = ReferenceReceiver(service, principal="replacement")
    async def complete():
        await finished(replacement)
        row = replacement._db.execute("SELECT result FROM requests WHERE operation_id=?", (handle.ref.operation_id,)).fetchone()
        result = json.loads(row["result"])
        for field in ("packet_artifact", "result_artifact"):
            with pytest.raises(ArtifactUnavailable):
                await replacement.get_artifact(ArtifactRef(result[field]), workspace_id="reference")
            assert replacement._db.execute("SELECT 1 FROM artifact_grants WHERE principal='owner' AND workspace_id='reference' AND hash=?",
                (result[field],)).fetchone()
        return result
    try:
        result = asyncio.run(complete())
    finally:
        replacement.close()
    owner = ReferenceReceiver(service, principal="owner")
    async def retrieve():
        for field in ("packet_artifact", "result_artifact"):
            assert await owner.get_artifact(ArtifactRef(result[field]), workspace_id="reference") == service.artifacts.get(result[field])
    try:
        asyncio.run(retrieve())
    finally:
        owner.close()
