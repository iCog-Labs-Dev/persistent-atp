"""Real FFI check for context-driven workers and live MORK projection."""

from uuid import uuid4

import pytest

from commit_gate.gate import CommitGate
from alignment.models import AlignmentCriteria, AlignmentReviewResult
from alignment.worker import AlignmentWorker
from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.scheduler import GlobalScheduler
from mathproof.workers import CriticWorker, FormalizerWorker, ResearchWorker
from mathproof.lean_statement import StatementCheck
from mork.backend import MorkSpace, MorkUnavailable, MorkView
from shared.llm_client import MockLLMClient


def test_real_mork_agent_cycle():
    try:
        view = MorkView(MorkSpace())
    except MorkUnavailable:
        pytest.skip("MORK library unavailable")

    proof_id = f"agent-{uuid4().hex[:12]}"
    store = JournalStore()
    gate = CommitGate(view, store)
    token = store.acquire_lease(
        proof_id, f"{proof_id}/seed", actor="seed", worker_class="maintenance"
    )
    seeded = gate.commit(Proposal(
        proof_id=proof_id, actor="seed", worker_class="maintenance",
        base_revision=0, lease_id=f"{proof_id}/seed", fencing_token=token,
        ops=(
            UpsertNode("ResearchState", f"{proof_id}/rs-1", {"status": "open"}),
            UpsertNode("ResearchMove", f"{proof_id}/rm-1", {
                    "status": "queued", "detail": "Investigate addition by zero",
                    "state_id": f"{proof_id}/rs-1",
            }),
            UpsertNode("Claim", f"{proof_id}/c-1", {
                "status": "provisional", "claim_text": "For all n : Nat, n + 0 = n"
            }),
            AddEdge("PROPOSES", f"{proof_id}/rs-1", f"{proof_id}/rm-1", f"{proof_id}/e-1"),
            AddEdge("MOVE_TARGETS", f"{proof_id}/rm-1", f"{proof_id}/c-1", f"{proof_id}/e-2"),
        ),
    ))
    assert seeded.accepted, seeded.rejections
    store.release_lease(proof_id, f"{proof_id}/seed", fencing_token=token)

    dispatcher = ScriptedDispatcher({
        "llm-research": ResearchWorker(MockLLMClient(default_json={
            "moves": [{
                "detail": "Check the successor case", "why_it_helps": "It completes induction",
                "first_test": "Run Lean on the successor case", "technique_tag": "induction",
                "prerequisite_claim_ids": [f"{proof_id}/c-1"],
            }],
            "reasoning": "The successor case is open",
        })).run,
        "critic": CriticWorker(MockLLMClient(default_json={
            "verdict": "supported", "reasoning": "Looks fine", "earliest_gap": None,
            "repair": None, "evidence_ids": [],
        })).run,
    })
    scheduler = GlobalScheduler(view, store)
    research = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler, dispatcher=dispatcher,
        worker_class="llm-research", category="research-move",
    )
    assert research.accepted, research.rejections
    review = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler, dispatcher=dispatcher,
        worker_class="critic", category="critic-task",
    )
    assert review.accepted, review.rejections
    assert view.projected_revision(proof_id) == store.head(proof_id)[0] == 3
    assert view.node(f"{proof_id}/rm-1").fields["status"] == "closed"
    assert view.node(f"{proof_id}/rm-1").fields["model_output"]["moves"][0]["technique_tag"] == "induction"
    assert view.node(f"{proof_id}/rm-2").fields["first_test"] == "Run Lean on the successor case"
    assert view.node(f"{proof_id}/at-1").fields["status"] == "pending"
    assert view.node(f"{proof_id}/c-1").fields["status"] == "provisional"


def test_real_mork_formalization_draft_and_alignment():
    try:
        view = MorkView(MorkSpace())
    except MorkUnavailable:
        pytest.skip("MORK library unavailable")
    proof_id = f"formalizer-{uuid4().hex[:12]}"
    store = JournalStore()
    gate = CommitGate(view, store)
    token = store.acquire_lease(
        proof_id, f"{proof_id}/seed", actor="seed", worker_class="maintenance"
    )
    seeded = gate.commit(Proposal(
        proof_id=proof_id, actor="seed", worker_class="maintenance",
        base_revision=0, lease_id=f"{proof_id}/seed", fencing_token=token,
        ops=(
            UpsertNode("Claim", f"{proof_id}/c-1", {
                "status": "provisional", "claim_text": "For all n : Nat, n + 0 = n"
            }),
            UpsertNode("Attempt", f"{proof_id}/at-1", {
                "status": "supported", "worker_class": "critic", "actor": "seed"
            }),
            AddEdge("REVIEWS_CLAIM", f"{proof_id}/at-1", f"{proof_id}/c-1", f"{proof_id}/reviewed"),
        ),
    ))
    assert seeded.accepted, seeded.rejections
    store.release_lease(proof_id, f"{proof_id}/seed", fencing_token=token)
    scheduler = GlobalScheduler(view, store)
    draft = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"formalizer": FormalizerWorker(MockLLMClient(default_json={
            "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"],
            "reasoning": "The Nat quantifier and equation match.",
        })).run}), worker_class="formalizer",
    )
    assert draft.accepted, draft.rejections

    class Reviewer:
        def review(self, request):
            assert request.claim_id == f"{proof_id}/c-1"
            return AlignmentReviewResult(
                verdict=AlignmentVerdict.ALIGNED, lifecycle=AlignmentLifecycle.REVIEWED,
                criteria=AlignmentCriteria(relation="exact"), reviewer="alignment-1",
                reasoning="Exact Nat quantifier and equation.",
            )

    class Checker:
        def check(self, lean_type, imports):
            assert lean_type == "∀ n : Nat, n + 0 = n"
            return StatementCheck(True, "elaborated")

    aligned = run_cycle(
        proof_id, view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="alignment-reviewer",
        actor="alignment-1", adapters={
            "alignment-reviewer": AlignmentWorker(actor="alignment-1", reviewer=Reviewer()),
            "lean-statement-checker": Checker(),
        },
    )
    assert aligned.accepted, aligned.rejections
    assert view.projected_revision(proof_id) == store.head(proof_id)[0] == 3
    assert view.edges_from(f"{proof_id}/fd-1", "DRAFTS_CLAIM")[0].dst_id == f"{proof_id}/c-1"
    assert view.node(f"{proof_id}/fd-1").fields["status"] == "aligned"
    assert view.node(f"{proof_id}/c-1").fields["status"] == "provisional"


def test_real_mork_critic_action_records_statement_diagnostic():
    try:
        view = MorkView(MorkSpace())
    except MorkUnavailable:
        pytest.skip("MORK library unavailable")
    proof_id = f"action-{uuid4().hex[:12]}"
    store = JournalStore()
    gate = CommitGate(view, store)
    token = store.acquire_lease(
        proof_id, f"{proof_id}/seed", actor="seed", worker_class="maintenance"
    )
    seeded = gate.commit(Proposal(
        proof_id=proof_id, actor="seed", worker_class="maintenance",
        base_revision=0, lease_id=f"{proof_id}/seed", fencing_token=token,
        ops=(
            UpsertNode("Claim", f"{proof_id}/c-1", {
                "status": "provisional", "claim_text": "For all n : Nat, n + 0 = n",
            }),
            UpsertNode("FormalDeclaration", f"{proof_id}/fd-1", {
                "status": "draft", "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"],
            }),
            UpsertNode("Attempt", f"{proof_id}/at-1", {
                "actor": "seed", "worker_class": "critic", "status": "pending",
                "follow_up": {"kind": "lean_statement_check", "declaration_id": f"{proof_id}/fd-1"},
            }),
            AddEdge("DRAFTS_CLAIM", f"{proof_id}/fd-1", f"{proof_id}/c-1", f"{proof_id}/draft"),
            AddEdge("REVIEWS_CLAIM", f"{proof_id}/at-1", f"{proof_id}/c-1", f"{proof_id}/review"),
        ),
    ))
    assert seeded.accepted, seeded.rejections
    store.release_lease(proof_id, f"{proof_id}/seed", fencing_token=token)

    class Checker:
        def check(self, lean_type, imports):
            assert lean_type == "∀ n : Nat, n + 0 = n"
            return StatementCheck(True, "elaborated")

    checked = run_cycle(
        proof_id, view=view, gate=gate, scheduler=GlobalScheduler(view, store),
        dispatcher=ScriptedDispatcher({}), worker_class="experiment",
        adapters={"lean-statement-checker": Checker()},
    )
    assert checked.accepted, checked.rejections
    assert view.projected_revision(proof_id) == store.head(proof_id)[0] == 2
    assert view.node(f"{proof_id}/ex-1").fields["success"] is True
    assert view.node(f"{proof_id}/at-1").fields["action_result_id"] == f"{proof_id}/ex-1"
    assert view.edges_from(f"{proof_id}/ex-1", "CHECKS_CLAIM")[0].dst_id == f"{proof_id}/c-1"
    assert view.node(f"{proof_id}/c-1").fields["status"] == "provisional"
