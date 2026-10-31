"""Research and critic workers through the shared context compiler and gate."""

from types import SimpleNamespace

import pytest

from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.worker_context import compile_context
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.scheduler import GlobalScheduler
from mathproof.workers import CriticWorker, ResearchWorker
from shared.llm_client import MockLLMClient


def seed(view: MemoryView, proof_id: str, *, argument: str | None = None) -> None:
    view.add_node(f"{proof_id}/rs-1", "ResearchState", {"status": "open"})
    view.add_node(f"{proof_id}/rm-1", "ResearchMove", {
        "status": "queued", "detail": "Investigate addition by zero",
        "state_id": f"{proof_id}/rs-1",
    })
    claim_fields = {"status": "provisional", "claim_text": "For all n : Nat, n + 0 = n"}
    if argument is not None:
        claim_fields["proof_sketch"] = argument
    view.add_node(f"{proof_id}/c-1", "Claim", claim_fields)
    view.add_edge("PROPOSES", f"{proof_id}/rs-1", f"{proof_id}/rm-1", f"{proof_id}/e-1")
    view.add_edge("MOVE_TARGETS", f"{proof_id}/rm-1", f"{proof_id}/c-1", f"{proof_id}/e-2")


def test_compiler_scopes_context_and_preserves_theorem_target():
    view = MemoryView()
    seed(view, "p1")
    seed(view, "p2")
    view.add_node("p1/proof", "Proof", {
        "title": "Addition by zero", "assumptions": ["n is a natural number"]
    })
    view.add_edge("HAS_TARGET", "p1/proof", "p1/c-1", "p1/e-3")
    view.add_node("p1/fd-1", "FormalDeclaration", {
        "status": "aligned", "lean_type": "∀ n : Nat, n + 0 = n"
    })
    view.add_node("p1/fd-2", "FormalDeclaration", {
        "status": "aligned", "lean_type": "An unrelated statement"
    })
    view.add_node("p1/al-1", "Alignment", {"verdict": "aligned", "lifecycle": "reviewed"})
    view.add_edge("ALIGNS_CLAIM", "p1/al-1", "p1/c-1", "p1/e-4")
    view.add_edge("ALIGNS_DECLARATION", "p1/al-1", "p1/fd-1", "p1/e-5")
    lease = SimpleNamespace(
        proof_id="p1", worker_class="llm-research", selected_move_id="p1/rm-1",
        base_revision=0, lease_id="p1/ls-1",
    )
    packet = compile_context(lease, view)
    assert packet["theorem"]["target_claims"] == [{"id": "p1/c-1"}]
    assert packet["theorem"]["statement"] == "For all n : Nat, n + 0 = n"
    assert "For all n : Nat, n + 0 = n" in packet["context_text"]
    assert "n is a natural number" in packet["context_text"]
    assert packet["manifest_digest"].startswith("sha256:")
    assert packet["revision"] == 0
    assert all(item["id"].startswith("p1/") for item in packet["claims"])
    assert "p2/" not in str(packet)


def test_research_and_critic_run_through_one_cycle_without_promoting_claim():
    proof_id = "p1"
    view, store = MemoryView(), JournalStore()
    seed(view, proof_id, argument="Induct on n, but the successor step is not shown.")
    research_llm = MockLLMClient(default_json={
        "moves": [{
            "detail": "Prove the successor step for Nat.add_zero",
            "why_it_helps": "It fills the missing induction step",
            "first_test": "Check the successor case in Lean",
            "technique_tag": "induction",
            "prerequisite_claim_ids": ["p1/c-1"],
        }],
        "reasoning": "The induction step is the first unresolved obligation",
    })
    critic_llm = MockLLMClient(default_json={
        "verdict": "pending",
        "reasoning": "The successor case has no derivation",
        "earliest_gap": "Successor step after the induction hypothesis",
        "repair": "Supply the successor case and check it in Lean",
        "evidence_ids": [],
    })
    dispatcher = ScriptedDispatcher({
        "llm-research": ResearchWorker(research_llm).run,
        "critic": CriticWorker(critic_llm).run,
    })
    gate = CommitGate(view, store)
    scheduler = GlobalScheduler(view, store)
    maintenance = lambda proposal, commit: view.apply_journal_event(
        store, proof_id, commit.revision
    )
    research = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler, dispatcher=dispatcher,
        worker_class="llm-research", actor="research-1", category="research-move",
        maintenance=maintenance,
    )
    assert research.accepted, research.rejections
    assert view.node("p1/rm-1").fields["status"] == "closed"
    assert view.node("p1/rm-1").fields["model_output"]["moves"][0]["technique_tag"] == "induction"
    assert view.node("p1/rm-2").fields["first_test"] == "Check the successor case in Lean"
    assert view.node("p1/rm-2").fields["state_id"] == "p1/rs-1"
    assert view.edges_from("p1/rm-2", "MOVE_TARGETS")[0].dst_id == "p1/c-1"
    assert "p1/c-1" in research_llm.history[0][1]
    next_packet = compile_context(SimpleNamespace(
        proof_id="p1", worker_class="llm-research", selected_move_id="p1/rm-2",
        base_revision=store.head("p1")[0], lease_id="p1/ls-next",
    ), view)
    assert "p1/rm-2" in next_packet["context_text"]

    critique = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler, dispatcher=dispatcher,
        worker_class="critic", actor="critic-1", category="critic-task",
        maintenance=maintenance,
    )
    assert critique.accepted, critique.rejections
    assert store.verify_chain(proof_id) == 2
    assert view.node("p1/c-1").fields["status"] == "provisional"
    attempt = view.node("p1/at-1")
    assert attempt.fields["earliest_gap"] == "Successor step after the induction hypothesis"
    assert attempt.fields["repair"] == "Supply the successor case and check it in Lean"
    assert attempt.fields["context_digest"]
    assert attempt.fields["model_output"]["earliest_gap"] == attempt.fields["earliest_gap"]
    assert "p1/c-1" in critic_llm.history[0][1]


def test_research_alternatives_create_conjunctive_subgoals():
    view, store = MemoryView(), JournalStore()
    seed(view, "p1")
    answer = {
        "moves": [
            {
                "detail": "Use induction", "why_it_helps": "Split the theorem",
                "first_test": "Check the base case", "technique_tag": "induction",
                "prerequisite_claim_ids": [],
                "subgoals": [
                    {"statement": "Base case", "why_needed": "Induction base"},
                    {"statement": "Successor case", "why_needed": "Induction step"},
                ],
            },
            {
                "detail": "Use rewriting", "why_it_helps": "Try an equality lemma",
                "first_test": "Search for a rewrite", "technique_tag": "rewriting",
                "prerequisite_claim_ids": [],
                "subgoals": [{"statement": "Base case", "why_needed": "Shared supporting fact"}],
            },
            {
                "detail": "Use direct algebra", "why_it_helps": "Avoid induction",
                "first_test": "Try a direct simplification", "technique_tag": "algebra",
                "prerequisite_claim_ids": [],
            },
        ],
        "reasoning": "Two distinct strategies",
    }
    gate = CommitGate(view, store)
    scheduler = GlobalScheduler(view, store)
    result = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"llm-research": ResearchWorker(MockLLMClient(default_json=answer)).run}),
        maintenance=lambda proposal, commit: view.apply_journal_event(store, "p1", commit.revision),
    )
    assert result.accepted, result.rejections
    alternatives = [view.node(f"p1/rm-{i}") for i in (2, 5, 6)]
    assert [move.fields["status"] for move in alternatives] == ["closed", "closed", "queued"]
    children = view.edges_from("p1/rm-2", "REQUIRES_SUBGOAL")
    assert [edge.dst_id for edge in children] == ["p1/rm-3", "p1/rm-4"]
    assert view.edges_from("p1/rm-5", "REQUIRES_SUBGOAL")[0].dst_id == "p1/rm-3"
    assert [view.node(f"p1/c-{i}").fields["status"] for i in (2, 3)] == ["conjectural"] * 2
    assert view.node("p1/c-1").fields["status"] == "provisional"
    assert [view.node(f"p1/rs-{i}").fields["branch_depth"] for i in (2, 3)] == [1, 1]
    frontier = {item.move_id for item in scheduler.build_frontier("p1")}
    assert {"p1/rm-3", "p1/rm-4", "p1/rm-6"} <= frontier
    assert not {"p1/rm-2", "p1/rm-5"} & frontier
    child_packet = compile_context(SimpleNamespace(
        proof_id="p1", worker_class="llm-research", selected_move_id="p1/rm-3",
        base_revision=store.head("p1")[0], lease_id="p1/ls-child",
    ), view)
    assert child_packet["parent_state"]["branch_depth"] == 1
    assert "Base case" in child_packet["context_text"]


def test_research_cannot_expand_subgoals_beyond_depth_limit():
    view, store = MemoryView(), JournalStore()
    seed(view, "p1")
    view.set_field("p1/rs-1", "branch_depth", 2)
    worker = ResearchWorker(MockLLMClient(default_json={
        "moves": [{
            "detail": "Split again", "why_it_helps": "Try a smaller lemma",
            "first_test": "Check the lemma", "technique_tag": "decompose",
            "prerequisite_claim_ids": [],
            "subgoals": [{"statement": "A smaller lemma", "why_needed": "Bridge step"}],
        }],
        "reasoning": "Further decomposition",
    }))
    with pytest.raises(ValueError, match="branch depth"):
        run_cycle(
            "p1", view=view, gate=CommitGate(view, store),
            scheduler=GlobalScheduler(view, store),
            dispatcher=ScriptedDispatcher({"llm-research": worker.run}),
        )
    assert store.head("p1")[0] == 0
    assert not store.active_leases("p1")


def test_research_cannot_repeat_its_target_as_a_subgoal():
    view = MemoryView()
    seed(view, "p1")
    lease = SimpleNamespace(
        proof_id="p1", worker_class="llm-research", selected_move_id="p1/rm-1",
        base_revision=0, lease_id="p1/ls-1",
    )
    packet = compile_context(lease, view)
    worker = ResearchWorker(MockLLMClient(default_json={
        "moves": [{
            "detail": "Repeat target", "why_it_helps": "Try again",
            "first_test": "Inspect it", "technique_tag": "repeat",
            "prerequisite_claim_ids": [],
            "subgoals": [{
                "statement": "For all n : Nat, n + 0 = n", "why_needed": "Circular step",
            }],
        }],
        "reasoning": "No smaller goal",
    }))
    with pytest.raises(ValueError, match="repeats the current theorem target"):
        worker.run(lease, packet)


def test_critic_downgrades_unsupported_opinion_and_rejects_invented_citations():
    view = MemoryView()
    seed(view, "p1")
    lease = SimpleNamespace(
        proof_id="p1", worker_class="critic", selected_move_id="p1/c-1",
        base_revision=0, lease_id="p1/ls-1",
    )
    packet = compile_context(lease, view)
    opinion = {
        "verdict": "supported", "reasoning": "Looks correct", "earliest_gap": None,
        "repair": None, "evidence_ids": [],
    }
    result = CriticWorker(MockLLMClient(default_json=opinion)).run(lease, packet)
    assert result["verdict"] == "pending"
    assert result["reported_verdict"] == "supported"
    with pytest.raises(ValueError, match="unknown evidence ID"):
        CriticWorker(MockLLMClient(default_json={**opinion, "evidence_ids": ["p2/c-1"]})).run(
            lease, packet
        )


def test_invalid_worker_output_releases_lease_without_journal_write():
    view, store = MemoryView(), JournalStore()
    seed(view, "p1")
    bad = ResearchWorker(MockLLMClient(default_json={
        "moves": [{
            "detail": "Try induction", "why_it_helps": "Maybe useful",
            "first_test": "Check base case", "technique_tag": "induction",
            "prerequisite_claim_ids": ["p2/c-1"],
        }],
        "reasoning": "Try it",
    }))
    with pytest.raises(ValueError, match="unknown prerequisite"):
        run_cycle(
            "p1", view=view, gate=CommitGate(view, store),
            scheduler=GlobalScheduler(view, store),
            dispatcher=ScriptedDispatcher({"llm-research": bad.run}),
            worker_class="llm-research", category="research-move",
        )
    assert store.head("p1")[0] == 0
    assert store.active_leases("p1") == []
