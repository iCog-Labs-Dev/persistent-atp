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


class TestCriticGating(unittest.TestCase):
    """provisional -> critic-accepted needs a favorable critic verdict."""

    def setUp(self):
        self.view = MemoryView()
        self.view.add_node("p1/c-1", "Claim", {"status": "provisional"})

    def promote(self, *ops) -> list[Reason]:
        proposal = Proposal(
            proof_id="p1",
            actor="coordinator",
            worker_class="coordinator",
            ops=(SetField("Claim", "p1/c-1", "status", "critic-accepted", prior="provisional"),)
            + tuple(ops),
            base_revision=0,
            lease_id="lease-1",
            fencing_token=1,
        )
        return [f.reason for f in validate_proposal(proposal, self.view)]

    def critic_attempt(self, attempt_id: str, status: str = "critic-accepted") -> tuple:
        return (
            UpsertNode(
                "Attempt",
                attempt_id,
                {"actor": "critic-a", "worker_class": "critic", "status": status},
            ),
            AddEdge("REVIEWS_CLAIM", attempt_id, "p1/c-1", f"{attempt_id}-rev"),
        )

    def aligned(self, alignment_id: str = "p1/al-1") -> None:
        self.view.add_node(
            alignment_id,
            "Alignment",
            {"lifecycle": "reviewed", "verdict": "aligned"},
        )
        self.view.add_edge(
            "ALIGNS_CLAIM", alignment_id, "p1/c-1", f"{alignment_id}-aligns"
        )

    def test_promotion_without_a_verdict_is_rejected(self):
        self.assertIn(Reason.CRITIC_VERDICT_REQUIRED, self.promote())

    def test_promotion_with_attached_favorable_verdict_passes(self):
        self.aligned()
        self.assertEqual(self.promote(*self.critic_attempt("p1/at-1")), [])

    def test_promotion_on_a_committed_verdict_passes(self):
        self.view.add_node(
            "p1/at-9",
            "Attempt",
            {"worker_class": "critic", "status": "supported"},
        )
        self.view.add_edge("REVIEWS_CLAIM", "p1/at-9", "p1/c-1", "p1/e-rev")
        self.aligned()
        self.assertEqual(self.promote(), [])

    def test_a_pending_critique_is_not_a_verdict(self):
        reasons = self.promote(*self.critic_attempt("p1/at-1", status="pending"))
        self.assertIn(Reason.CRITIC_VERDICT_REQUIRED, reasons)

    def test_a_refuted_critique_cannot_promote(self):
        reasons = self.promote(*self.critic_attempt("p1/at-1", status="refuted"))
        self.assertIn(Reason.CRITIC_VERDICT_REQUIRED, reasons)

    def test_a_non_critic_reviewer_cannot_promote(self):
        attempt = (
            UpsertNode(
                "Attempt",
                "p1/at-1",
                {"actor": "llm-a", "worker_class": "llm-research", "status": "supported"},
            ),
            AddEdge("REVIEWS_CLAIM", "p1/at-1", "p1/c-1", "p1/e-rev"),
        )
        reasons = self.promote(*attempt)
        self.assertIn(Reason.CRITIC_VERDICT_REQUIRED, reasons)

    def test_other_claim_transitions_need_no_verdict(self):
        proposal = Proposal(
            proof_id="p1",
            actor="human",
            worker_class="human",
            ops=(
                SetField("Claim", "p1/c-1", "status", "refuted", prior="provisional"),
            ),
            base_revision=0,
            lease_id="lease-1",
            fencing_token=1,
        )
        findings = validate_proposal(proposal, self.view)
        self.assertNotIn(Reason.CRITIC_VERDICT_REQUIRED, [f.reason for f in findings])

    def test_unrelated_promotions_are_untouched(self):
        # A different claim's promotion does not satisfy this one.
        self.view.add_node("p1/c-2", "Claim", {"status": "provisional"})
        self.view.add_node(
            "p1/at-2", "Attempt", {"worker_class": "critic", "status": "supported"}
        )
        self.view.add_edge("REVIEWS_CLAIM", "p1/at-2", "p1/c-2", "p1/e-rev2")
        self.assertIn(Reason.CRITIC_VERDICT_REQUIRED, self.promote())


H1 = "sha256:" + "11" * 32
H2 = "sha256:" + "22" * 32


class TestEnvironmentBinding(unittest.TestCase):
    """C4: a certificate under the wrong environment hash is stale, not fresh."""

    def setUp(self):
        self.view = MemoryView()
        self.view.add_node("p1/env1", "Environment", {"environment_hash": H1})
        self.view.add_node("p1/env2", "Environment", {"environment_hash": H2})
        self.view.add_node("p1/fd1", "FormalDeclaration", {"status": "searching"})
        self.view.add_node("p1/run1", "FormalRun", {"status": "proved-pending-replay"})
        self.view.add_edge("PINNED_ENVIRONMENT", "p1/fd1", "p1/env1", "p1/e-pin")
        self.view.add_edge("SEARCHES", "p1/run1", "p1/fd1", "p1/e-search")

    def certificate(self, cert_id="p1/cert2", env="p1/env2", status="candidate"):
        fields = {
            "actor": "atp-worker-3",
            "producer_run_id": "p1/run1",
            "artifact_hash": "sha256:" + "aa" * 32,
            "environment_hash": H1 if env == "p1/env1" else H2,
            "status": status,
        }
        return [
            UpsertNode("Certificate", cert_id, fields),
            AddEdge("CERTIFICATE_ENVIRONMENT", cert_id, env, f"{cert_id}-env-{env}"),
            AddEdge("PRODUCED_CERTIFICATE", "p1/run1", cert_id, f"{cert_id}-from-run"),
        ]

    def reasons(self, ops) -> set:
        return {f.reason for f in validate_proposal(propose(*ops), self.view)}

    def test_c4_second_certificate_under_new_environment_is_rejected(self):
        found = self.reasons(self.certificate())
        self.assertIn(Reason.ENVIRONMENT_DRIFT, found)

    def test_c4_same_certificate_commits_as_stale(self):
        ops = self.certificate(status="stale")
        self.assertEqual(validate_proposal(propose(*ops), self.view), [])

    def test_c4_matching_environment_stays_fresh(self):
        ops = self.certificate(env="p1/env1")
        self.assertEqual(validate_proposal(propose(*ops), self.view), [])

    def test_c4_no_pinned_declaration_leaves_the_check_silent(self):
        self.view.remove_edge("p1/e-pin")
        self.assertNotIn(
            Reason.ENVIRONMENT_DRIFT, self.reasons(self.certificate())
        )

    def test_c4_status_flip_to_replay_accepted_on_drifted_cert_is_rejected(self):
        self.view.add_node(
            "p1/cert3",
            "Certificate",
            {
                "actor": "a",
                "producer_run_id": "p1/run1",
                "artifact_hash": "sha256:" + "bb" * 32,
                "environment_hash": H2,
                "status": "replay-pending",
            },
        )
        self.view.add_edge(
            "PRODUCED_CERTIFICATE", "p1/run1", "p1/cert3", "p1/e-pc3"
        )
        proposal = propose(
            SetField("Certificate", "p1/cert3", "status", "replay-accepted",
                     prior="replay-pending"),
        )
        found = {f.reason for f in validate_proposal(proposal, self.view)}
        self.assertIn(Reason.ENVIRONMENT_DRIFT, found)


if __name__ == "__main__":
    unittest.main()
