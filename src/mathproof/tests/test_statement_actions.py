"""A typed follow-up reaches Lean and feeds its diagnostic back to workers."""

import pytest

from commit_gate.gate import CommitGate
from commit_gate.ops import SetField
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.validate import check_worker_authority
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.lean_statement import StatementCheck
from mathproof.scheduler import GlobalScheduler
from mathproof.workers import CriticWorker, ResearchWorker
from shared.llm_client import MockLLMClient


class Checker:
    def __init__(self, ok: bool):
        self.ok = ok
        self.calls = []

    def check(self, lean_type, imports):
        self.calls.append((lean_type, imports))
        return StatementCheck(self.ok, "elaborated" if self.ok else "unknown type")


def setup(*, research: bool):
    view, store = MemoryView(), JournalStore()
    view.add_node("p1/c-1", "Claim", {
        "status": "provisional", "claim_text": "For all n : Nat, n + 0 = n",
        "proof_sketch": "Induct on n; the successor step needs checking.",
    })
    view.add_node("p1/fd-1", "FormalDeclaration", {
        "status": "draft", "lean_type": "∀ n : Nat, n + 0 = n",
        "imports": ["Init"], "statement_source": "theorem add_zero : ∀ n : Nat, n + 0 = n",
    })
    view.add_edge("DRAFTS_CLAIM", "p1/fd-1", "p1/c-1", "p1/draft")
    if research:
        view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
        view.add_node("p1/rm-1", "ResearchMove", {"status": "queued", "detail": "Check the statement", "state_id": "p1/rs-1"})
        view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/proposes")
        view.add_edge("MOVE_TARGETS", "p1/rm-1", "p1/c-1", "p1/targets")
    gate, scheduler = CommitGate(view, store), GlobalScheduler(view, store)
    maintenance = lambda proposal, commit: view.apply_journal_event(store, "p1", commit.revision)
    return view, store, gate, scheduler, maintenance


def test_research_action_executes_and_records_lean_diagnostic():
    view, store, gate, scheduler, maintenance = setup(research=True)
    llm = MockLLMClient(default_json={
        "moves": [{
            "detail": "Elaborate the draft statement", "why_it_helps": "It tests the Lean types",
            "first_test": "Run Lean elaboration", "technique_tag": "lean-elaboration",
            "prerequisite_claim_ids": ["p1/c-1"],
            "action": {"kind": "lean_statement_check", "declaration_id": "p1/fd-1"},
        }],
        "reasoning": "Check the statement before proof search",
    })
    research = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"llm-research": ResearchWorker(llm).run}),
        worker_class="llm-research", maintenance=maintenance,
    )
    assert research.accepted, research.rejections
    assert view.node("p1/rm-2").fields["action"]["declaration_id"] == "p1/fd-1"
    frontier = scheduler.build_frontier("p1")
    assert [c.move_id for c in frontier if c.category == "statement-check"] == ["p1/rm-2"]
    assert "p1/rm-2" not in [c.move_id for c in frontier if c.category == "research-move"]

    checker = Checker(True)
    execution = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="experiment",
        adapters={"lean-statement-checker": checker}, maintenance=maintenance,
    )
    assert execution.accepted, execution.rejections
    assert checker.calls == [("∀ n : Nat, n + 0 = n", ["Init"])]
    assert view.node("p1/ex-1").fields["success"] is True
    assert view.edges_from("p1/ex-1", "CHECKS_CLAIM")[0].dst_id == "p1/c-1"
    assert view.node("p1/rm-2").fields["status"] == "closed"
    assert view.node("p1/rm-2").fields["action_result_id"] == "p1/ex-1"
    assert view.node("p1/c-1").fields["status"] == "provisional"
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "statement-check"]
    assert store.verify_chain("p1") == 2


def test_critic_waits_for_action_then_reads_result_once():
    view, store, gate, scheduler, maintenance = setup(research=False)
    first = MockLLMClient(default_json={
        "verdict": "pending", "reasoning": "The Lean statement may not elaborate",
        "earliest_gap": "The formal type is untested", "repair": "Elaborate the draft",
        "evidence_ids": [],
        "follow_up": {"kind": "lean_statement_check", "declaration_id": "p1/fd-1"},
    })
    critique = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"critic": CriticWorker(first).run}),
        worker_class="critic", maintenance=maintenance,
    )
    assert critique.accepted, critique.rejections
    assert "p1/fd-1" in first.history[0][1]
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "critic-task"]
    assert [c.move_id for c in scheduler.build_frontier("p1") if c.category == "statement-check"] == ["p1/at-1"]

    execution = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="experiment",
        adapters={"lean-statement-checker": Checker(False)}, maintenance=maintenance,
    )
    assert execution.accepted, execution.rejections
    assert view.node("p1/ex-1").fields["diagnostics"] == "unknown type"
    assert view.node("p1/at-1").fields["action_result_id"] == "p1/ex-1"
    assert [c.move_id for c in scheduler.build_frontier("p1") if c.category == "critic-task"] == ["p1/c-1"]

    second = MockLLMClient(default_json={
        "verdict": "pending", "reasoning": "Lean reports an unknown type",
        "earliest_gap": "Unknown type in the draft", "repair": "Correct the type",
        "evidence_ids": ["p1/ex-1"],
    })
    recheck = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"critic": CriticWorker(second).run}),
        worker_class="critic", maintenance=maintenance,
    )
    assert recheck.accepted, recheck.rejections
    assert "unknown type" in second.history[0][1]
    assert view.node("p1/at-2").fields["evidence_ids"] == ["p1/ex-1"]
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "critic-task"]
    assert store.verify_chain("p1") == 3


def test_unknown_declaration_action_is_rejected_before_commit():
    view, store, gate, scheduler, _ = setup(research=True)
    llm = MockLLMClient(default_json={
        "moves": [{
            "detail": "Check another proof", "why_it_helps": "Maybe useful",
            "first_test": "Run Lean", "technique_tag": "lean",
            "prerequisite_claim_ids": [],
            "action": {"kind": "lean_statement_check", "declaration_id": "p2/fd-1"},
        }],
        "reasoning": "Try it",
    })
    with pytest.raises(ValueError, match="unknown draft declaration"):
        run_cycle(
            "p1", view=view, gate=gate, scheduler=scheduler,
            dispatcher=ScriptedDispatcher({"llm-research": ResearchWorker(llm).run}),
            worker_class="llm-research",
        )
    assert store.head("p1")[0] == 0
    assert not store.active_leases("p1")


def test_pending_critic_reopens_only_after_new_evidence():
    view, store, gate, scheduler, maintenance = setup(research=False)
    llm = MockLLMClient(default_json={
        "verdict": "pending", "reasoning": "The argument has a gap",
        "earliest_gap": "Successor case", "repair": "Supply the calculation",
        "evidence_ids": [],
    })
    first = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"critic": CriticWorker(llm).run}),
        worker_class="critic", maintenance=maintenance,
    )
    assert first.accepted, first.rejections
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "critic-task"]

    view.add_node("p1/ex-1", "Experiment", {
        "kind": "lean_statement_check", "success": False, "diagnostics": "unknown type",
    })
    view.add_edge("CHECKS_CLAIM", "p1/ex-1", "p1/c-1", "p1/ex-1-checks")
    assert [c.move_id for c in scheduler.build_frontier("p1") if c.category == "critic-task"] == ["p1/c-1"]


def test_new_diagnostic_invalidates_a_supported_opinion():
    view, store, gate, scheduler, maintenance = setup(research=False)
    llm = MockLLMClient(default_json={
        "verdict": "supported", "reasoning": "The sketch appears complete",
        "earliest_gap": None, "repair": None, "evidence_ids": [],
    })
    first = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"critic": CriticWorker(llm).run}),
        worker_class="critic", maintenance=maintenance,
    )
    assert first.accepted, first.rejections
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "critic-task"]
    view.add_node("p1/ex-1", "Experiment", {
        "kind": "lean_statement_check", "success": False, "diagnostics": "unknown type",
    })
    view.add_edge("CHECKS_CLAIM", "p1/ex-1", "p1/c-1", "p1/ex-1-checks")
    frontier = scheduler.build_frontier("p1")
    assert [c.move_id for c in frontier if c.category == "critic-task"] == ["p1/c-1"]


def test_stale_declaration_is_not_dispatched_as_an_action():
    view, _, _, scheduler, _ = setup(research=True)
    view.set_field("p1/rm-1", "action", {
        "kind": "lean_statement_check", "declaration_id": "p1/fd-1",
    })
    view.set_field("p1/fd-1", "status", "stale")
    assert "p1/rm-1" not in [candidate.move_id for candidate in scheduler.build_frontier("p1")]


def test_experiment_cannot_close_a_move_without_recording_a_result():
    proposal = Proposal(
        proof_id="p1", actor="experiment", worker_class="experiment",
        ops=(SetField("ResearchMove", "p1/rm-1", "status", "closed", prior="queued"),),
    )
    assert list(check_worker_authority(proposal))
