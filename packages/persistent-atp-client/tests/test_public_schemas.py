"""The packaged response/control schemas match public DTO serialization."""
import json
from importlib.resources import files

import jsonschema
import pytest

from persistent_atp_client import (
    Admission, ArtifactRef, Budget, BudgetAmendment, CancelRequest,
    CapabilitySet, DecisionResponse, OperationRef, OperationSnapshot,
    ProjectDigest, CoordinationItem, CoordinationPage, schema_manifest, AsyncClient, Preconditions,
    QueueResearchMoves, ResearchGuidance,
)
from persistent_atp_client.codecs import to_dict
from persistent_atp_client.models import Event


def test_public_models_have_packaged_strict_schemas():
    schema = json.loads(files("persistent_atp_client").joinpath("schemas/public-models.schema.json").read_text())
    digest = "sha256:" + "a" * 64
    ref = OperationRef("lab", "op1")
    artifact = ArtifactRef(digest, 3, "text/plain")
    models = (
        artifact,
        ref,
        Admission("request-1", digest, True, ref),
        OperationSnapshot(ref, "request-1", 2, "succeeded", proof_id="p1", project_revision=1,
                          journal_hash=digest, projection_watermark=1, evidence_artifacts=(artifact,)),
        ProjectDigest("lab", "p1", 1, digest, 1, artifact, "∀ x, x = x", "provisional", False, "open"),
        Event("event-1", 1, "op1:1", ref, "queued", 0),
        CapabilitySet("2.0", frozenset(("proof-cycle",)), backend_mode="scripted-test", receiver_mode="scripted-test"),
        CapabilitySet("2.1", frozenset(("queue-research-moves",)), frozenset(("coordination-read",)),
                      backend_mode="unavailable", receiver_mode="reference-scripted"),
        CapabilitySet("2.2", frozenset(("queue-coordinator-escalations",)),
                      frozenset(("atomic-coordinator-escalations", "cycle-execution-revision")),
                      backend_mode="unavailable", receiver_mode="reference-scripted"),
        CoordinationItem("node", "p1/c-1", "Claim", {"status": "provisional"}),
        CoordinationPage("lab", "p1", 1, digest, 1,
                         (CoordinationItem("node", "p1/c-1", "Claim", {}),), 1,
                         None, "reference-scripted"),
        CancelRequest("cancel-1", ref),
        BudgetAmendment("budget-1", ref, 0, Budget(2, 100)),
        DecisionResponse("decision-1", ref, "d1", 0, "keep", digest),
    )
    for model in models:
        selected = {"$schema": schema["$schema"], "$defs": schema["$defs"],
                    "$ref": f"#/$defs/{type(model).__name__}"}
        jsonschema.validate(to_dict(model), selected)
        with_extra = {**to_dict(model), "actor": "forged"}
        assert not jsonschema.Draft202012Validator(selected).is_valid(with_extra)


def test_schema_manifest_uses_packaged_bytes_and_stable_ids():
    manifest = schema_manifest()
    assert {item["name"] for item in manifest} == {
        "action-request.schema.json", "coordination-request.schema.json",
        "coordinator-request.schema.json",
        "public-models.schema.json",
    }
    assert all(item["schema_id"].startswith("https://persistent-atp.local/schemas/")
               and item["sha256"].startswith("sha256:") for item in manifest)
    assert manifest == schema_manifest()


@pytest.mark.parametrize("version,actions,controls", [
    ("2.0", ("proof-cycle-selected",), ()),
    ("2.0", ("queue-research-moves",), ()),
    ("2.0", ("queue-coordinator-escalations",), ()),
    ("2.1", ("queue-coordinator-escalations",), ()),
    ("2.0", (), ("atomic-coordinator-escalations",)),
    ("2.1", (), ("atomic-coordinator-escalations",)),
    ("2.0", (), ("cycle-execution-revision",)),
    ("2.1", (), ("cycle-execution-revision",)),
])
def test_capability_schema_rejects_actions_and_guarantees_from_newer_contracts(version, actions, controls):
    schema = json.loads(files("persistent_atp_client").joinpath("schemas/public-models.schema.json").read_text())
    validator = jsonschema.Draft202012Validator({"$defs": schema["$defs"],
                                               "$ref": "#/$defs/CapabilitySet"})
    raw = {"contract_version": version, "actions": list(actions), "controls": list(controls),
           "backend_mode": "unavailable", "independent_replay": False,
           "receiver_mode": "reference-scripted"}
    assert not validator.is_valid(raw)
    with pytest.raises(ValueError):
        CapabilitySet(version, frozenset(actions), frozenset(controls))


def test_coordination_request_schema_matches_typed_client():
    schema = json.loads(files("persistent_atp_client").joinpath(
        "schemas/coordination-request.schema.json").read_text())
    request = AsyncClient(None).prepare(QueueResearchMoves("p1", "intent-1", (
        ResearchGuidance("bridge-lemma", "Investigate an intermediate statement", "p1/c-1"),)),
        workspace_id="reference", preconditions=Preconditions(expected_revision=3))
    payload = to_dict(request)
    jsonschema.validate(payload, schema)
    assert not jsonschema.Draft202012Validator(schema).is_valid({
        **payload, "action": {**payload["action"], "ops": [{"op": "set_field"}]}})
    assert not jsonschema.Draft202012Validator(schema).is_valid({
        **payload, "contract_version": "2.0"})
