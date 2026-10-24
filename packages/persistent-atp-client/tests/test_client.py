"""Contract tests that need no proof service, graph, database or Omega runtime."""
import asyncio
import hashlib
from importlib.resources import files
import json

import pytest
import jsonschema

from persistent_atp_client import (
    ActionRequest, Admission, ArtifactRef, AsyncClient, Budget, CapabilitySet,
    FormalResume, FormalSearch, LemmaBridge, OperationRef, OperationSnapshot,
    ProofAudit, ProofCreate, ProofCycle, ProofExport, ProofStatus, StatementAlign,
    SyncClient,
)
from persistent_atp_client.codecs import canonical_json, fingerprint, request_from_dict, request_from_json, to_dict
from persistent_atp_client.errors import HashMismatch, SubmissionUncertain


HASH = "sha256:" + "a" * 64
ACTIONS = (
    ProofCreate(ArtifactRef(HASH, 5, "application/json")),
    ProofCycle("p1"), ProofStatus("p1", 1), ProofAudit("p1"),
    ProofExport("p1", "registered-export"), FormalSearch("p1", "p1/c-1", Budget(3, 1000)),
    FormalResume("p1", "p1/run-1", Budget(2, 500)),
    StatementAlign("p1", "p1/c-1", ArtifactRef(HASH)),
    LemmaBridge("p1", "p1/ob-1"),
)


@pytest.mark.parametrize("action", ACTIONS)
def test_nine_actions_round_trip_exactly(action):
    request = ActionRequest("request-Ω", "math-lab", action)
    encoded = canonical_json(request)
    assert request_from_json(encoded) == request
    assert encoded == canonical_json(request_from_dict(to_dict(request)))
    assert fingerprint(request) == "sha256:" + hashlib.sha256(encoded).hexdigest()
    assert b"\\u03a9" not in encoded
    schema = json.loads(files("persistent_atp_client").joinpath("schemas/action-request.schema.json").read_text())
    jsonschema.validate(json.loads(encoded), schema)


def test_unknown_fields_and_duplicate_keys_fail_closed():
    raw = to_dict(ActionRequest("r1", "w", ProofCycle("p1")))
    with pytest.raises(ValueError):
        request_from_dict({**raw, "actor": "administrator"})
    with pytest.raises(ValueError):
        request_from_dict({**raw, "action": {**raw["action"], "lease": "forged"}})
    with pytest.raises(ValueError):
        request_from_json(canonical_json(ActionRequest("r1", "w", ProofCycle("p1"))).replace(b'"request_id":"r1"', b'"request_id":"r1","request_id":"r2"'))


class ScriptedPort:
    """Scripted-test only; it cannot certify real ATP or replay."""
    def __init__(self):
        self.calls = 0
        self.requests = []
        self.saved = None
        self.snapshot = None
        self.uploaded = None

    async def capabilities(self):
        return CapabilitySet("2.0", frozenset(a.kind for a in ACTIONS),
                             backend_mode="scripted-test", receiver_mode="scripted-test")

    async def submit(self, request):
        self.calls += 1
        self.requests.append(request)
        ref = OperationRef(request.workspace_id, "op1")
        self.saved = Admission(request.request_id, fingerprint(request), True, ref)
        self.snapshot = OperationSnapshot(ref, request.request_id, 0, "queued")
        raise TimeoutError("acknowledgment lost")

    async def lookup_request(self, workspace_id, request_id):
        return self.saved

    async def get_operation(self, ref):
        return self.snapshot

    async def read_status(self, **kwargs):
        return kwargs

    async def put_artifact(self, source, **_):
        self.uploaded = source
        return ArtifactRef("sha256:" + hashlib.sha256(source).hexdigest(), len(source), "text/plain")

    async def get_artifact(self, ref, **_):
        return self.uploaded

    async def aclose(self):
        pass


def test_lost_ack_reconciles_one_operation_and_local_timeout():
    async def scenario():
        port = ScriptedPort()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="w", request_id="stable")
        handle = await client.submit(request)
        assert port.calls == 1
        assert handle.ref == OperationRef("w", "op1")
        with pytest.raises(Exception) as exc:
            await handle.wait(timeout=0.01, poll_interval=0.001)
        assert type(exc.value).__name__ == "ObservationTimeout"
        assert port.calls == 1
        assert (await client.attach(handle.ref).snapshot()).lifecycle == "queued"
    asyncio.run(scenario())


def test_unknown_admission_remains_uncertain():
    async def scenario():
        port = ScriptedPort()
        async def missing(*_): return None
        port.lookup_request = missing
        request = AsyncClient(port).prepare(ProofCycle("p1"), workspace_id="w")
        with pytest.raises(SubmissionUncertain) as exc:
            await AsyncClient(port).submit(request)
        assert exc.value.request_id == request.request_id
        assert exc.value.request is request
        assert port.calls == 1
    asyncio.run(scenario())


def test_artifact_bytes_are_checked_on_both_sides():
    async def scenario():
        port = ScriptedPort()
        client = AsyncClient(port)
        data = b"theorem x : True := by trivial\n"
        ref = await client.put_artifact(data, workspace_id="w", media_type="text/plain")
        assert await client.get_artifact(ref, workspace_id="w") == data
        port.uploaded = b"tampered"
        with pytest.raises(HashMismatch):
            await client.get_artifact(ref, workspace_id="w")
    asyncio.run(scenario())


def test_sync_client_rejects_active_event_loop():
    async def scenario():
        client = SyncClient(AsyncClient(ScriptedPort()))
        with pytest.raises(RuntimeError, match="active event loop"):
            client.capabilities()
    asyncio.run(scenario())


def test_named_methods_share_preparation_and_submission_path():
    async def scenario():
        port = ScriptedPort()
        client = AsyncClient(port)
        ref = ArtifactRef(HASH)
        calls = (
            client.proof_create(ref, workspace_id="w", request_id="one"),
            client.proof_cycle("p1", workspace_id="w", request_id="two"),
            client.proof_audit("p1", workspace_id="w", request_id="three"),
            client.proof_export("p1", "registered", workspace_id="w", request_id="four"),
            client.formal_search("p1", "p1/c-1", Budget(1, 100), workspace_id="w", request_id="five"),
            client.formal_resume("p1", "run", Budget(1, 100), workspace_id="w", request_id="six"),
            client.statement_align("p1", "p1/c-1", ref, workspace_id="w", request_id="seven"),
            client.lemma_bridge("p1", "p1/ob-1", workspace_id="w", request_id="eight"),
        )
        for call in calls:
            assert (await call).ref.workspace_id == "w"
        assert [item.action.kind for item in port.requests] == [
            "proof-create", "proof-cycle", "proof-audit", "proof-export",
            "formal-search", "formal-resume", "statement-align", "lemma-bridge"]
        assert (await client.proof_status("p1", workspace_id="w"))["proof_id"] == "p1"
    asyncio.run(scenario())
