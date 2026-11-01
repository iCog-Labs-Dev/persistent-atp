"""The CLI seeds a durable proof and runs the same coordinator as the API."""

import json
from uuid import uuid4

import pytest

from commit_gate.state import MemoryView
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore
from coordinator.cli import initialize, inspect, main, run_proof
from coordinator.workspace import project
from shared.llm_client import MockLLMClient


def test_init_and_status_are_durable_and_preserve_theorem_kernel(tmp_path, capsys):
    journal = tmp_path / "proof" / "journal.sqlite"
    proof_id = "cli-1"
    assert main([
        "init", "--journal", str(journal), "--proof-id", proof_id,
        "--statement", "For all n : Nat, n + 0 = n",
        "--assumption", "n is a natural number",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    assert main(["status", "--journal", str(journal), "--proof-id", proof_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["frontier"] == {"research-move": 1}
    assert status["verified_chain"] == 1

    view = MemoryView()
    project(view, JournalStore(str(journal)), proof_id)
    assert view.node(f"{proof_id}/theorem").fields["assumptions"] == ["n is a natural number"]
    with pytest.raises(ValueError, match="already has journal events"):
        initialize(journal, proof_id, "changed theorem", "new task")


@pytest.mark.parametrize("backend", ["memory", "mork"])
def test_run_uses_real_research_worker_and_journal_projection(tmp_path, backend):
    pytest.importorskip("langgraph")
    if backend == "mork":
        from mork.backend import MorkSpace, MorkUnavailable
        try:
            MorkSpace()
        except MorkUnavailable:
            pytest.skip("MORK library unavailable")

    journal = tmp_path / "journal.sqlite"
    proof_id = f"cli-{uuid4().hex[:12]}"
    initialize(journal, proof_id, "For all n : Nat, n + 0 = n", "Find a useful induction step")
    llm = MockLLMClient(default_json={"moves": [], "reasoning": "No justified next move."})

    result = run_proof(
        journal, proof_id, rounds=1, backend=backend, lean_project=None, llm=llm,
    )

    assert result["revision"] == result["verified_chain"]
    assert result["reports"][0]["slots"][0]["accepted"]
    assert result["reports"][0]["slots"][0]["worker_class"] == "llm-research"
    assert len(llm.history) == 1
    assert "For all n : Nat, n + 0 = n" in llm.history[0][1]
    assert inspect(journal, proof_id)["revision"] == result["revision"]


def test_lean_project_enables_formalizer_and_alignment_workers(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    from mathproof.lean_statement import StatementCheck
    from coordinator import cli

    class Checked:
        def __init__(self, project):
            assert project == tmp_path

        def check(self, lean_type, imports):
            assert lean_type == "True" and imports == ["Init"]
            return StatementCheck(True, "elaborated")

    monkeypatch.setattr(cli, "LeanStatementChecker", Checked)
    journal = tmp_path / "journal.sqlite"
    proof_id = "lean-cli"
    initialize(journal, proof_id, "True", "Investigate the theorem")
    store = JournalStore(str(journal))
    lease_id = f"{proof_id}/seed-claim"
    token = store.acquire_lease(proof_id, lease_id, actor="seed", worker_class="maintenance")
    try:
        seeded = CommitGate(MemoryView(), store).commit(Proposal(
            proof_id, "seed", "maintenance",
            ops=(UpsertNode("Claim", f"{proof_id}/c-1", {
                "status": "provisional", "claim_text": "True", "proof_sketch": "True.intro",
            }),),
            base_revision=store.head(proof_id)[0], lease_id=lease_id, fencing_token=token,
        ))
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=token)
    assert seeded.accepted, seeded.rejections
    llm = MockLLMClient(responses=[
        {"verdict": "supported", "reasoning": "The sketch supplies True.intro",
         "earliest_gap": None, "repair": None, "evidence_ids": []},
        {"lean_type": "True", "imports": ["Init"], "reasoning": "Literal statement"},
        {"verdict": "aligned", "relation": "exact", "reasoning": "Both say True"},
        {"moves": [], "reasoning": "No additional research move"},
    ])

    result = run_proof(
        journal, proof_id, rounds=1, backend="memory", lean_project=tmp_path, llm=llm,
    )

    assert [slot["slot"] for slot in result["reports"][0]["slots"]] == [
        "critic", "formalize", "alignment", "explore",
    ]
    view = MemoryView()
    project(view, store, proof_id)
    assert view.node(f"{proof_id}/fd-1").fields["status"] == "aligned"
    assert len(llm.history) == 4
