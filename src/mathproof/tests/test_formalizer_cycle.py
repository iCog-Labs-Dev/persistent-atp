"""Formalization draft and statement review stay separate from proof search."""

from alignment.models import AlignmentCriteria, AlignmentReviewResult
from alignment.worker import AlignmentWorker
from commit_gate.gate import CommitGate
from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict
from commit_gate.validate import check_worker_authority
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.lean_statement import StatementCheck
from mathproof.scheduler import GlobalScheduler
from mathproof.workers import FormalizerWorker
from shared.llm_client import MockLLMClient
import pytest
import shutil
from pathlib import Path
from mathproof.lean_statement import LeanStatementChecker


class Checked:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self, lean_type, imports):
        assert lean_type == "∀ n : Nat, n + 0 = n"
        assert imports == ["Init"]
        return StatementCheck(self.ok, "elaborated" if self.ok else "unknown identifier")


class Reviewer:
    def __init__(self, verdict=AlignmentVerdict.ALIGNED):
        self.verdict = verdict

    def review(self, request):
        assert request.informal_statement == "For all natural n, n + 0 = n"
        assert "theorem clm_c_1" in request.formal_statement
        return AlignmentReviewResult(
            verdict=self.verdict, lifecycle=AlignmentLifecycle.REVIEWED,
            criteria=AlignmentCriteria(relation="exact"), reviewer="alignment-1",
            reasoning="Quantifier, Nat domain, and equation correspond.",
        )


def setup():
    view, store = MemoryView(), JournalStore()
    view.add_node("p1/c-1", "Claim", {
        "status": "provisional", "claim_text": "For all natural n, n + 0 = n",
    })
    view.add_node("p1/at-1", "Attempt", {"status": "supported", "worker_class": "critic"})
    view.add_edge("REVIEWS_CLAIM", "p1/at-1", "p1/c-1", "p1/at-1-reviews")
    view.add_node("p2/c-1", "Claim", {
        "status": "critic-accepted", "claim_text": "Other proof",
    })
    gate, scheduler = CommitGate(view, store), GlobalScheduler(view, store)
    maintenance = lambda proposal, commit: view.apply_journal_event(store, "p1", commit.revision)
    return view, store, gate, scheduler, maintenance


def test_draft_then_review_does_not_verify_claim():
    view, store, gate, scheduler, maintenance = setup()
    llm = MockLLMClient(default_json={
        "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"],
        "reasoning": "The domain is Nat and the equation is unchanged.",
    })
    draft = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"formalizer": FormalizerWorker(llm).run}),
        worker_class="formalizer", actor="formalizer-1", maintenance=maintenance,
    )
    assert draft.accepted, draft.rejections
    assert view.node("p1/fd-1").fields["status"] == "draft"
    assert view.edges_from("p1/fd-1", "DRAFTS_CLAIM")[0].dst_id == "p1/c-1"
    assert "p2/c-1" not in llm.history[0][1]
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formalization-task"]
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formal-run"]

    review = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="alignment-reviewer",
        actor="alignment-1", maintenance=maintenance,
        adapters={
            "alignment-reviewer": AlignmentWorker(actor="alignment-1", reviewer=Reviewer()),
            "lean-statement-checker": Checked(),
        },
    )
    assert review.accepted, review.rejections
    assert view.node("p1/fd-1").fields["status"] == "aligned"
    assert view.node("p1/al-1").fields["statement_checked"] is True
    assert view.node("p1/c-1").fields["status"] == "provisional"
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formal-run"]
    view.add_node("p1/env-1", "Environment", {"lake_manifest_hash": "sha256:" + "11" * 32})
    view.add_edge("PINNED_ENVIRONMENT", "p1/fd-1", "p1/env-1", "p1/pin")
    assert [c.move_id for c in scheduler.build_frontier("p1") if c.category == "formal-run"] == ["p1/fd-1"]
    assert store.verify_chain("p1") == 2


def test_failed_statement_check_cannot_schedule_atp():
    view, store, gate, scheduler, maintenance = setup()
    run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"formalizer": FormalizerWorker(MockLLMClient(default_json={
            "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"], "reasoning": "literal translation",
        })).run}), worker_class="formalizer", maintenance=maintenance,
    )
    review = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="alignment-reviewer",
        actor="alignment-1", maintenance=maintenance,
        adapters={"alignment-reviewer": AlignmentWorker(actor="alignment-1", reviewer=Reviewer()),
                  "lean-statement-checker": Checked(ok=False)},
    )
    assert review.accepted, review.rejections
    assert view.node("p1/fd-1").fields["status"] == "draft"
    assert view.node("p1/al-1").fields["lifecycle"] == "review-needed"
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formal-run"]


def test_alignment_mismatch_remains_a_draft():
    view, store, gate, scheduler, maintenance = setup()
    run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"formalizer": FormalizerWorker(MockLLMClient(default_json={
            "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"], "reasoning": "translation",
        })).run}), worker_class="formalizer", maintenance=maintenance,
    )
    review = run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({}), worker_class="alignment-reviewer",
        actor="alignment-1", maintenance=maintenance,
        adapters={"alignment-reviewer": AlignmentWorker(actor="alignment-1", reviewer=Reviewer(AlignmentVerdict.MISMATCH)),
                  "lean-statement-checker": Checked()},
    )
    assert review.accepted, review.rejections
    assert view.node("p1/al-1").fields["verdict"] == "mismatch"
    assert view.node("p1/fd-1").fields["status"] == "draft"
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formal-run"]


def test_rule_based_reviewer_is_not_an_automatic_alignment_gate():
    view, store, gate, scheduler, maintenance = setup()
    run_cycle(
        "p1", view=view, gate=gate, scheduler=scheduler,
        dispatcher=ScriptedDispatcher({"formalizer": FormalizerWorker(MockLLMClient(default_json={
            "lean_type": "∀ n : Nat, n + 0 = n", "imports": ["Init"], "reasoning": "translation",
        })).run}), worker_class="formalizer", maintenance=maintenance,
    )
    with pytest.raises(ValueError, match="rule-based alignment"):
        run_cycle(
            "p1", view=view, gate=gate, scheduler=scheduler,
            dispatcher=ScriptedDispatcher({}), worker_class="alignment-reviewer",
            actor="alignment-1", adapters={
                "alignment-reviewer": AlignmentWorker(actor="alignment-1"),
                "lean-statement-checker": Checked(),
            },
        )
    assert store.head("p1")[0] == 1
    assert not store.active_leases("p1")


def test_unverified_dependency_blocks_formalization_frontier():
    view, store, _, scheduler, _ = setup()
    view.add_node("p1/c-2", "Claim", {"status": "provisional", "claim_text": "A dependency"})
    view.add_edge("DEPENDS_ON", "p1/c-1", "p1/c-2", "p1/dependency")
    assert not [c for c in scheduler.build_frontier("p1") if c.category == "formalization-task"]


def test_formalizer_cannot_publish_an_aligned_declaration():
    proposal = Proposal(
        proof_id="p1", actor="formalizer-1", worker_class="formalizer",
        ops=(UpsertNode("FormalDeclaration", "p1/fd-1", {"status": "aligned"}),),
    )
    assert list(check_worker_authority(proposal))
    proposal = Proposal(
        proof_id="p1", actor="formalizer-1", worker_class="formalizer",
        ops=(SetField("FormalDeclaration", "p1/fd-1", "status", "aligned", prior="draft"),),
    )
    assert list(check_worker_authority(proposal))


def test_alignment_reviewer_cannot_align_without_checked_review():
    proposal = Proposal(
        proof_id="p1", actor="alignment-1", worker_class="alignment-reviewer",
        ops=(SetField("FormalDeclaration", "p1/fd-1", "status", "aligned", prior="draft"),),
    )
    assert list(check_worker_authority(proposal))


@pytest.mark.skipif(shutil.which("lake") is None or shutil.which("lean") is None,
                    reason="Lean toolchain is not installed")
def test_real_lean_statement_elaboration():
    checker = LeanStatementChecker(Path.cwd(), timeout_seconds=30)
    assert checker.check("∀ n : Nat, n + 0 = n", ["Init"]).ok
    assert not checker.check("∀ n : UnknownType, n = n", ["Init"]).ok
    assert not checker.check("True := by sorry", ["Init"]).ok
