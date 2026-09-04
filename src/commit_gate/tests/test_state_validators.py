import unittest

from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal

def propose(*ops) -> Proposal:
    # Concurrency tokens are mandatory (`check_concurrency_tokens`); these
    # tests are about the state validators, so every proposal carries them.
    return Proposal(
        proof_id="p1",
        actor="user",
        worker_class="human",
        ops=tuple(ops),
        base_revision=0,
        lease_id="lease-1",
        fencing_token=1,
    )

class TestStateValidators(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def validate(self, proposal: Proposal) -> list[Reason]:
        findings = validate_proposal(proposal, self.view)
        return [f.reason for f in findings]

    def test_check_references_unknown_node(self):
        self.view.add_node("p1/fs1", "FormalState", {})
        proposal = propose(SetField("FormalState", "p1/fs2", "status", "open", prior="open"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.UNKNOWN_NODE, reasons)

    def test_check_references_label_mismatch(self):
        self.view.add_node("p1/fs1", "FormalState", {})
        proposal = propose(SetField("Claim", "p1/fs1", "status", "provisional", prior="open"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.NODE_ALREADY_EXISTS_WITH_LABEL, reasons)

    def test_check_references_valid_edge(self):
        self.view.add_node("p1/fs1", "FormalState", {})
        self.view.add_node("p1/ta1", "TacticApplication", {})
        proposal = propose(AddEdge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1"))
        self.assertEqual(self.validate(proposal), [])

    def test_check_references_invalid_edge_endpoint(self):
        self.view.add_node("p1/fs1", "FormalState", {})
        self.view.add_node("p1/ta1", "TacticApplication", {})
        proposal = propose(AddEdge("HAS_TACTIC", "p1/ta1", "p1/fs1", "p1/e1")) # Flipped
        reasons = self.validate(proposal)
        self.assertIn(Reason.EDGE_ENDPOINT_TYPE_INVALID, reasons)

    def test_check_prior_values_mismatch(self):
        self.view.add_node("p1/fs1", "FormalState", {"status": "expanded"})
        proposal = propose(SetField("FormalState", "p1/fs1", "status", "formally-closed", prior="open"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.PRIOR_VALUE_MISMATCH, reasons)

    def test_check_status_transitions_illegal(self):
        self.view.add_node("p1/fs1", "FormalState", {"status": "formally-closed"})
        # formally-closed cannot go back to open
        proposal = propose(SetField("FormalState", "p1/fs1", "status", "open", prior="formally-closed"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.ILLEGAL_STATUS_TRANSITION, reasons)

    def test_check_status_transitions_legal(self):
        self.view.add_node("p1/fs1", "FormalState", {"status": "formally-closed"})
        proposal = propose(SetField("FormalState", "p1/fs1", "status", "lean-verified", prior="formally-closed"))
        self.assertEqual(self.validate(proposal), [])

    def test_check_immutability(self):
        self.view.add_node("p1/fs1", "FormalState", {"goal_text": "A"})
        proposal = propose(SetField("FormalState", "p1/fs1", "goal_text", "B", prior="A"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.IMMUTABLE_FIELD_OVERWRITE, reasons)

    def test_check_stagnation_without_obstruction(self):
        self.view.add_node("p1/run1", "FormalRun", {"status": "searching"})
        proposal = propose(SetField("FormalRun", "p1/run1", "status", "stagnated", prior="searching"))
        reasons = self.validate(proposal)
        self.assertIn(Reason.STAGNATION_WITHOUT_OBSTRUCTION, reasons)

    def test_check_stagnation_with_obstruction_in_proposal(self):
        self.view.add_node("p1/run1", "FormalRun", {"status": "searching"})
        proposal = propose(
            SetField("FormalRun", "p1/run1", "status", "stagnated", prior="searching"),
            UpsertNode("Obstruction", "p1/obs1", {}),
            AddEdge("RAISED_OBSTRUCTION", "p1/run1", "p1/obs1", "p1/e1")
        )
        self.assertEqual(self.validate(proposal), [])


    def test_remove_edge_unknown(self):
        proposal = propose(RemoveEdge("HAS_TACTIC", "p1/e-nope"))
        self.assertIn(Reason.UNKNOWN_EDGE, self.validate(proposal))

    def test_remove_edge_committed(self):
        self.view.add_edge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1")
        proposal = propose(RemoveEdge("HAS_TACTIC", "p1/e1"))
        self.assertEqual(self.validate(proposal), [])

    def test_remove_edge_rel_type_mismatch(self):
        # MORK removes by exact bytes, so a wrong rel would match nothing and
        # still report OK, leaving the edge live while the journal says gone.
        self.view.add_edge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1")
        proposal = propose(RemoveEdge("CITES", "p1/e1"))
        self.assertIn(Reason.UNKNOWN_EDGE, self.validate(proposal))

    def test_remove_edge_added_in_the_same_proposal(self):
        self.view.add_node("p1/fs1", "FormalState", {})
        self.view.add_node("p1/ta1", "TacticApplication", {})
        proposal = propose(
            AddEdge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1"),
            RemoveEdge("HAS_TACTIC", "p1/e1"),
        )
        self.assertEqual(self.validate(proposal), [])

    def test_remove_edge_twice_in_one_proposal(self):
        self.view.add_edge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1")
        proposal = propose(
            RemoveEdge("HAS_TACTIC", "p1/e1"),
            RemoveEdge("HAS_TACTIC", "p1/e1"),
        )
        self.assertIn(Reason.UNKNOWN_EDGE, self.validate(proposal))


        # --- Test for Soundness Gates
class TestSoundnessGates(unittest.TestCase):
    """ replay, self-certification, and alignment gates."""

    def setUp(self):
        self.view = MemoryView()
        self.view.add_node("p1/claim1", "Claim", {"status": "formally-closed"})

    def validate(self, proposal: Proposal) -> list[Reason]:
        findings = validate_proposal(proposal, self.view)
        return [f.reason for f in findings]

    def _promote_claim(self):
        return SetField("Claim", "p1/claim1", "status", "lean-verified", prior="formally-closed")

    # -- check_replay_evidence ------------------------------------------

    def test_promotion_without_any_replay_is_rejected(self):
        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))

    def test_promotion_with_rejected_replay_is_still_rejected(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node("p1/replay1", "LeanReplay", {"actor": "checker", "status": "rejected", "sorry_detected": False})
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")

        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))

    def test_promotion_with_sorry_detected_is_still_rejected(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node("p1/replay1", "LeanReplay", {"actor": "checker", "status": "verified", "sorry_detected": True})
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")

        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))

    def test_promotion_with_verified_sorry_free_replay_passes_this_gate(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node("p1/replay1", "LeanReplay", {"actor": "checker", "status": "verified", "sorry_detected": False})
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")

        proposal = propose(self._promote_claim())
        self.assertNotIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))

    # -- check_self_certification -----------------------------------------

    def test_self_certified_replay_is_rejected(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "same-person"})
        proposal = propose(
            UpsertNode("LeanReplay", "p1/replay1", {"actor": "same-person", "status": "verified", "sorry_detected": False}),
            AddEdge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e1"),
        )
        self.assertIn(Reason.SELF_CERTIFICATION, self.validate(proposal))

    def test_independent_replay_actor_is_not_self_certification(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        proposal = propose(
            UpsertNode("LeanReplay", "p1/replay1", {"actor": "different-person", "status": "verified", "sorry_detected": False}),
            AddEdge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e1"),
        )
        self.assertNotIn(Reason.SELF_CERTIFICATION, self.validate(proposal))

    # -- check_alignment_gate --------------------------------------------

    def test_promotion_without_any_alignment_is_rejected(self):
        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, self.validate(proposal))

    def test_promotion_with_unreviewed_alignment_is_still_rejected(self):
        self.view.add_node("p1/align1", "Alignment", {"lifecycle": "draft", "verdict": "aligned"})
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e1")

        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, self.validate(proposal))

    def test_promotion_with_reviewed_but_mismatched_alignment_is_still_rejected(self):
        self.view.add_node("p1/align1", "Alignment", {"lifecycle": "reviewed", "verdict": "mismatch"})
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e1")

        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, self.validate(proposal))

    def test_promotion_with_reviewed_aligned_alignment_passes_this_gate(self):
        self.view.add_node("p1/align1", "Alignment", {"lifecycle": "reviewed", "verdict": "aligned"})
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e1")

        proposal = propose(self._promote_claim())
        self.assertNotIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, self.validate(proposal))

    # -- full green path ----------------------------------------------------

    def test_promotion_with_all_three_gates_satisfied_is_clean(self):
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node("p1/replay1", "LeanReplay", {"actor": "checker", "status": "verified", "sorry_detected": False})
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")
        self.view.add_node("p1/align1", "Alignment", {"lifecycle": "reviewed", "verdict": "aligned"})
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e3")

        proposal = propose(self._promote_claim())
        self.assertEqual(self.validate(proposal), [])


 # -- test for: re-upsert cannot launder committed state, and
    #    self-certification is caught even across two proposals ------------
 
    def test_re_upsert_cannot_launder_a_rejected_committed_replay(self):
        """apply_ops drops a re-upsert of an already-committed node -- it is
        identity confirmation, never a mutation. The validator must read the
        committed fields, not whatever the proposal's UpsertNode claims."""
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node(
            "p1/replay1",
            "LeanReplay",
            {"actor": "checker", "status": "rejected", "sorry_detected": True},
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")
 
        proposal = propose(
            UpsertNode(
                "LeanReplay",
                "p1/replay1",
                {"actor": "checker", "status": "verified", "sorry_detected": False},
            ),
            self._promote_claim(),
        )
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))
 
    def test_self_certification_across_two_proposals_is_rejected(self):
        """The replay was created and linked to its certificate in an earlier,
        already-committed proposal. A later proposal only promotes the claim
        -- the gate must still catch that the replay was self-certified."""
        self.view.add_node("p1/cert1", "Certificate", {"actor": "same-person"})
        self.view.add_node(
            "p1/replay1",
            "LeanReplay",
            {"actor": "same-person", "status": "verified", "sorry_detected": False},
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")
 
        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))
 
    def test_self_certified_link_made_in_a_later_proposal_is_rejected(self):
        """The replay node already exists (committed, independently); a later
        proposal only adds the REPLAYED_BY edge linking it to the certificate.
        check_self_certification must trigger on that edge, not just on a
        same-proposal LeanReplay upsert, and must produce the specific
        SELF_CERTIFICATION reason at the point the link is made."""
        self.view.add_node("p1/cert1", "Certificate", {"actor": "same-person"})
        self.view.add_node(
            "p1/replay1",
            "LeanReplay",
            {"actor": "same-person", "status": "verified", "sorry_detected": False},
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
 
        proposal = propose(AddEdge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2"))
        self.assertIn(Reason.SELF_CERTIFICATION, self.validate(proposal))
    def test_actorless_certificate_cannot_launder_a_self_report(self):
        """A Certificate with no `actor` recorded must not be treated as
        'provably a different actor' from the replay. Missing evidence is
        insufficient evidence, not proof of independence -- promotion must
        stay blocked, even with a satisfied alignment record."""
        self.view.add_node("p1/cert1", "Certificate", {})  # no actor at all
        self.view.add_node(
            "p1/replay1",
            "LeanReplay",
            {"actor": "same-person", "status": "verified", "sorry_detected": False},
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2")
        self.view.add_node("p1/align1", "Alignment", {"lifecycle": "reviewed", "verdict": "aligned"})
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e3")
 
        proposal = propose(self._promote_claim())
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, self.validate(proposal))


    # Test for --- Alignment.verdict must be settable at review time -------
 
    def test_reviewer_can_record_verdict_when_lifecycle_is_review_needed(self):
        """The bug: verdict was immutable, so it could only ever be set at
        Alignment creation -- before any review happened. A reviewer must be
        able to set it at the point they actually reach a conclusion."""
        self.view.add_node(
            "p1/align1", "Alignment",
            {"actor": "reviewer-1", "lifecycle": "review-needed", "verdict": None},
        )
        proposal = propose(
            SetField("Alignment", "p1/align1", "verdict", "aligned", prior=None)
        )
        self.assertEqual(self.validate(proposal), [])
 
    def test_verdict_recorded_at_review_step_satisfies_the_alignment_gate(self):
        """End to end: draft -> review-needed -> reviewed, with the verdict
        set at the review step (not baked in at creation), must be enough
        to satisfy check_alignment_gate for a real promotion."""
        self.view.add_node(
            "p1/align1", "Alignment",
            {"actor": "reviewer-1", "lifecycle": "review-needed", "verdict": None},
        )
        self.view.add_edge("ALIGNS_CLAIM", "p1/align1", "p1/claim1", "p1/e1")
        self.view.add_node("p1/cert1", "Certificate", {"actor": "producer"})
        self.view.add_node(
            "p1/replay1", "LeanReplay",
            {"actor": "checker", "status": "verified", "sorry_detected": False},
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e2")
        self.view.add_edge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e3")
 
        review = propose(
            SetField("Alignment", "p1/align1", "lifecycle", "reviewed", prior="review-needed"),
            SetField("Alignment", "p1/align1", "verdict", "aligned", prior=None),
        )
        self.assertEqual(self.validate(review), [])
        self.view.set_field("p1/align1", "lifecycle", "reviewed")
        self.view.set_field("p1/align1", "verdict", "aligned")
 
        promotion = propose(self._promote_claim())
        self.assertEqual(self.validate(promotion), [])
 
    def test_alignment_actor_remains_immutable_after_the_verdict_fix(self):
        self.view.add_node(
            "p1/align1", "Alignment",
            {"actor": "reviewer-1", "lifecycle": "review-needed", "verdict": None},
        )
        proposal = propose(
            SetField("Alignment", "p1/align1", "actor", "someone-else", prior="reviewer-1")
        )
        self.assertIn(Reason.IMMUTABLE_FIELD_OVERWRITE, self.validate(proposal))
    
    def test_verdict_setfield_without_a_lease_is_rejected(self):
        """op_class classifies any field ending in 'verdict' as status-class
        (ops.py), which check_concurrency_tokens requires a lease and
        fencing token for. This must hold specifically for Alignment.verdict,
        not just for the generic case."""
        from dataclasses import replace
        self.view.add_node(
            "p1/align1", "Alignment",
            {"actor": "reviewer-1", "lifecycle": "review-needed", "verdict": None},
        )
        proposal = replace(
            propose(SetField("Alignment", "p1/align1", "verdict", "aligned", prior=None)),
            lease_id=None, fencing_token=None,
        )
        self.assertIn(Reason.MISSING_CONCURRENCY_TOKEN, self.validate(proposal))
 
    def test_verdict_outside_the_enum_is_still_rejected(self):
        """ENUM_FIELDS[("Alignment", "verdict")] must still enforce
        AlignmentVerdict now that the field is actually reachable via
        SetField."""
        self.view.add_node(
            "p1/align1", "Alignment",
            {"actor": "reviewer-1", "lifecycle": "review-needed", "verdict": None},
        )
        proposal = propose(
            SetField("Alignment", "p1/align1", "verdict", "not-a-real-verdict", prior=None)
        )
        self.assertIn(Reason.UNKNOWN_STATUS_VALUE, self.validate(proposal))
 


if __name__ == "__main__":
    unittest.main()
