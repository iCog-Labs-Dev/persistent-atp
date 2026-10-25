"""The stagnation policy, rule by rule, over committed-state fixtures (Section 10.4)."""

import unittest

from commit_gate.state import MemoryView

from coordinator.policy import (
    ALIGNMENT_REVIEW,
    BRIDGE_LEMMA,
    DIVERSIFY,
    FIVE_MECHANISMS,
    GLOBAL_AUDIT,
    REPAIR,
    StagnationPolicy,
    classify,
)
from coordinator.progress import ProofReading, read_proof


def proof() -> MemoryView:
    view = MemoryView()
    view.add_node("p1/fd-1", "FormalDeclaration", {"status": "searching"})
    return view


def stuck_run(view, run, *obstructions, status="stagnated", declaration="p1/fd-1"):
    """A run on `declaration` that raised each (id, kind, state, signature)."""
    view.add_node(run, "FormalRun", {"status": status})
    view.add_edge("SEARCHES", run, declaration, f"{run}-searches")
    for obstruction_id, kind, state_id, signature in obstructions:
        if view.node(state_id) is None:
            view.add_node(state_id, "FormalState", {"status": "open", "semantic_signature": signature})
        view.add_node(obstruction_id, "Obstruction", {"kind": kind})
        view.add_edge("RAISED_OBSTRUCTION", run, obstruction_id, f"{obstruction_id}-raised")
        view.add_edge("AT_STATE", obstruction_id, state_id, f"{obstruction_id}-at")


def rules(view, *, stalled_rounds=0, frontier_empty=False, policy=StagnationPolicy()):
    reading = read_proof(view, "p1", 0)
    return [
        (escalation.rule, escalation.target_id)
        for escalation in classify(
            view,
            "p1",
            reading,
            stalled_rounds=stalled_rounds,
            frontier_empty=frontier_empty,
            policy=policy,
        )
    ]


class TestRules(unittest.TestCase):
    def test_a_quiet_proof_raises_nothing(self):
        self.assertEqual(rules(proof()), [])

    def test_rule_1_a_refuted_attempt_is_repaired_at_its_gap(self):
        view = proof()
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        view.add_node(
            "p1/at-1",
            "Attempt",
            {"worker_class": "critic", "status": "refuted", "earliest_gap": "step 3"},
        )
        view.add_edge("REVIEWS_CLAIM", "p1/at-1", "p1/c-1", "p1/at-1-reviews")

        (escalation,) = classify(view, "p1", read_proof(view, "p1", 0), stalled_rounds=0, frontier_empty=False)

        self.assertEqual((escalation.rule, escalation.target_id), (REPAIR, "p1/at-1"))
        self.assertEqual(escalation.claim_id, "p1/c-1")
        self.assertIn("step 3", escalation.detail)

    def test_rule_1_a_claim_already_refuted_needs_no_repair(self):
        view = proof()
        view.add_node("p1/c-1", "Claim", {"status": "refuted"})
        view.add_node("p1/at-1", "Attempt", {"worker_class": "critic", "status": "refuted"})
        view.add_edge("REVIEWS_CLAIM", "p1/at-1", "p1/c-1", "p1/at-1-reviews")
        self.assertEqual(rules(view), [])

    def test_rule_2_two_rounds_without_progress_ask_for_five_mechanisms(self):
        self.assertEqual(rules(proof(), stalled_rounds=1), [])
        self.assertEqual(rules(proof(), stalled_rounds=2), [(FIVE_MECHANISMS, "p1")])

    def test_rule_3_a_repeated_failure_family_diversifies_the_search(self):
        view = proof()
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "typeclass", "p1/fs-1", "s1"), status="searching")
        self.assertEqual(rules(view), [])
        stuck_run(view, "p1/fr-2", ("p1/obs-2", "typeclass", "p1/fs-2", "s2"), status="searching")
        self.assertEqual(rules(view), [(DIVERSIFY, "p1/fd-1")])

    def test_rule_4_a_stagnated_run_missing_a_lemma_asks_for_a_bridge(self):
        view = proof()
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "missing-lemma", "p1/fs-2", "s2"))
        self.assertEqual(rules(view), [(BRIDGE_LEMMA, "p1/fs-2")])

    def test_rule_4_runs_converging_on_one_residual_ask_for_a_bridge(self):
        view = proof()
        # Two searching runs, different states, one semantic residual.
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "search-policy", "p1/fs-1", "same"), status="searching")
        stuck_run(view, "p1/fr-2", ("p1/obs-2", "resource", "p1/fs-2", "same"), status="searching")
        (escalation,) = classify(view, "p1", read_proof(view, "p1", 0), stalled_rounds=0, frontier_empty=False)
        self.assertEqual((escalation.rule, escalation.target_id), (BRIDGE_LEMMA, "p1/fs-1"))
        self.assertIn("2 runs converge", escalation.detail)

    def test_rule_4_a_representation_mismatch_asks_for_a_representation_change(self):
        view = proof()
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "representation-mismatch", "p1/fs-1", "s"))
        (escalation,) = classify(view, "p1", read_proof(view, "p1", 0), stalled_rounds=0, frontier_empty=False)
        self.assertIn("representation change", escalation.detail)

    def test_rule_5_a_doubted_statement_is_reviewed_and_holds_more_search(self):
        view = proof()
        stuck_run(
            view,
            "p1/fr-1",
            ("p1/obs-1", "missing-lemma", "p1/fs-1", "s1"),
            ("p1/obs-2", "statement-too-strong", "p1/fs-1", "s1"),
            ("p1/obs-3", "typeclass", "p1/fs-1", "s1"),
            ("p1/obs-4", "typeclass", "p1/fs-1", "s1"),
        )
        # No bridge lemma or diversified search for a statement in doubt.
        self.assertEqual(rules(view), [(ALIGNMENT_REVIEW, "p1/fd-1")])

    def test_rule_5_a_reviewed_mismatch_is_reviewed_again(self):
        view = proof()
        view.add_node("p1/al-1", "Alignment", {"lifecycle": "reviewed", "verdict": "weaker"})
        view.add_edge("ALIGNS_DECLARATION", "p1/al-1", "p1/fd-1", "p1/al-1-aligns")
        self.assertEqual(rules(view), [(ALIGNMENT_REVIEW, "p1/fd-1")])

    def test_rule_6_an_empty_frontier_or_a_long_stall_calls_an_audit(self):
        self.assertEqual(rules(proof(), frontier_empty=True), [(GLOBAL_AUDIT, "p1")])
        self.assertEqual(
            rules(proof(), stalled_rounds=4),
            [(FIVE_MECHANISMS, "p1"), (GLOBAL_AUDIT, "p1")],
        )


class TestPolicyOrder(unittest.TestCase):
    def test_escalations_come_in_rule_order(self):
        view = proof()
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "missing-lemma", "p1/fs-1", "s1"))
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        view.add_node("p1/at-1", "Attempt", {"worker_class": "critic", "status": "refuted"})
        view.add_edge("REVIEWS_CLAIM", "p1/at-1", "p1/c-1", "p1/at-1-reviews")

        self.assertEqual(
            [rule for rule, _ in rules(view, stalled_rounds=4, frontier_empty=True)],
            [REPAIR, FIVE_MECHANISMS, BRIDGE_LEMMA, GLOBAL_AUDIT],
        )

    def test_thresholds_are_configuration(self):
        eager = StagnationPolicy(non_progress_rounds=1)
        self.assertEqual(rules(proof(), stalled_rounds=1, policy=eager), [(FIVE_MECHANISMS, "p1")])

    def test_a_solved_proof_raises_nothing(self):
        view = MemoryView()
        view.add_node("p1/fd-1", "FormalDeclaration", {"status": "replay-accepted"})
        self.assertEqual(rules(view, stalled_rounds=9, frontier_empty=True), [])

    def test_a_certificate_awaiting_replay_is_not_stagnation(self):
        view = proof()
        view.add_node("p1/cert-1", "Certificate", {"status": "candidate"})
        self.assertEqual(rules(view, stalled_rounds=9, frontier_empty=True), [])


class TestOnRecord(unittest.TestCase):
    def coordinator_move(self, view, move_id, rule, target, basis="0/0/0", status="exhausted"):
        if view.node("p1/rs-1") is None:
            view.add_node("p1/rs-1", "ResearchState", {"status": "open", "role": "coordinator"})
        view.add_node(
            move_id,
            "ResearchMove",
            {"status": status, "escalation": rule, "target_id": target, "basis": basis},
        )
        view.add_edge("PROPOSES", "p1/rs-1", move_id, f"{move_id}-proposed")

    def test_a_local_escalation_is_raised_once_per_target(self):
        view = proof()
        stuck_run(view, "p1/fr-1", ("p1/obs-1", "missing-lemma", "p1/fs-2", "s2"))
        self.coordinator_move(view, "p1/rm-1", BRIDGE_LEMMA, "p1/fs-2", basis="9/9/9")
        self.assertEqual(rules(view), [])

    def test_a_stall_escalation_is_raised_again_at_a_new_progress_level(self):
        view = proof()
        self.coordinator_move(view, "p1/rm-1", FIVE_MECHANISMS, "p1")
        self.assertEqual(rules(view, stalled_rounds=2), [])

        view.add_node("p1/c-9", "Claim", {"status": "critic-accepted"})
        self.assertEqual(read_proof(view, "p1", 0).basis, "1/0/0")
        self.assertEqual(rules(view, stalled_rounds=2), [(FIVE_MECHANISMS, "p1")])

    def test_moves_from_other_research_states_are_not_on_record(self):
        view = proof()
        view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
        view.add_node(
            "p1/rm-1",
            "ResearchMove",
            {"status": "queued", "escalation": FIVE_MECHANISMS, "target_id": "p1", "basis": "0/0/0"},
        )
        view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/rm-1-proposed")
        self.assertEqual(rules(view, stalled_rounds=2), [(FIVE_MECHANISMS, "p1")])


class TestReading(unittest.TestCase):
    def test_progress_counts_only_verified_gain(self):
        view = proof()
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        view.add_node("p1/c-2", "Claim", {"status": "refuted"})
        view.add_node("p1/fs-1", "FormalState", {"status": "failed"})
        before = read_proof(view, "p1", 0)
        self.assertEqual(before.progress, (0, 0, 0))

        view.set_field("p1/c-1", "status", "critic-accepted")
        view.add_node("p1/fs-2", "FormalState", {"status": "lean-verified"})
        after = read_proof(view, "p1", 1)
        self.assertEqual(after.progress, (1, 1, 0))
        self.assertTrue(after.advanced_from(before))
        self.assertFalse(before.advanced_from(after))

    def test_a_proof_is_solved_when_every_declaration_is_replayed(self):
        view = proof()
        view.add_node("p1/fd-2", "FormalDeclaration", {"status": "replay-accepted"})
        self.assertFalse(read_proof(view, "p1", 0).solved)
        view.set_field("p1/fd-1", "status", "replay-accepted")
        self.assertTrue(read_proof(view, "p1", 0).solved)
        self.assertFalse(ProofReading(revision=0).solved)

    def test_other_proofs_are_not_read(self):
        view = proof()
        view.add_node("p2/c-1", "Claim", {"status": "critic-accepted"})
        self.assertEqual(read_proof(view, "p1", 0).settled_claims, 0)


if __name__ == "__main__":
    unittest.main()
