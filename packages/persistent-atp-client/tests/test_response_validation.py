"""Malformed data is a protocol failure, never authoritative proof state."""
import asyncio
from dataclasses import replace
import json

import pytest

from persistent_atp_client import (Admission, ArtifactRef, AsyncClient, CapabilitySet,
    CoordinationPage, Event, OperationRef, OperationSnapshot, ProjectDigest, ProofCycle)
from persistent_atp_client.errors import ReceiverProtocolError, SubmissionUncertain
from persistent_atp_client.testing import ScriptedReceiverPort

HASH = "sha256:" + "a" * 64


def test_capability_flags_and_response_metadata_have_strict_types():
    with pytest.raises(TypeError):
        CapabilitySet("2.1", frozenset(), independent_replay="false")
    with pytest.raises(TypeError):
        CapabilitySet("2.1", {"proof-cycle"})
    ref = OperationRef("lab", "op1")
    with pytest.raises(TypeError):
        Admission("r1", HASH, True, {"workspace_id": "lab", "operation_id": "op1"})
    with pytest.raises(ValueError):
        OperationSnapshot(ref, "r1", 0, "running", project_revision=1, projection_watermark=2)
    with pytest.raises(TypeError):
        OperationSnapshot(ref, "r1", 0, "running", evidence_artifacts=[ArtifactRef(HASH)])
    with pytest.raises(ValueError):
        Event("e1", 1, "op1:1", ref, "running", "0")


@pytest.mark.parametrize("field,value", [("workspace_id", "other"), ("proof_id", "p2"), ("revision", 2)])
def test_status_rejects_wrong_identity_or_requested_revision(field, value):
    async def scenario():
        port = ScriptedReceiverPort()
        response = ProjectDigest("lab", "p1", 1, HASH, 1, ArtifactRef(HASH),
            "True", "provisional", False, "scripted", "scripted-test")
        async def read(**kwargs):
            return replace(response, **{field: value})
        port.read_status = read
        with pytest.raises(ReceiverProtocolError):
            await AsyncClient(port).status(workspace_id="lab", proof_id="p1", at_revision=1)
    asyncio.run(scenario())


def test_coordination_read_rejects_wrong_scope():
    async def scenario():
        port = ScriptedReceiverPort()
        async def caps(**kwargs):
            return CapabilitySet("2.1", frozenset(), frozenset({"coordination-read"}))
        async def read(**kwargs):
            return CoordinationPage("other", "p2", 1, HASH, 1, (), 0, None, "scripted-test")
        port.capabilities, port.read_coordination = caps, read
        with pytest.raises(ReceiverProtocolError):
            await AsyncClient(port).coordination_page("p1", workspace_id="lab")
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["workspace", "proof", "duplicate"])
def test_operation_listing_rejects_misrouted_or_duplicate_records(kind):
    async def scenario():
        port = ScriptedReceiverPort()
        item = OperationSnapshot(OperationRef("other" if kind == "workspace" else "lab", "op1"),
            "r1", 0, "queued", proof_id="p2" if kind == "proof" else "p1")
        async def read(**kwargs): return (item, item) if kind == "duplicate" else (item,)
        port.list_operations = read
        with pytest.raises(ReceiverProtocolError):
            await AsyncClient(port).list_operations(workspace_id="lab", proof_id="p1")
    asyncio.run(scenario())


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_malformed_acknowledgment_reconciles_or_retains_the_original_request(lookup_fails):
    class BadAcknowledgment(ScriptedReceiverPort):
        calls = 0
        lookups = 0
        async def submit(self, request):
            self.calls += 1
            await super().submit(request)
            raise json.JSONDecodeError("truncated acknowledgment", "{", 1)
        async def lookup_request(self, *args):
            self.lookups += 1
            if lookup_fails:
                raise ReceiverProtocolError("malformed lookup")
            return await super().lookup_request(*args)
    async def scenario():
        port = BadAcknowledgment()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab", request_id="stable")
        if lookup_fails:
            with pytest.raises(SubmissionUncertain) as error:
                await client.submit(request)
            assert error.value.request is request
        else:
            handle = await client.submit(request)
            assert (await handle.snapshot()).request_id == "stable"
        assert port.calls == port.lookups == len(port._admissions) == 1
    asyncio.run(scenario())


def test_mismatched_admission_never_returns_an_unrelated_handle():
    class WrongAcknowledgment(ScriptedReceiverPort):
        async def submit(self, request):
            receipt = await super().submit(request)
            return replace(receipt, request_id="wrong")
        async def lookup_request(self, *args):
            receipt = await super().lookup_request(*args)
            return replace(receipt, operation_ref=OperationRef("other", "op1"))
    async def scenario():
        client = AsyncClient(WrongAcknowledgment())
        request = client.prepare(ProofCycle("p1"), workspace_id="lab")
        with pytest.raises(SubmissionUncertain) as error:
            await client.submit(request)
        assert error.value.request is request
    asyncio.run(scenario())
