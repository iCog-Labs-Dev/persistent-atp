"""Unit tests for FormalSearchRequest and FormalSearchBudget."""

import pytest
from atp_bridge.request import FormalSearchBudget, FormalSearchRequest


def test_budget_default_and_dict_roundtrip():
    budget = FormalSearchBudget()
    assert budget.wall_seconds == 1800.0
    assert budget.max_nodes == 500
    assert budget.max_depth == 20
    assert budget.checkpoint_every_nodes == 50

    d = budget.to_dict()
    reconstructed = FormalSearchBudget.from_dict(d)
    assert reconstructed == budget


def test_budget_validation():
    with pytest.raises(ValueError, match="wall_seconds"):
        FormalSearchBudget(wall_seconds=-1.0)
    with pytest.raises(ValueError, match="max_nodes"):
        FormalSearchBudget(max_nodes=0)
    with pytest.raises(ValueError, match="max_depth"):
        FormalSearchBudget(max_depth=-5)
    with pytest.raises(ValueError, match="checkpoint_every_nodes"):
        FormalSearchBudget(checkpoint_every_nodes=0)


def test_request_creation_and_serialization():
    req = FormalSearchRequest(
        proof_id="p17",
        claim_id="c42",
        formal_declaration_id="fd17",
        run_id="fr31",
        base_revision=982,
        lease_id="lease771",
        fencing_token=14,
        lean_source_artifact="art-source-17",
        environment_id="env9",
        environment_hash="sha256:123456",
        corpus_manifest="corpus-lean-dojo-4",
        model_bundle="models-atp-2026-07",
        search_policy="gnn-pln-best-first-v1",
        budget=FormalSearchBudget(wall_seconds=600.0, max_nodes=1000),
        goal_statement="forall (p q: Prop), Or p q -> Or q p",
        hypotheses=("p: Prop", "q: Prop"),
    )

    d = req.to_dict()
    assert d["proof_id"] == "p17"
    assert d["claim_id"] == "c42"
    assert d["formal_declaration_id"] == "fd17"
    assert d["run_id"] == "fr31"
    assert d["base_revision"] == 982
    assert d["lease_id"] == "lease771"
    assert d["fencing_token"] == 14
    assert d["budget"]["max_nodes"] == 1000
    assert d["hypotheses"] == ["p: Prop", "q: Prop"]

    reconstructed = FormalSearchRequest.from_dict(d)
    assert reconstructed == req


def test_request_validation():
    with pytest.raises(ValueError, match="proof_id"):
        FormalSearchRequest(
            proof_id="",
            claim_id="c42",
            formal_declaration_id="fd17",
            run_id="fr31",
            base_revision=0,
            lease_id="lease1",
            fencing_token=0,
        )

    with pytest.raises(ValueError, match="base_revision"):
        FormalSearchRequest(
            proof_id="p1",
            claim_id="c1",
            formal_declaration_id="fd1",
            run_id="fr1",
            base_revision=-1,
            lease_id="lease1",
            fencing_token=0,
        )

    with pytest.raises(ValueError, match="fencing_token"):
        FormalSearchRequest(
            proof_id="p1",
            claim_id="c1",
            formal_declaration_id="fd1",
            run_id="fr1",
            base_revision=0,
            lease_id="lease1",
            fencing_token=-5,
        )
