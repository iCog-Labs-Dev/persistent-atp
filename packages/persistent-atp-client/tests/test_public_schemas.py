"""The packaged response/control schemas match public DTO serialization."""
import json
from importlib.resources import files

import jsonschema

from persistent_atp_client import (
    Admission, ArtifactRef, Budget, BudgetAmendment, CancelRequest,
    CapabilitySet, DecisionResponse, OperationRef, OperationSnapshot,
    ProjectDigest, schema_manifest,
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
    assert len(manifest) == 2
    assert all(item["schema_id"].startswith("https://persistent-atp.local/schemas/")
               and item["sha256"].startswith("sha256:") for item in manifest)
    assert manifest == schema_manifest()
