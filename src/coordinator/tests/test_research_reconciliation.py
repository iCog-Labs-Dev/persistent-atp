"""Conjunctive research alternatives converge through journaled maintenance."""

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from coordinator.research import reconcile_research
from mathproof.scheduler import GlobalScheduler
from mathproof.worker_context import compile_context


def _seed(statuses):
    proof_id = "p1"
    store, view = JournalStore(), MemoryView()
    view.add_node("p1/proof", "Proof", {"title": "Parent theorem"})
    view.add_node("p1/c-1", "Claim", {
        "status": "conjectural", "claim_text": "Parent claim",
    })
    view.add_node("p1/rs-1", "ResearchState", {
        "status": "open", "target_claim_id": "p1/c-1", "branch_depth": 0,
    })
    view.add_node("p1/rm-1", "ResearchMove", {
        "status": "closed", "state_id": "p1/rs-1", "detail": "Alternative A",
    })
    view.add_node("p1/rm-4", "ResearchMove", {
        "status": "queued", "state_id": "p1/rs-1", "detail": "Alternative B",
    })
    view.add_edge("HAS_TARGET", "p1/proof", "p1/c-1", "p1/target")
    view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/proposes-1")
    view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-4", "p1/proposes-4")
    view.add_edge("MOVE_TARGETS", "p1/rm-1", "p1/c-1", "p1/targets-1")
    view.add_edge("MOVE_TARGETS", "p1/rm-4", "p1/c-1", "p1/targets-4")
    for index, status in enumerate(statuses, start=2):
        child_move = f"p1/rm-{index}"
        child_claim = f"p1/c-{index}"
        view.add_node(child_claim, "Claim", {
            "status": status, "claim_text": f"Required subgoal {index}",
        })
        view.add_node(child_move, "ResearchMove", {
            "status": "closed", "state_id": f"p1/rs-{index}",
        })
        view.add_edge(
            "MOVE_TARGETS", child_move, child_claim, f"p1/child-target-{index}"
        )
        view.add_edge(
            "REQUIRES_SUBGOAL", "p1/rm-1", child_move,
            f"p1/requires-{index}", {"why_needed": "Needed by alternative A"},
        )
    return store, view


def test_accepted_subgoals_queue_parent_synthesis_with_prerequisites():
    store, view = _seed(("critic-accepted", "formally-closed"))

    result = reconcile_research(store, view, "p1")

    assert result is not None and result.accepted, result.rejections
    view.apply_journal_event(store, "p1", result.revision)
    synthesis = next(
        move for move in view.list_nodes("p1", "ResearchMove")
        if move.fields.get("synthesis_of") == "p1/rm-1"
    )
    assert synthesis.fields["status"] == "queued"
    assert synthesis.fields["prerequisite_claim_ids"] == ["p1/c-2", "p1/c-3"]
    assert view.node("p1/rm-1").fields["alternative_status"] == "synthesis-queued"
    lease = type("Lease", (), {
        "proof_id": "p1", "worker_class": "llm-research",
        "selected_move_id": synthesis.node_id,
        "base_revision": store.head("p1")[0], "lease_id": "p1/ls-read",
    })()
    packet = compile_context(lease, view)
    assert [claim["id"] for claim in packet["prerequisite_claims"]] == [
        "p1/c-2", "p1/c-3",
    ]
    assert store.read_events("p1")[-1]["worker_class"] == "maintenance"


def test_refuted_subgoal_disables_only_its_parent_alternative():
    store, view = _seed(("refuted", "critic-accepted"))

    result = reconcile_research(store, view, "p1")

    assert result is not None and result.accepted, result.rejections
    view.apply_journal_event(store, "p1", result.revision)
    parent = view.node("p1/rm-1")
    assert parent.fields["alternative_status"] == "unusable"
    assert parent.fields["refuted_subgoal_ids"] == ["p1/c-2"]
    frontier = {candidate.move_id for candidate in GlobalScheduler(view, store).build_frontier("p1")}
    assert "p1/rm-4" in frontier
    assert not any(
        move.fields.get("synthesis_of") == "p1/rm-1"
        for move in view.list_nodes("p1", "ResearchMove")
    )
