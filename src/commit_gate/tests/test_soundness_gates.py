import unittest

from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal

PRODUCER = "producer-alpha"
REPLAYER = "replayer-beta"


def propose(*ops, actor="coordinator-1") -> Proposal:
    return Proposal(
        proof_id="p1",
        actor=actor,
        worker_class="coordinator",
        ops=tuple(ops),
        base_revision=0,
        lease_id="lease-1",
        fencing_token=1,
    )


def promote(to="lean-verified", claim_id="p1/claim1", prior="formally-closed"):
    return SetField("Claim", claim_id, "status", to, prior=prior)


def wire(
    view: MemoryView,
    claim_id="p1/claim1",
    cert_id="p1/cert1",
    replay_id="p1/replay1",
    alignment_id="p1/alignment1",
    claim_status="formally-closed",
    cert_fields=None,
    replay_fields=None,
    alignment_fields=None,
):
    view.add_node(claim_id, "Claim", {"status": claim_status})
    view.add_node(
        cert_id, "Certificate", {"actor": PRODUCER, **(cert_fields or {})}
    )
    view.add_node(
        replay_id,
        "LeanReplay",
        {
            "actor": REPLAYER,
            "status": "verified",
            "sorry_detected": False,
            **(replay_fields or {}),
        },
    )
    view.add_node(
        alignment_id,
        "Alignment",
        {"lifecycle": "reviewed", "verdict": "aligned", **(alignment_fields or {})},
    )
    view.add_edge("PROVED_BY", claim_id, cert_id, f"{claim_id}-proved-{cert_id}")
    view.add_edge("REPLAYED_BY", cert_id, replay_id, f"{cert_id}-replayed-{replay_id}")
    view.add_edge(
        "ALIGNS_CLAIM", alignment_id, claim_id, f"{alignment_id}-aligns-{claim_id}"
    )


class TestReplayGate(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def reasons(self, proposal: Proposal) -> list[Reason]:
        return [f.reason for f in validate_proposal(proposal, self.view)]

    def test_promotion_without_replay_evidence(self):
        self.view.add_node("p1/claim1", "Claim", {"status": "formally-closed"})
        proposal = propose(promote())
        findings = validate_proposal(proposal, self.view)
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, [f.reason for f in findings])
        replay = next(
            f for f in findings if f.reason == Reason.PROMOTION_WITHOUT_REPLAY
        )
        self.assertEqual(replay.op_index, 0)

    def test_broken_chain_without_certificate(self):
        self.view.add_node("p1/claim1", "Claim", {"status": "formally-closed"})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_certificate_without_replay_is_not_evidence(self):
        wire(self.view)
        self.view.remove_edge("p1/cert1-replayed-p1/replay1")
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_unverified_replay_is_not_evidence(self):
        wire(self.view, replay_fields={"status": "rejected"})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)
        self.assertNotIn(Reason.SELF_CERTIFICATION, reasons)

    def test_sorry_detected_replay_is_not_evidence(self):
        wire(self.view, replay_fields={"sorry_detected": True})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_missing_sorry_flag_is_not_evidence(self):
        wire(self.view, replay_fields={"sorry_detected": None})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)


class TestSelfCertificationGate(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def reasons(self, proposal: Proposal) -> list[Reason]:
        return [f.reason for f in validate_proposal(proposal, self.view)]

    def test_replay_by_certificate_producer(self):
        wire(self.view, replay_fields={"actor": PRODUCER})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.SELF_CERTIFICATION, reasons)
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_replay_by_submitting_actor(self):
        wire(self.view)
        reasons = self.reasons(propose(promote(), actor=REPLAYER))
        self.assertIn(Reason.SELF_CERTIFICATION, reasons)
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_self_certified_replay_poisons_an_independent_one(self):
        wire(self.view, replay_fields={"actor": PRODUCER})
        self.view.add_node(
            "p1/replay2",
            "LeanReplay",
            {"actor": "replayer-gamma", "status": "verified", "sorry_detected": False},
        )
        self.view.add_edge(
            "REPLAYED_BY", "p1/cert1", "p1/replay2", "p1/cert1-replayed-p1/replay2"
        )

        reasons = self.reasons(propose(promote()))
        self.assertEqual(reasons, [Reason.SELF_CERTIFICATION])


class TestAlignmentGate(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def reasons(self, proposal: Proposal) -> list[Reason]:
        return [f.reason for f in validate_proposal(proposal, self.view)]

    def test_promotion_without_alignment(self):
        wire(self.view)
        self.view.remove_edge("p1/alignment1-aligns-p1/claim1")
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)
        self.assertNotIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)

    def test_every_promotion_target_requires_alignment(self):
        cases = {
            "critic-accepted": "provisional",
            "formally-closed": "provisional",
            "lean-verified": "formally-closed",
        }
        for status, prior in cases.items():
            with self.subTest(status=status):
                view = MemoryView()
                wire(view, claim_status=prior)
                view.remove_edge("p1/alignment1-aligns-p1/claim1")
                proposal = propose(promote(to=status, prior=prior))
                reasons = [f.reason for f in validate_proposal(proposal, view)]
                self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)

    def test_unreviewed_alignment_is_not_evidence(self):
        wire(self.view, alignment_fields={"lifecycle": "review-needed"})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)

    def test_disagreeing_alignment_is_not_evidence(self):
        wire(self.view, alignment_fields={"verdict": "mismatch"})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)

    def test_superseded_alignment_is_not_evidence(self):
        wire(self.view, alignment_fields={"lifecycle": "superseded"})
        reasons = self.reasons(propose(promote()))
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)


class TestSoundnessGatesHappyPath(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def findings(self, proposal: Proposal):
        return validate_proposal(proposal, self.view)

    def test_committed_evidence_validates_clean(self):
        wire(self.view)
        self.assertEqual(self.findings(propose(promote())), [])

    def test_evidence_in_same_proposal(self):
        proposal = propose(
            UpsertNode(
                "Certificate",
                "p1/cert1",
                {"actor": PRODUCER, "producer_run_id": "p1/run1"},
            ),
            UpsertNode(
                "LeanReplay",
                "p1/replay1",
                {
                    "actor": REPLAYER,
                    "status": "verified",
                    "sorry_detected": False,
                    "replayed_at": "2026-08-24T00:00:00Z",
                },
            ),
            UpsertNode(
                "Alignment",
                "p1/alignment1",
                {"lifecycle": "reviewed", "verdict": "aligned", "actor": "reviewer"},
            ),
            AddEdge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1"),
            AddEdge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2"),
            AddEdge("ALIGNS_CLAIM", "p1/alignment1", "p1/claim1", "p1/e3"),
            promote(),
        )
        self.view.add_node("p1/claim1", "Claim", {"status": "formally-closed"})
        self.assertEqual(self.findings(proposal), [])

    def test_formal_state_promotion_stays_ungated(self):
        self.view.add_node("p1/fs1", "FormalState", {"status": "formally-closed"})
        proposal = propose(
            SetField(
                "FormalState", "p1/fs1", "status", "lean-verified", prior="formally-closed"
            )
        )
        self.assertEqual(self.findings(proposal), [])

    def test_claim_downgrade_needs_no_gates(self):
        wire(self.view)
        proposal = propose(
            SetField(
                "Claim", "p1/claim1", "status", "tainted", prior="formally-closed"
            )
        )
        self.assertEqual(self.findings(proposal), [])

    def test_immutable_sorry_detected_cannot_be_flipped(self):
        from commit_gate.transitions import IMMUTABLE_FIELDS

        self.assertIn("sorry_detected", IMMUTABLE_FIELDS["LeanReplay"])


if __name__ == "__main__":
    unittest.main()
