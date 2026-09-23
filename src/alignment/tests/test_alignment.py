"""Unit and integration tests for the Alignment Worker and Reviewer."""

import unittest

from alignment.models import (
    AlignmentCriteria,
    AlignmentReviewRequest,
    compute_content_hash,
)
from alignment.reviewer import RuleBasedAlignmentReviewer
from alignment.worker import AlignmentWorker, build_alignment_proposal
from commit_gate.validate import validate_proposal
from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict, WorkerClass


class TestAlignmentModels(unittest.TestCase):
    def test_compute_content_hash(self):
        h1 = compute_content_hash("theorem add_comm (a b : Nat) : a + b = b + a")
        self.assertTrue(h1.startswith("sha256:"))
        self.assertEqual(len(h1), 71)  # "sha256:" + 64 hex chars

    def test_request_auto_hashing(self):
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all naturals a and b, a + b = b + a",
            formal_statement="theorem add_comm (a b : Nat) : a + b = b + a",
        )
        self.assertTrue(req.statement_hash.startswith("sha256:"))
        self.assertTrue(req.source_hash.startswith("sha256:"))


class TestAlignmentReviewer(unittest.TestCase):
    def setUp(self):
        self.reviewer = RuleBasedAlignmentReviewer(reviewer_id="test-reviewer")

    def test_review_exact_match(self):
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="Every prime greater than 2 is odd.",
            formal_statement="theorem prime_odd (p : Nat) (hp : Prime p) (h2 : p > 2) : Odd p",
        )
        result = self.reviewer.review(req)
        self.assertEqual(result.verdict, AlignmentVerdict.ALIGNED)
        self.assertEqual(result.lifecycle, AlignmentLifecycle.REVIEWED)
        self.assertEqual(result.criteria.relation, "exact")

    def test_review_empty_statement(self):
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="",
            formal_statement="theorem trivial : True := trivial",
        )
        result = self.reviewer.review(req)
        self.assertEqual(result.verdict, AlignmentVerdict.MISMATCH)

    def test_review_sorry_statement(self):
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="Complex proposition",
            formal_statement="theorem prop : sorry := sorry",
        )
        result = self.reviewer.review(req)
        self.assertEqual(result.verdict, AlignmentVerdict.AMBIGUOUS)


class TestLLMAlignmentReviewer(unittest.TestCase):
    def test_llm_review_exact_aligned(self):
        from alignment.llm_client import MockLLMClient
        from alignment.reviewer import LLMAlignmentReviewer

        mock_response = {
            "quantifier_correspondence": "forall p : Nat matches all prime numbers p",
            "domain_correspondence": "Nat corresponds to natural numbers > 2",
            "universe_assumptions": [],
            "typeclass_assumptions": ["[Fact (Prime p)]"],
            "classical_assumptions": [],
            "constructive_assumptions": [],
            "selected_definitions_match": True,
            "implication_to_target_explicit": True,
            "relation": "exact",
            "verdict": "aligned",
            "reasoning": "The Lean 4 theorem faithfully encodes the informal mathematical statement without modification.",
        }

        mock_client = MockLLMClient(default_json=mock_response)
        reviewer = LLMAlignmentReviewer(llm_client=mock_client, reviewer_id="llm-critic-01")

        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="Every prime greater than 2 is odd.",
            formal_statement="theorem prime_odd (p : Nat) (hp : Prime p) (h2 : p > 2) : Odd p",
        )

        result = reviewer.review(req)
        self.assertEqual(result.verdict, AlignmentVerdict.ALIGNED)
        self.assertEqual(result.lifecycle, AlignmentLifecycle.REVIEWED)
        self.assertEqual(result.criteria.relation, "exact")
        self.assertEqual(result.criteria.typeclass_assumptions, ("[Fact (Prime p)]",))
        self.assertTrue(result.criteria.selected_definitions_match)
        self.assertEqual(result.reviewer, "llm-critic-01")

    def test_llm_review_with_markdown_fences(self):
        from alignment.llm_client import MockLLMClient
        from alignment.reviewer import LLMAlignmentReviewer

        markdown_json = """Here is my review:
```json
{
  "quantifier_correspondence": "forall n : Nat",
  "domain_correspondence": "Nat",
  "universe_assumptions": [],
  "typeclass_assumptions": [],
  "classical_assumptions": [],
  "constructive_assumptions": [],
  "selected_definitions_match": true,
  "implication_to_target_explicit": true,
  "relation": "weakening",
  "verdict": "weaker",
  "reasoning": "Formal theorem adds extra hypothesis not in informal claim."
}
```"""
        mock_client = MockLLMClient(responses=[markdown_json])
        reviewer = LLMAlignmentReviewer(llm_client=mock_client)

        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all integers n, n * 0 = 0",
            formal_statement="theorem mul_zero_pos (n : Nat) (h : n > 0) : n * 0 = 0",
        )

        result = reviewer.review(req)
        self.assertEqual(result.verdict, AlignmentVerdict.WEAKER)
        self.assertEqual(result.criteria.relation, "weakening")

    def test_worker_with_llm_reviewer_integration(self):
        from alignment.llm_client import MockLLMClient
        from alignment.reviewer import LLMAlignmentReviewer

        mock_client = MockLLMClient(
            default_json={
                "quantifier_correspondence": "matches",
                "domain_correspondence": "matches",
                "selected_definitions_match": True,
                "implication_to_target_explicit": True,
                "relation": "exact",
                "verdict": "aligned",
                "reasoning": "Exact match across all 8 dimensions.",
            }
        )
        llm_reviewer = LLMAlignmentReviewer(llm_client=mock_client)
        worker = AlignmentWorker(actor="llm-alignment-worker", reviewer=llm_reviewer)

        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="A + B = B + A",
            formal_statement="theorem add_comm : a + b = b + a",
        )

        result, proposal = worker.run_review(
            req,
            alignment_id="p1/al-1",
            base_revision=1,
            lease_id="lease-1",
            fencing_token=1,
        )

        self.assertEqual(result.verdict, AlignmentVerdict.ALIGNED)
        self.assertEqual(proposal.actor, "llm-alignment-worker")
        self.assertEqual(proposal.worker_class, WorkerClass.ALIGNMENT_REVIEWER.value)
        self.assertEqual(validate_proposal(proposal), [])



class TestAlignmentWorker(unittest.TestCase):
    def setUp(self):
        self.worker = AlignmentWorker(actor="alignment-reviewer-agent")

    def test_run_review_and_gate_validation(self):
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all n, n + 0 = n",
            formal_statement="theorem add_zero (n : Nat) : n + 0 = n",
        )

        result, proposal = self.worker.run_review(
            req,
            alignment_id="p1/al-1",
            base_revision=1,
            lease_id="lease-align-1",
            fencing_token=1,
        )

        self.assertEqual(result.verdict, AlignmentVerdict.ALIGNED)
        self.assertEqual(proposal.actor, "alignment-reviewer-agent")
        self.assertEqual(proposal.worker_class, WorkerClass.ALIGNMENT_REVIEWER.value)
        self.assertEqual(len(proposal.ops), 3)

        # Validate proposal against CommitGate rules
        rejections = validate_proposal(proposal)
        self.assertEqual(rejections, [], f"Proposal failed validation: {rejections}")


class TestAlignmentSchemaIntegration(unittest.TestCase):
    def test_schema_conformance(self):
        """Test that AlignmentRecord serializes into a payload valid against statement-alignment schema."""
        from mathproof.schemas import validate

        worker = AlignmentWorker(actor="alignment-reviewer-agent")
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all naturals a and b, a + b = b + a",
            formal_statement="theorem add_comm (a b : Nat) : a + b = b + a",
            imports=("Mathlib.Data.Nat.Basic",),
            namespace="Nat",
        )
        result, _ = worker.run_review(req, alignment_id="p1/al-1")
        record = worker.create_record(req, result, alignment_id="p1/al-1")
        payload = record.to_dict()

        # Validate against JSON schema
        validate("statement-alignment", payload)

        # Test deserialization roundtrip
        roundtripped = record.from_dict(payload)
        self.assertEqual(roundtripped.alignment_id, record.alignment_id)
        self.assertEqual(roundtripped.claim_id, record.claim_id)
        self.assertEqual(roundtripped.declaration_id, record.declaration_id)
        self.assertEqual(roundtripped.verdict, record.verdict)

    def test_worker_result_schema_conformance(self):
        """Test that build_worker_result outputs a payload valid against worker-result schema."""
        from alignment.worker import build_worker_result
        from mathproof.schemas import validate

        worker = AlignmentWorker(actor="alignment-reviewer-agent")
        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all naturals a and b, a + b = b + a",
            formal_statement="theorem add_comm (a b : Nat) : a + b = b + a",
        )
        result, _ = worker.run_review(req, alignment_id="p1/al-1")
        worker_res_dict = build_worker_result(
            attempt_id="p1/at-1",
            proof_id="p1",
            result=result,
            request=req,
            base_revision=1,
            lease_id="lease-1",
            fencing_token=1,
        )

        # Validate against JSON schema
        validate("worker-result", worker_res_dict)
        self.assertEqual(worker_res_dict["worker_class"], "alignment-reviewer")
        self.assertEqual(worker_res_dict["evidence_kind"], "critic-review")



class TestAlignmentCommitGateIntegration(unittest.TestCase):
    def setUp(self):
        from commit_gate.gate import CommitGate
        from commit_gate.state import MemoryView
        from commit_gate.store import JournalStore

        self.view = MemoryView()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)
        self.worker = AlignmentWorker(actor="alignment-reviewer-agent")

        # Setup prerequisite Claim and FormalDeclaration in committed state
        from commit_gate.apply import apply_ops
        from commit_gate.ops import UpsertNode
        from commit_gate.proposal import Proposal

        init_proposal = Proposal(
            proof_id="p1",
            actor="coordinator",
            worker_class="coordinator",
            base_revision=0,
            ops=(
                UpsertNode("Claim", "p1/c-1", {"status": "provisional"}),
                UpsertNode("FormalDeclaration", "p1/fd-1", {"exact_hash": "sha256:abc"}),
            ),
        )
        res = self.gate.commit(init_proposal)
        self.assertTrue(res.accepted, f"Init proposal rejected: {res.rejections}")
        apply_ops(self.view, init_proposal.ops)

    def test_gate_commits_alignment_proposal(self):
        from commit_gate.apply import apply_ops

        # Acquire lease from the store
        token = self.store.acquire_lease("p1", "lease-align-1")

        req = AlignmentReviewRequest(
            proof_id="p1",
            claim_id="p1/c-1",
            declaration_id="p1/fd-1",
            informal_statement="For all n, n + 0 = n",
            formal_statement="theorem add_zero (n : Nat) : n + 0 = n",
        )

        result, proposal = self.worker.run_review(
            req,
            alignment_id="p1/al-1",
            base_revision=1,
            lease_id="lease-align-1",
            fencing_token=token,
        )

        # Commit to gate
        commit_res = self.gate.commit(proposal)
        self.assertTrue(commit_res.accepted, f"Gate rejected: {commit_res.rejections}")
        self.assertEqual(commit_res.revision, 2)

        # Apply to view and verify nodes and edges exist
        apply_ops(self.view, proposal.ops)
        node = self.view.node("p1/al-1")
        self.assertIsNotNone(node)
        self.assertEqual(node.label, "Alignment")
        self.assertEqual(node.fields["verdict"], "aligned")

        claim_edge = self.view.edge("p1/al-1->p1/c-1:ALIGNS_CLAIM")
        self.assertIsNotNone(claim_edge)
        self.assertEqual(claim_edge.rel_type, "ALIGNS_CLAIM")

        decl_edge = self.view.edge("p1/al-1->p1/fd-1:ALIGNS_DECLARATION")
        self.assertIsNotNone(decl_edge)
        self.assertEqual(decl_edge.rel_type, "ALIGNS_DECLARATION")


if __name__ == "__main__":
    unittest.main()

