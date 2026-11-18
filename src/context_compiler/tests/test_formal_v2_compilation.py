"""Lease compiler produces the exact v2 request covered by its packet digest."""

import json
import pytest

from context_compiler.errors import ContextValidationError
from context_compiler.formal import compile_formal
from context_compiler.tests.test_formal import make, DECL
from mathproof.formal_v2 import validate


def compilation(**kwargs):
    args = list(make())
    args[2].bodies[DECL]["lean_name"] = "Demo.goal"
    args[2].bodies[DECL]["lean_source_artifact"] = "sha256:" + "a" * 64
    return compile_formal(*args, protocol_version=2, **kwargs)


def test_v2_default_source_and_total_allowance_are_manifest_bound():
    out = compilation()
    body = json.loads(out.packet.content)["request"]
    assert validate("formal-search-request", body) == []
    assert body["input"] == {"kind": "source", "artifact_hash": "sha256:" + "a" * 64,
                             "declaration": "Demo.goal", "starting_state": "binders-introduced"}
    assert body["budget"] == {"max_steps": 40, "wall_clock_ms": 60000}
    assert "backend" not in body and "deployment_id" not in body
    assert "max_tactic_steps" not in body["search_policy"]
    assert body["fencing_token"] == out.payload.fencing_token


def test_backend_override_changes_packet_digest_and_is_strict():
    default = compilation()
    selected = compilation(backend={"name": "mcts-actor-critic", "profile": "puct"}, deployment_id="trained")
    assert default.packet_digest != selected.packet_digest
    assert json.loads(selected.packet.content)["request"]["deployment_id"] == "trained"
    with pytest.raises(ContextValidationError):
        compilation(backend={"name": "mcts-actor-critic", "profile": "puct", "typo": 1})


def test_goal_requires_explicit_context_and_never_replaces_source_silently():
    args = make()
    out = compile_formal(*args)
    with pytest.raises(ContextValidationError, match="lean_name"):
        out.payload.to_schema_dict(protocol_version=2)
    goal = {"kind": "goal", "goal_text": "P", "hypotheses": ["P : Prop", "h : P"], "universe_levels": []}
    body = out.payload.to_schema_dict(protocol_version=2, formal_input=goal)
    assert body["input"] == goal
    with pytest.raises(ContextValidationError):
        out.payload.to_schema_dict(protocol_version=2, formal_input={"kind": "goal", "goal_text": "P"})
    with pytest.raises(ContextValidationError, match="selected declaration"):
        compilation(formal_input={"kind": "source", "artifact_hash": "sha256:" + "b" * 64,
                                  "declaration": "Demo.goal", "starting_state": "binders-introduced"})


def test_legacy_compilation_stays_explicit():
    payload = compilation().payload
    assert "protocol_version" not in payload.to_schema_dict()
    with pytest.raises(ContextValidationError):
        payload.to_schema_dict(protocol_version=True)


def test_v2_scheduler_dispatch_matches_the_hashed_packet():
    from context_compiler.formal_runtime import compile_formal_for_lease
    from context_compiler.tests.test_formal_runtime import runtime_case, config, NoArtifacts

    _, _, lease, snapshot = runtime_case()
    # V2 requires a source artifact, not the hash of the declaration record.
    from commit_gate.state import JournalOverlayView
    from commit_gate.ops import SetField
    from context_compiler.snapshot import ProofSnapshot
    snapshot = ProofSnapshot(snapshot.proof_id, snapshot.revision,
        JournalOverlayView(snapshot.view, snapshot.revision,
            (SetField("FormalDeclaration", "p1/fd-1", "lean_source_artifact", "sha256:" + "a" * 64, prior=None),)))
    dispatch = compile_formal_for_lease(lease, snapshot, NoArtifacts(), config(protocol_version=2))
    assert dispatch.worker_input == json.loads(dispatch.packet.content)["request"]
    assert dispatch.worker_input["input"]["declaration"] == "Example.main"
    assert dispatch.worker_input["budget"]["max_steps"] == 50
    assert dispatch.worker_input["base_revision"] == lease.base_revision
    assert dispatch.worker_input["fencing_token"] == lease.fencing_token


def test_provider_environment_id_is_distinct_from_proof_graph_node_id():
    args = list(make())
    args[2].bodies[DECL]["lean_name"] = "Demo.goal"
    args[2].bodies["env-1"]["provider_environment_id"] = "mathlib-4.10-rc1"
    args[2].bodies[DECL]["lean_source_artifact"] = "sha256:" + "a" * 64
    out = compile_formal(*args, protocol_version=2)
    body = json.loads(out.packet.content)["request"]
    assert body["environment_id"] == "mathlib-4.10-rc1"
    assert out.payload.environment["environment_id"] == "env-1"
