"""Focused checks for research and critic workers and their commit path."""

from types import SimpleNamespace

import pytest

from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.scheduler import GlobalScheduler
from mathproof.worker_cycle import run_worker_cycle
from mathproof.workers import CriticWorker, ResearchWorker, WorkerDispatcher
from shared.llm_client import MockLLMClient


def setup_cycle(responses):
    view, store = MemoryView(), JournalStore()
    view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
    view.add_node("p1/rm-1", "ResearchMove", {"status": "queued", "detail": "look for a lemma"})
    view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/e-1")
    view.add_node("p1/c-1", "Claim", {"status": "provisional", "claim_text": "n + 0 = n"})
    view.add_node("p1/c-2", "Claim", {"status": "conjectural", "claim_text": "0 is neutral"})
    view.add_edge("DEPENDS_ON", "p1/c-1", "p1/c-2", "p1/e-2")
    view.add_node("p2/c-1", "Claim", {"status": "provisional", "claim_text": "other proof"})
    llm = MockLLMClient(responses=responses)
    return (
        view, store, llm,
        GlobalScheduler(view, store),
        WorkerDispatcher(ResearchWorker(llm), CriticWorker(llm)),
        CommitGate(view, store),
    )


def test_roles_share_client_but_use_distinct_prompts_and_validate_output():
    llm = MockLLMClient(responses=[
        {"next_move": "try induction", "reasoning": "the statement is recursive"},
        {"verdict": "pending", "reasoning": "no derivation was supplied"},
    ])
    dispatcher = WorkerDispatcher(ResearchWorker(llm), CriticWorker(llm))
    dispatcher.run(SimpleNamespace(worker_class="llm-research", selected_move_id="p1/rm-1"), {})
    dispatcher.run(SimpleNamespace(worker_class="critic", selected_move_id="p1/c-1"), {})
    assert llm.history[0][0] != llm.history[1][0]
    with pytest.raises(ValueError, match="invalid critic verdict"):
        CriticWorker(MockLLMClient(default_json={"verdict": "lean-verified", "reasoning": "x"})).run(
            SimpleNamespace(worker_class="critic", selected_move_id="p1/c-1"), {}
        )


def test_research_and_critic_commit_under_their_own_leases():
    view, store, llm, scheduler, dispatcher, gate = setup_cycle([
        {"next_move": "try induction", "reasoning": "recursive structure"},
        {"verdict": "supported", "reasoning": "the algebraic argument looks sound"},
    ])
    research = run_worker_cycle(
        "p1", "research-move", actor="research-1",
        scheduler=scheduler, dispatcher=dispatcher, gate=gate,
    )
    assert research.commit.accepted, research.commit.rejections
    assert research.lease.selected_move_id == "p1/rm-1"
    assert {c.move_id for c in scheduler.build_frontier("p1")} == {"p1/rm-2", "p1/c-1"}

    critic = run_worker_cycle(
        "p1", "critic-task", actor="critic-1",
        scheduler=scheduler, dispatcher=dispatcher, gate=gate,
    )
    assert critic.commit.accepted, critic.commit.rejections
    assert critic.lease.selected_move_id == "p1/c-1"
    assert store.verify_chain("p1") == 2
    assert store.head("p2")[0] == 0
    assert {event["worker_class"] for event in store.read_events("p1")} == {"llm-research", "critic"}
    critic_ops = store.read_events("p1")[1]["ops"]
    assert {op.get("label") for op in critic_ops if "label" in op} == {"Attempt"}
    assert "p1/c-1" not in {c.move_id for c in scheduler.build_frontier("p1")}
    assert llm.history[0][0] != llm.history[1][0]
    assert "0 is neutral" in llm.history[1][1]
    assert "other proof" not in llm.history[1][1]


def test_invalid_llm_output_releases_lease_without_journal_mutation():
    view, store, _, scheduler, _, gate = setup_cycle([])
    bad_llm = MockLLMClient(default_json={"next_move": "", "reasoning": "unclear"})
    dispatcher = WorkerDispatcher(ResearchWorker(bad_llm), CriticWorker(bad_llm))
    with pytest.raises(ValueError, match="next_move"):
        run_worker_cycle(
            "p1", "research-move", actor="research-1",
            scheduler=scheduler, dispatcher=dispatcher, gate=gate,
        )
    assert store.head("p1")[0] == 0
    assert store.active_leases("p1") == []


def test_exhausted_research_move_does_not_queue_an_empty_followup():
    _, store, _, scheduler, _, gate = setup_cycle([])
    llm = MockLLMClient(default_json={"next_move": None, "reasoning": "no viable next step"})
    dispatcher = WorkerDispatcher(ResearchWorker(llm), CriticWorker(llm))
    result = run_worker_cycle(
        "p1", "research-move", actor="research-1",
        scheduler=scheduler, dispatcher=dispatcher, gate=gate,
    )
    assert result.commit.accepted, result.commit.rejections
    assert "p1/rm-1" not in {c.move_id for c in scheduler.build_frontier("p1")}
    assert not any(
        op.get("id") == "p1/rm-2"
        for op in store.read_events("p1")[0]["ops"]
    )
