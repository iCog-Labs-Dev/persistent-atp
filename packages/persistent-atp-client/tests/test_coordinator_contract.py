"""Contract 2.2 DTOs and negotiation; scripted ports provide no proof authority."""
import asyncio
from dataclasses import replace
from importlib.resources import files
import json

import jsonschema
import pytest

from persistent_atp_client import (
    AsyncClient, CapabilitySet, CoordinatorEscalation, Preconditions,
    QueueCoordinatorEscalations, SelectedProofCycle,
)
from persistent_atp_client.codecs import canonical_json, request_from_json, to_dict
from persistent_atp_client.errors import UnsupportedCapability, UnsupportedContract
from persistent_atp_client.testing import ScriptedReceiverPort


def batch(count=1):
    return QueueCoordinatorEscalations("p1", "round-two", (0, 0, 0), 2,
        tuple(CoordinatorEscalation("repair", f"p1/at-{n}", "Repair the base case", "p1/c-1") for n in range(count)))


def test_typed_atomic_batch_round_trips_and_schema_preserves_distinct_claim_link():
    request = AsyncClient(None).prepare(batch(100), workspace_id="lab", preconditions=Preconditions(expected_revision=7))
    assert request.contract_version == "2.2"
    assert request_from_json(canonical_json(request)) == request
    schema = json.loads(files("persistent_atp_client").joinpath("schemas/coordinator-request.schema.json").read_text())
    jsonschema.validate(to_dict(request), schema)
    first = request.to_dict()["action"]["escalations"][0]
    assert first["target_id"] == "p1/at-0" and first["claim_id"] == "p1/c-1"
    assert len(request.action.escalations) == 100
    validator = jsonschema.Draft202012Validator(schema)
    raw = request.to_dict()
    assert not validator.is_valid({**raw, "action": {**raw["action"], "ops": []}})
    assert not validator.is_valid({**raw, "preconditions": {**raw["preconditions"], "expected_revision": None}})


@pytest.mark.parametrize("changes", [
    {"basis": (True, 0, 0)}, {"basis": (0, 0)}, {"basis": (-1, 0, 0)},
    {"round_number": True}, {"round_number": -1}, {"escalations": ()},
    {"escalations": (CoordinatorEscalation("global-audit", "p1", "audit"),) * 101},
    {"escalations": (CoordinatorEscalation("global-audit", "p1", "audit"),) * 2},
])
def test_invalid_or_truncated_batch_shapes_fail_before_delivery(changes):
    with pytest.raises(ValueError):
        replace(batch(), **changes)


@pytest.mark.parametrize("item", [
    CoordinatorEscalation("repair", "p2/at-1", "wrong proof"),
    CoordinatorEscalation("repair", "p1/at-1", "wrong claim", "p2/c-1"),
])
def test_cross_proof_targets_and_claims_are_rejected(item):
    with pytest.raises(ValueError):
        replace(batch(), escalations=(item,))


def test_metadata_cannot_be_smuggled_into_detail_or_arbitrary_authority_fields():
    request = AsyncClient(None).prepare(batch(), workspace_id="lab", preconditions=Preconditions(expected_revision=1))
    raw = request.to_dict()
    raw["action"]["escalations"][0]["worker_class"] = "maintenance"
    with pytest.raises(ValueError):
        request_from_json(json.dumps(raw))
    with pytest.raises(ValueError):
        CoordinatorEscalation("not-a-policy-rule", "p1", "invalid")
    with pytest.raises(ValueError):
        CoordinatorEscalation("global-audit", "p1", "é" * 2049)


@pytest.mark.parametrize("detail,accepted", [
    ("a" * 4096, True), ("a" * 4097, False),
    ("é" * 2048, True), ("é" * 2049, False),
    ("🧮" * 1024, True), ("🧮" * 1025, False),
    (" \t\n", False),
])
def test_wire_decoder_enforces_nonblank_utf8_byte_limit(detail, accepted):
    request = AsyncClient(None).prepare(batch(), workspace_id="lab",
                                       preconditions=Preconditions(expected_revision=1))
    raw = request.to_dict()
    raw["action"]["escalations"][0]["detail"] = detail
    encoded = json.dumps(raw, ensure_ascii=False)
    if accepted:
        restored = request_from_json(encoded)
        assert restored.action.escalations[0].detail == detail
    else:
        with pytest.raises(ValueError, match="4096 bytes"):
            request_from_json(encoded)


def test_contracts_20_and_21_cannot_carry_the_new_action_or_guarantees():
    for version in ("2.0", "2.1"):
        with pytest.raises(ValueError):
            AsyncClient(None).prepare(batch(), workspace_id="lab", preconditions=Preconditions(expected_revision=1),
                                      contract_version=version)
        with pytest.raises(ValueError):
            CapabilitySet(version, frozenset(), frozenset({"cycle-execution-revision"}))
    with pytest.raises(ValueError):
        AsyncClient(None).prepare(batch(), workspace_id="lab")
    assert AsyncClient(None).prepare(SelectedProofCycle("p1", "critic-task"), workspace_id="lab").contract_version == "2.1"


@pytest.mark.parametrize("action,control", [(batch(), "atomic-coordinator-escalations"),
    (SelectedProofCycle("p1", "critic-task"), "cycle-execution-revision")])
def test_missing_guarantee_stops_before_admission(action, control):
    class Incomplete(ScriptedReceiverPort):
        def __init__(self):
            super().__init__()
            self.submitted = []
        async def submit(self, request):
            self.submitted.append(request)
            return await super().submit(request)
    async def scenario():
        port = Incomplete()
        client = AsyncClient(port)
        prepared = client.prepare(action, workspace_id="lab", preconditions=Preconditions(expected_revision=1),
                                  contract_version="2.2")
        with pytest.raises(UnsupportedCapability, match=control):
            await client.submit(prepared)
        assert port.submitted == []
    asyncio.run(scenario())


def test_new_version_is_negotiated_without_a_silent_downgrade():
    class Legacy(ScriptedReceiverPort):
        async def capabilities(self, *, contract_version="2.0"):
            return await super().capabilities(contract_version="2.1")
    async def scenario():
        client = AsyncClient(Legacy())
        request = client.prepare(batch(), workspace_id="lab", preconditions=Preconditions(expected_revision=1))
        with pytest.raises(UnsupportedContract):
            await client.submit(request)
    asyncio.run(scenario())


def test_named_method_requires_the_advertised_atomic_contract():
    class ExplicitlyScripted(ScriptedReceiverPort):
        async def capabilities(self, *, contract_version="2.0"):
            base = await super().capabilities(contract_version=contract_version)
            if contract_version == "2.2":
                return replace(base, controls=base.controls | {"atomic-coordinator-escalations"})
            return base
    async def scenario():
        client = AsyncClient(ExplicitlyScripted())
        handle = await client.queue_coordinator_escalations("p1", "round-two", batch().escalations,
            basis=(0, 0, 0), round_number=2, workspace_id="lab", preconditions=Preconditions(expected_revision=1))
        assert (await handle.snapshot()).lifecycle == "queued"
    asyncio.run(scenario())
