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


def write_lean_environment(path):
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n", encoding="utf-8")
    (path / "lakefile.toml").write_text('name = "cli_test"\n', encoding="utf-8")
    (path / "lake-manifest.json").write_text(
        json.dumps({"version": "1.1.0", "packages": []}), encoding="utf-8"
    )


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
    write_lean_environment(tmp_path)
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
    environment = view.node(f"{proof_id}/env-1")
    assert environment is not None
    assert environment.fields["toolchain"] == "leanprover/lean4:v4.19.0"
    assert view.edges_from(f"{proof_id}/fd-1", "PINNED_ENVIRONMENT")[0].dst_id == environment.node_id
    assert result["frontier"]["formal-run"] == 1
    assert len(llm.history) == 4


def test_research_argument_flows_to_critic_formalizer_and_alignment(tmp_path, monkeypatch):
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
    write_lean_environment(tmp_path)
    journal = tmp_path / "integrated.sqlite"
    proof_id = "integrated-cli"
    initialize(journal, proof_id, "True", "Give a complete argument for the theorem")
    llm = MockLLMClient(responses=[
        {
            "moves": [],
            "candidate_argument": {
                "claim_id": f"{proof_id}/theorem",
                "argument": "True.intro is a direct witness of True.",
                "prerequisite_claim_ids": [],
            },
            "reasoning": "The theorem has a direct complete argument.",
        },
        {
            "verdict": "supported", "reasoning": "True.intro establishes True",
            "earliest_gap": None, "repair": None, "evidence_ids": [],
        },
        {"lean_type": "True", "imports": ["Init"], "reasoning": "Literal statement"},
        {"verdict": "aligned", "relation": "exact", "reasoning": "Both state True"},
    ])
    events = []

    result = run_proof(
        journal, proof_id, rounds=2, backend="memory", lean_project=tmp_path, llm=llm,
        event_sink=lambda event, fields: events.append({"event": event, **fields}),
    )

    assert [slot["slot"] for slot in result["reports"][0]["slots"]] == ["explore"]
    assert [slot["slot"] for slot in result["reports"][1]["slots"]] == [
        "critic", "formalize", "alignment",
    ]
    view = MemoryView()
    project(view, JournalStore(str(journal)), proof_id)
    claim = view.node(f"{proof_id}/theorem")
    assert claim.fields["status"] == "critic-accepted"
    assert claim.fields["argument"] == "True.intro is a direct witness of True."
    assert view.node(f"{proof_id}/at-1").fields["status"] == "supported"
    assert view.node(f"{proof_id}/fd-1").fields["status"] == "aligned"
    assert len(llm.history) == 4
    completed = [event for event in events if event["event"] == "worker_completed"]
    assert next(
        event for event in completed if event["worker_class"] == "formalizer"
    )["result"] == "formal_declaration_drafted"
    alignment = next(
        event for event in completed if event["worker_class"] == "alignment-reviewer"
    )
    assert alignment["verdict"] == "aligned"
    assert alignment["statement_checked"] is True


def test_fake_formal_mode_completes_certificate_and_replay_cycle(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    from mathproof.lean_statement import StatementCheck
    from coordinator import cli

    class Checked:
        def __init__(self, project):
            assert project == tmp_path

        def check(self, lean_type, imports):
            return StatementCheck(lean_type == "True" and imports == ["Init"], "elaborated")

    monkeypatch.setattr(cli, "LeanStatementChecker", Checked)
    write_lean_environment(tmp_path)
    journal = tmp_path / "fake-cycle.sqlite"
    proof_id = "fake-cycle"
    initialize(journal, proof_id, "True", "Give a complete argument for True")
    llm = MockLLMClient(responses=[
        {
            "moves": [],
            "candidate_argument": {
                "claim_id": f"{proof_id}/theorem",
                "argument": "True.intro establishes True.",
                "prerequisite_claim_ids": [],
            },
            "reasoning": "The theorem is immediate.",
        },
        {
            "verdict": "supported", "reasoning": "True.intro is sufficient",
            "earliest_gap": None, "repair": None, "evidence_ids": [],
        },
        {"lean_type": "True", "imports": ["Init"], "reasoning": "Literal statement"},
        {"verdict": "aligned", "relation": "exact", "reasoning": "Both state True"},
    ])

    result = run_proof(
        journal, proof_id, rounds=2, backend="memory", lean_project=tmp_path,
        llm=llm, fake_formal=True,
    )

    assert result["outcome"] == "simulated-complete"
    assert result["verification_mode"] == "simulated"
    assert "real replay is still required" in result["outcome_reason"]
    view = MemoryView()
    project(view, JournalStore(str(journal)), proof_id)
    assert view.node(f"{proof_id}/theorem").fields["status"] == "critic-accepted"
    assert view.node(f"{proof_id}/fd-1").fields["status"] == "certificate-produced"
    assert view.node(f"{proof_id}/cert-1").fields["status"] == "replay-pending"
    replay = view.node(f"{proof_id}/lr-1")
    assert replay.fields["status"] == "verified"
    assert replay.fields["simulated"] is True
    assert view.edges_from(f"{proof_id}/fr-1", "RAN_UNDER")[0].dst_id == f"{proof_id}/env-1"
    assert view.edges_from(f"{proof_id}/cert-1", "CERTIFICATE_ENVIRONMENT")[0].dst_id == f"{proof_id}/env-1"
    assert view.edges_from(f"{proof_id}/lr-1", "REPLAY_ENVIRONMENT")[0].dst_id == f"{proof_id}/env-1"
    assert view.edges_from(f"{proof_id}/theorem", "PROVED_BY")[0].dst_id == f"{proof_id}/cert-1"


def test_test_mode_reaches_terminal_path_without_claiming_real_verification(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    from mathproof.lean_statement import StatementCheck
    from coordinator import cli

    class Checked:
        def __init__(self, project):
            assert project == tmp_path

        def check(self, lean_type, imports):
            return StatementCheck(lean_type == "True" and imports == ["Init"], "elaborated")

    monkeypatch.setattr(cli, "LeanStatementChecker", Checked)
    write_lean_environment(tmp_path)
    journal = tmp_path / "test-mode.sqlite"
    proof_id = "test-mode"
    initialize(journal, proof_id, "True", "Give a complete argument for True")
    llm = MockLLMClient(responses=[
        {
            "moves": [],
            "candidate_argument": {
                "claim_id": f"{proof_id}/theorem",
                "argument": "True.intro establishes True.",
                "prerequisite_claim_ids": [],
            },
            "reasoning": "The theorem is immediate.",
        },
        {
            "verdict": "supported", "reasoning": "True.intro is sufficient",
            "earliest_gap": None, "repair": None, "evidence_ids": [],
        },
        {"lean_type": "True", "imports": ["Init"], "reasoning": "Literal statement"},
        {"verdict": "aligned", "relation": "exact", "reasoning": "Both state True"},
    ])

    result = run_proof(
        journal, proof_id, rounds=2, backend="memory", lean_project=tmp_path,
        llm=llm, test_mode=True,
    )

    assert result["outcome"] == "test-verified"
    assert result["verification_mode"] == "test"
    assert "no real Lean proof" in result["outcome_reason"]
    view = MemoryView()
    project(view, JournalStore(str(journal)), proof_id)
    claim = view.node(f"{proof_id}/theorem")
    declaration = view.node(f"{proof_id}/fd-1")
    certificate = view.node(f"{proof_id}/cert-1")
    replay = view.node(f"{proof_id}/lr-1")
    assert claim.fields["status"] == "lean-verified"
    assert claim.fields["verification_mode"] == "test"
    assert declaration.fields["status"] == "replay-accepted"
    assert declaration.fields["verification_mode"] == "test"
    assert certificate.fields["status"] == "replay-accepted"
    assert certificate.fields["test_mode"] is True
    assert replay.fields["status"] == "verified"
    assert replay.fields["test_mode"] is True


def test_real_atp_registration_polls_and_records_exhaustion(tmp_path, monkeypatch):
    from coordinator import cli
    from mathproof.formal_atp import build_result
    from mathproof.lean_statement import StatementCheck
    from mathproof.schemas import validate

    class Checked:
        def __init__(self, project):
            assert project == tmp_path

        def check(self, lean_type, imports):
            return StatementCheck(True, "elaborated")

    class Provider:
        def __init__(self):
            self.request = None
            self.polled = False

        def formal_search_start(self, request):
            self.request = request
            return {
                "run_id": request["run_id"], "proof_id": request["proof_id"],
                "declaration_id": request["formal_declaration_id"],
                "environment_hash": request["environment_hash"],
                "disposition": "searching", "states": [], "tactic_edges": [],
                "artifacts": [],
            }

        def formal_search_status(self, run_id):
            assert run_id == self.request["run_id"]
            self.polled = True
            return build_result("stagnated", self.request)

    provider = Provider()
    monkeypatch.setattr(cli, "LeanStatementChecker", Checked)
    monkeypatch.setattr("mathproof.maths_ai_http_atp", lambda url: provider)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    write_lean_environment(tmp_path)
    journal = tmp_path / "real-atp.sqlite"
    proof_id = "real-atp"
    initialize(journal, proof_id, "True", "Find a direct argument")
    llm = MockLLMClient(responses=[
        {"moves": [], "candidate_argument": {
            "claim_id": f"{proof_id}/theorem", "argument": "True.intro proves True.",
            "prerequisite_claim_ids": [],
        }, "reasoning": "Direct argument"},
        {"verdict": "supported", "reasoning": "Valid", "earliest_gap": None,
         "repair": None, "evidence_ids": []},
        {"lean_type": "True", "imports": ["Init"], "reasoning": "Literal"},
        {"verdict": "aligned", "relation": "exact", "reasoning": "Same proposition"},
    ])
    result = run_proof(
        journal, proof_id, rounds=3, backend="memory", lean_project=tmp_path,
        llm=llm, formal_atp_url="http://localhost:8001",
    )
    assert provider.polled, repr(result["reports"][-1]["slots"])
    assert validate("formal-search-request", provider.request) == []
    assert result["verification_mode"] == "not-replayed"
    assert result["outcome"] != "verified"
