import unittest

from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal

PRODUCER = "producer-alpha"
REPLAYER = "replayer-beta"
ARTIFACT_HASH = "sha256:" + "aa" * 32
ENVIRONMENT_HASH = "sha256:" + "11" * 32


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
    declaration_id="p1/declaration1",
    run_id="p1/run1",
    environment_id="p1/environment1",
    claim_status="formally-closed",
    cert_fields=None,
    replay_fields=None,
    alignment_fields=None,
):
    view.add_node(claim_id, "Claim", {"status": claim_status})
    view.add_node(
        cert_id,
        "Certificate",
        {
            "actor": PRODUCER,
            "status": "replay-accepted",
            "artifact_hash": ARTIFACT_HASH,
            "environment_hash": ENVIRONMENT_HASH,
            "producer_run_id": run_id,
            **(cert_fields or {}),
        },
    )
    view.add_node(
        run_id, "FormalRun",
        {"status": "proved-pending-replay", "environment_hash": ENVIRONMENT_HASH},
    )
    view.add_node(
        environment_id, "Environment", {"environment_hash": ENVIRONMENT_HASH}
    )
    view.add_node(
        replay_id,
        "LeanReplay",
        {
            "actor": REPLAYER,
            "status": "verified",
            "sorry_detected": False,
            "environment_hash": ENVIRONMENT_HASH,
            **(replay_fields or {}),
        },
    )
    view.add_node(
        alignment_id,
        "Alignment",
        {"lifecycle": "reviewed", "verdict": "aligned", **(alignment_fields or {})},
    )
    view.add_node(declaration_id, "FormalDeclaration")
    view.add_edge("PROVED_BY", claim_id, cert_id, f"{claim_id}-proved-{cert_id}")
    view.add_edge("REPLAYED_BY", cert_id, replay_id, f"{cert_id}-replayed-{replay_id}")
    view.add_edge("CERTIFIES", cert_id, declaration_id, f"{cert_id}-certifies-{declaration_id}")
    view.add_edge("PRODUCED_CERTIFICATE", run_id, cert_id, f"{run_id}-produced-{cert_id}")
    view.add_edge("SEARCHES", run_id, declaration_id, f"{run_id}-searches-{declaration_id}")
    view.add_edge("PINNED_ENVIRONMENT", declaration_id, environment_id, f"{declaration_id}-pinned-{environment_id}")
    view.add_edge("CERTIFICATE_ENVIRONMENT", cert_id, environment_id, f"{cert_id}-under-{environment_id}")
    view.add_edge("RAN_UNDER", run_id, environment_id, f"{run_id}-under-{environment_id}")
    view.add_edge("REPLAY_ENVIRONMENT", replay_id, environment_id, f"{replay_id}-under-{environment_id}")
    view.add_edge(
        "ALIGNS_CLAIM", alignment_id, claim_id, f"{alignment_id}-aligns-{claim_id}"
    )
    view.add_edge(
        "ALIGNS_DECLARATION", alignment_id, declaration_id,
        f"{alignment_id}-aligns-{declaration_id}",
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

    def test_direct_verified_claim_creation_needs_evidence(self):
        proposal = propose(
            UpsertNode("Claim", "p1/new-claim", {"status": "lean-verified"})
        )
        reasons = self.reasons(proposal)
        self.assertIn(Reason.PROMOTION_WITHOUT_REPLAY, reasons)
        self.assertIn(Reason.PROMOTION_WITHOUT_ALIGNMENT, reasons)

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

    def test_self_certified_replay_does_not_block_an_independent_one(self):
        wire(self.view, replay_fields={"actor": PRODUCER})
        self.view.add_node(
            "p1/replay2",
            "LeanReplay",
            {
                "actor": "replayer-gamma", "status": "verified",
                "sorry_detected": False, "environment_hash": ENVIRONMENT_HASH,
            },
        )
        self.view.add_edge(
            "REPLAYED_BY", "p1/cert1", "p1/replay2", "p1/cert1-replayed-p1/replay2"
        )
        self.view.add_edge(
            "REPLAY_ENVIRONMENT", "p1/replay2", "p1/environment1", "p1/replay2-env"
        )

        reasons = self.reasons(propose(promote()))
        self.assertEqual(reasons, [])


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


class TestDeclarationChain(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()
        wire(self.view)

    def reasons(self, *ops) -> list[Reason]:
        return [f.reason for f in validate_proposal(propose(*ops), self.view)]

    def test_certificate_without_declaration_link(self):
        self.view.remove_edge("p1/cert1-certifies-p1/declaration1")
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN, self.reasons(promote())
        )

    def test_alignment_without_declaration_link(self):
        self.view.remove_edge("p1/alignment1-aligns-p1/declaration1")
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN, self.reasons(promote())
        )

    def test_alignment_to_other_declaration(self):
        self.view.add_node("p1/declaration2", "FormalDeclaration")
        self.view.remove_edge("p1/alignment1-aligns-p1/declaration1")
        self.view.add_edge(
            "ALIGNS_DECLARATION", "p1/alignment1", "p1/declaration2", "p1/other"
        )
        findings = validate_proposal(propose(promote()), self.view)
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN,
            [finding.reason for finding in findings],
        )
        self.assertEqual(
            next(
                finding.op_index
                for finding in findings
                if finding.reason == Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN
            ),
            0,
        )

    def test_declaration_must_belong_to_replayed_certificate(self):
        self.view.add_node("p1/cert2", "Certificate", {"actor": PRODUCER})
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert2", "p1/proved2")
        self.view.remove_edge("p1/cert1-certifies-p1/declaration1")
        self.view.add_edge(
            "CERTIFIES", "p1/cert2", "p1/declaration1", "p1/certifies2"
        )
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN, self.reasons(promote())
        )
        self.assertNotIn(Reason.PROMOTION_WITHOUT_REPLAY, self.reasons(promote()))

    def test_matching_replayed_certificate_among_other_evidence(self):
        self.view.add_node("p1/declaration2", "FormalDeclaration")
        self.view.add_node(
            "p1/cert2", "Certificate",
            {
                "actor": PRODUCER, "status": "replay-accepted",
                "artifact_hash": ARTIFACT_HASH, "environment_hash": ENVIRONMENT_HASH,
                "producer_run_id": "p1/run1",
            },
        )
        self.view.add_node(
            "p1/replay2", "LeanReplay",
            {
                "actor": REPLAYER, "status": "verified",
                "sorry_detected": False, "environment_hash": ENVIRONMENT_HASH,
            },
        )
        self.view.add_edge("PROVED_BY", "p1/claim1", "p1/cert2", "p1/proved2")
        self.view.add_edge("REPLAYED_BY", "p1/cert2", "p1/replay2", "p1/replayed2")
        self.view.add_edge("PRODUCED_CERTIFICATE", "p1/run1", "p1/cert2", "p1/produced2")
        self.view.add_edge("CERTIFICATE_ENVIRONMENT", "p1/cert2", "p1/environment1", "p1/cert2-env")
        self.view.add_edge("REPLAY_ENVIRONMENT", "p1/replay2", "p1/environment1", "p1/replay2-env")
        self.view.remove_edge("p1/cert1-certifies-p1/declaration1")
        self.view.add_edge("CERTIFIES", "p1/cert1", "p1/declaration2", "p1/other")
        self.view.add_edge("CERTIFIES", "p1/cert2", "p1/declaration1", "p1/certifies2")
        self.assertEqual(self.reasons(promote()), [])

    def test_removing_declaration_link_in_promotion(self):
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN,
            self.reasons(
                RemoveEdge("CERTIFIES", "p1/cert1-certifies-p1/declaration1"),
                promote(),
            ),
        )

    def test_superseding_alignment_in_promotion(self):
        self.assertIn(
            Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN,
            self.reasons(
                SetField(
                    "Alignment", "p1/alignment1", "lifecycle", "superseded",
                    prior="reviewed",
                ),
                promote(),
            ),
        )

    def test_earlier_promotion_needs_only_reviewed_alignment(self):
        self.view.remove_edge("p1/cert1-certifies-p1/declaration1")
        self.view.set_field("p1/claim1", "status", "provisional")
        self.assertEqual(
            self.reasons(promote(to="formally-closed", prior="provisional")),
            [],
        )


class TestCertificatePromotion(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()
        wire(self.view)

    def reasons(self) -> set[Reason]:
        return {f.reason for f in validate_proposal(propose(promote()), self.view)}

    def test_certificate_status_must_be_replay_accepted(self):
        self.view.set_field("p1/cert1", "status", "stale")
        self.assertIn(Reason.PROMOTION_WITHOUT_VALID_CERTIFICATE, self.reasons())

    def test_certificate_needs_valid_artifact_and_environment_hashes(self):
        for field in ("artifact_hash", "environment_hash"):
            with self.subTest(field=field):
                view = MemoryView()
                wire(view)
                view.set_field("p1/cert1", field, "not-a-hash")
                reasons = {f.reason for f in validate_proposal(propose(promote()), view)}
                self.assertIn(Reason.PROMOTION_WITHOUT_VALID_CERTIFICATE, reasons)

    def test_existing_certificate_needs_a_producing_run(self):
        self.view.remove_edge("p1/run1-produced-p1/cert1")
        self.assertIn(Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING, self.reasons())

    def test_existing_certificate_needs_a_declaration_pin(self):
        self.view.remove_edge("p1/declaration1-pinned-p1/environment1")
        self.assertIn(Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING, self.reasons())

    def test_existing_certificate_environment_drift_is_rejected(self):
        self.view.set_field("p1/cert1", "environment_hash", "sha256:" + "22" * 32)
        self.assertIn(Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING, self.reasons())

    def test_run_must_search_the_certified_declaration(self):
        self.view.add_node("p1/declaration2", "FormalDeclaration")
        self.view.remove_edge("p1/run1-searches-p1/declaration1")
        self.view.add_edge("SEARCHES", "p1/run1", "p1/declaration2", "p1/run1-other")
        self.assertIn(Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING, self.reasons())

    def test_replay_must_bind_to_the_same_environment(self):
        self.view.remove_edge("p1/replay1-under-p1/environment1")
        self.assertIn(Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING, self.reasons())


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
                {
                    "actor": PRODUCER, "producer_run_id": "p1/run1",
                    "status": "replay-accepted", "artifact_hash": ARTIFACT_HASH,
                    "environment_hash": ENVIRONMENT_HASH,
                },
            ),
            UpsertNode(
                "FormalRun", "p1/run1",
                {"status": "proved-pending-replay", "environment_hash": ENVIRONMENT_HASH},
            ),
            UpsertNode(
                "Environment", "p1/environment1",
                {"environment_hash": ENVIRONMENT_HASH},
            ),
            UpsertNode(
                "LeanReplay",
                "p1/replay1",
                {
                    "actor": REPLAYER,
                    "status": "verified",
                    "sorry_detected": False,
                    "environment_hash": ENVIRONMENT_HASH,
                    "replayed_at": "2026-08-24T00:00:00Z",
                },
            ),
            UpsertNode(
                "Alignment",
                "p1/alignment1",
                {"lifecycle": "reviewed", "verdict": "aligned", "actor": "reviewer"},
            ),
            UpsertNode("FormalDeclaration", "p1/declaration1", {}),
            AddEdge("PROVED_BY", "p1/claim1", "p1/cert1", "p1/e1"),
            AddEdge("REPLAYED_BY", "p1/cert1", "p1/replay1", "p1/e2"),
            AddEdge("ALIGNS_CLAIM", "p1/alignment1", "p1/claim1", "p1/e3"),
            AddEdge("CERTIFIES", "p1/cert1", "p1/declaration1", "p1/e4"),
            AddEdge("ALIGNS_DECLARATION", "p1/alignment1", "p1/declaration1", "p1/e5"),
            AddEdge("PRODUCED_CERTIFICATE", "p1/run1", "p1/cert1", "p1/e6"),
            AddEdge("SEARCHES", "p1/run1", "p1/declaration1", "p1/e7"),
            AddEdge("PINNED_ENVIRONMENT", "p1/declaration1", "p1/environment1", "p1/e8"),
            AddEdge("CERTIFICATE_ENVIRONMENT", "p1/cert1", "p1/environment1", "p1/e9"),
            AddEdge("RAN_UNDER", "p1/run1", "p1/environment1", "p1/e10"),
            AddEdge("REPLAY_ENVIRONMENT", "p1/replay1", "p1/environment1", "p1/e11"),
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
