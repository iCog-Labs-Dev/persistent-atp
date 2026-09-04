import unittest
from neo4j_adapter.adapter import Neo4jAdapter
from neo4j_adapter.constants import (
    STATE_CLOSED, STATE_TAINTED, STATE_REOPENED,
    MOVE_CLOSED, MOVE_OPEN, MOVE_LEASED, MOVE_REFUTED,
    CLAIM_REFUTED, CLAIM_TAINTED,
)


class TestRules(unittest.TestCase):
    PROOF_ID = "test-proof-rules"

    def setUp(self):
        self.adapter = Neo4jAdapter()
        self.adapter.init_proof(self.PROOF_ID, "test kernel", event_id="ev0")

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
        self.adapter.close()

    # -- OR rule ------------------------------------------------------

    def test_state_is_solved_true_when_a_proposed_move_is_closed(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "try X", event_id="e2")
        self.adapter.update_move_status("m1", MOVE_CLOSED, proof_id=self.PROOF_ID, event_id="e3")

        self.assertTrue(self.adapter.state_is_solved(self.PROOF_ID, "s1"))

    def test_state_is_solved_false_when_no_move_closed(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "try X", event_id="e2")

        self.assertFalse(self.adapter.state_is_solved(self.PROOF_ID, "s1"))

    # -- AND rule -------------------------------------------------------

    def test_move_is_complete_true_when_all_subgoals_closed(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "split", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sg1", "case 1", event_id="e3")
        self.adapter.update_state_status(self.PROOF_ID, "sg1", STATE_CLOSED, event_id="e4")

        self.assertTrue(self.adapter.move_is_complete(self.PROOF_ID, "m1"))

    def test_move_is_complete_false_with_an_open_subgoal(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "split", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sg1", "case 1", event_id="e3")
        self.assertFalse(self.adapter.move_is_complete(self.PROOF_ID, "m1"))

    def test_move_is_complete_treats_reopened_subgoal_as_still_open(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "split", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sg1", "case 1", event_id="e3")
        self.adapter.update_state_status(self.PROOF_ID, "sg1", STATE_CLOSED, event_id="e4")
        self.adapter.reopen_state(self.PROOF_ID, "sg1", reason="counterexample", event_id="e5")

        self.assertFalse(self.adapter.move_is_complete(self.PROOF_ID, "m1"))

    # -- close_state cascade (AND then OR to fixpoint) -------------------

    def test_close_state_cascades_through_and_then_or(self):
        """root --PROPOSES--> m1 --REQUIRES--> sg1

        Closing sg1 directly should: close m1 (AND, its only subgoal is
        closed) -> close root (OR, its only move is now closed).
        """
        self.adapter.add_state(self.PROOF_ID, "root", "root goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "root", "split", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sg1", "case 1", event_id="e3")

        self.adapter.close_state("sg1", self.PROOF_ID, reason="proved", event_id="e4")

        m1 = self.adapter.get_moves_for_state("root", self.PROOF_ID)[0]
        self.assertEqual(m1["status"], MOVE_CLOSED)

        root = self.adapter.get_state("root", self.PROOF_ID)
        self.assertEqual(root["status"], STATE_CLOSED)

    def test_close_state_respects_max_iter_bound_when_propagation_is_capped(self):
        """Build a deep 4-level linear AND/OR goal tree:
           s0 <- m1 <- s1 <- m2 <- s2 <- m3 <- s3

        Closing s3 requires 3 iterations of the AND/OR loop to reach s0.
        If max_iter=1, only m3 and s2 close; s0 and s1 remain open.
        """
        self.adapter.add_state(self.PROOF_ID, "s0", "root", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s0", "move 1", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "s1", "subgoal 1", event_id="e3")

        self.adapter.add_move(self.PROOF_ID, "m2", "s1", "move 2", event_id="e4")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m2", "s2", "subgoal 2", event_id="e5")

        self.adapter.add_move(self.PROOF_ID, "m3", "s2", "move 3", event_id="e6")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m3", "s3", "subgoal 3", event_id="e7")

        # Manually invoke fixpoint with max_iter=1 to verify bounded propagation
        self.adapter.update_state_status(self.PROOF_ID, "s3", STATE_CLOSED, event_id="e8")
        self.adapter._propagate_closures(self.PROOF_ID, event_id="e9", max_iter=1)

        # Iteration 1 closed m3 and s2
        m3 = next(m for m in self.adapter.get_moves_for_state("s2", self.PROOF_ID) if m["id"] == "m3")
        s2 = self.adapter.get_state("s2", self.PROOF_ID)
        self.assertEqual(m3["status"], MOVE_CLOSED)
        self.assertEqual(s2["status"], STATE_CLOSED)

        # Root state s0 remains unclosed due to iteration cap
        s0 = self.adapter.get_state("s0", self.PROOF_ID)
        self.assertNotEqual(s0["status"], STATE_CLOSED)

    def test_close_state_does_not_close_unrelated_sibling_branch(self):
        self.adapter.add_state(self.PROOF_ID, "root", "root goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "root", "branch A", event_id="e2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sgA", "A's subgoal", event_id="e3")
        self.adapter.add_move(self.PROOF_ID, "m2", "root", "branch B", event_id="e4")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m2", "sgB", "B's subgoal", event_id="e5")

        self.adapter.close_state("sgA", self.PROOF_ID, event_id="e6")

        m2 = next(m for m in self.adapter.get_moves_for_state("root", self.PROOF_ID) if m["id"] == "m2")
        self.assertNotEqual(m2["status"], MOVE_CLOSED)
        root = self.adapter.get_state("root", self.PROOF_ID)
        self.assertEqual(root["status"], STATE_CLOSED)  

    # -- taint propagation ------------------------------------------------

    def test_propagate_taint_refutes_taints_and_reopens(self):
        """c2 DEPENDS_ON c1; s1 USES_CLAIM c2 and is closed.

        Refuting c1 should taint c2 (transitive dependent) and reopen s1
        (it was closed while depending on a now-tainted claim).
        """
        self.adapter.add_claim(self.PROOF_ID, "c1", "base claim", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "dependent claim", event_id="e2")
        self.adapter.add_claim_dependency("c2", "c1", proof_id=self.PROOF_ID, event_id="e3")

        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e4")
        self.adapter.link_state_claim(self.PROOF_ID, "s1", "c2", event_id="e5")
        self.adapter.update_state_status(self.PROOF_ID, "s1", STATE_CLOSED, event_id="e6")

        result = self.adapter.propagate_taint(self.PROOF_ID, "c1", event_id="e7", reason="counterexample found")

        self.assertIn("c2", result["tainted"])
        self.assertIn("s1", result["reopened_states"])

        c1 = next(c for c in self.adapter.get_all_claims(self.PROOF_ID) if c["id"] == "c1")
        self.assertEqual(c1["status"], CLAIM_REFUTED)

        s1 = self.adapter.get_state("s1", self.PROOF_ID)
        self.assertEqual(s1["status"], STATE_REOPENED)

    def test_multi_level_claim_dependency_cascade(self):
        """Deep claim dependency chain: c4 -> c3 -> c2 -> c1

        Closed state s1 uses c4, closed state s2 uses c3.
        Refuting root claim c1 must:
        1. Set c1 status to CLAIM_REFUTED
        2. Taint all downstream claims [c2, c3, c4]
        3. Reopen all closed states [s1, s2] relying on any tainted claim
        """
        self.adapter.add_claim(self.PROOF_ID, "c1", "leaf claim", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "mid-low claim", event_id="e2")
        self.adapter.add_claim(self.PROOF_ID, "c3", "mid-high claim", event_id="e3")
        self.adapter.add_claim(self.PROOF_ID, "c4", "top claim", event_id="e4")

        self.adapter.add_claim_dependency("c2", "c1", proof_id=self.PROOF_ID, event_id="e5")
        self.adapter.add_claim_dependency("c3", "c2", proof_id=self.PROOF_ID, event_id="e6")
        self.adapter.add_claim_dependency("c4", "c3", proof_id=self.PROOF_ID, event_id="e7")

        self.adapter.add_state(self.PROOF_ID, "s1", "top state", event_id="e8")
        self.adapter.link_state_claim(self.PROOF_ID, "s1", "c4", event_id="e9")
        self.adapter.update_state_status(self.PROOF_ID, "s1", STATE_CLOSED, event_id="e10")

        self.adapter.add_state(self.PROOF_ID, "s2", "mid state", event_id="e11")
        self.adapter.link_state_claim(self.PROOF_ID, "s2", "c3", event_id="e12")
        self.adapter.update_state_status(self.PROOF_ID, "s2", STATE_CLOSED, event_id="e13")

        result = self.adapter.propagate_taint(self.PROOF_ID, "c1", event_id="e14", reason="found counterexample")

        # Verify all transitively dependent claims were tainted
        self.assertCountEqual(result["tainted"], ["c2", "c3", "c4"])

        # Verify all affected closed states were reopened
        self.assertCountEqual(result["reopened_states"], ["s1", "s2"])

        # Verify persisted statuses in the database
        all_claims = {c["id"]: c for c in self.adapter.get_all_claims(self.PROOF_ID)}
        self.assertEqual(all_claims["c1"]["status"], CLAIM_REFUTED)
        self.assertEqual(all_claims["c2"]["status"], CLAIM_TAINTED)
        self.assertEqual(all_claims["c3"]["status"], CLAIM_TAINTED)
        self.assertEqual(all_claims["c4"]["status"], CLAIM_TAINTED)

        s1_node = self.adapter.get_state("s1", self.PROOF_ID)
        s2_node = self.adapter.get_state("s2", self.PROOF_ID)
        self.assertEqual(s1_node["status"], STATE_REOPENED)
        self.assertEqual(s2_node["status"], STATE_REOPENED)

    def test_taint_cone_returns_transitive_dependents(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "base", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "mid", event_id="e2")
        self.adapter.add_claim(self.PROOF_ID, "c3", "top", event_id="e3")
        self.adapter.add_claim_dependency("c2", "c1", proof_id=self.PROOF_ID, event_id="e4")
        self.adapter.add_claim_dependency("c3", "c2", proof_id=self.PROOF_ID, event_id="e5")

        cone = self.adapter.taint_cone(self.PROOF_ID, "c1")
        self.assertCountEqual(cone, ["c2", "c3"])

    def test_propagate_taint_leaves_unrelated_states_untouched(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "base", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c_unrelated", "unrelated", event_id="e2")
        self.adapter.add_state(self.PROOF_ID, "s_unrelated", "goal", event_id="e3")
        self.adapter.link_state_claim(self.PROOF_ID, "s_unrelated", "c_unrelated", event_id="e4")
        self.adapter.update_state_status(self.PROOF_ID, "s_unrelated", STATE_CLOSED, event_id="e5")

        self.adapter.propagate_taint(self.PROOF_ID, "c1", event_id="e6")

        s_unrelated = self.adapter.get_state("s_unrelated", self.PROOF_ID)
        self.assertEqual(s_unrelated["status"], STATE_CLOSED)

    # -- eligible_frontier -------------------------------------------------

    def test_eligible_frontier_excludes_leased_refuted_dominated_exhausted(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m_open", "s1", "open move", event_id="e2", status=MOVE_OPEN)
        self.adapter.add_move(self.PROOF_ID, "m_leased", "s1", "leased move", event_id="e3", status=MOVE_OPEN)
        self.adapter.update_move_status("m_leased", MOVE_LEASED, proof_id=self.PROOF_ID, event_id="e4")
        self.adapter.add_move(self.PROOF_ID, "m_refuted", "s1", "refuted move", event_id="e5", status=MOVE_OPEN)
        self.adapter.update_move_status("m_refuted", MOVE_REFUTED, proof_id=self.PROOF_ID, event_id="e6")

        frontier_ids = [m["id"] for m in self.adapter.eligible_frontier(self.PROOF_ID)]
        self.assertIn("m_open", frontier_ids)
        self.assertNotIn("m_leased", frontier_ids)
        self.assertNotIn("m_refuted", frontier_ids)

    def test_eligible_frontier_excludes_moves_on_tainted_or_closed_states(self):
        self.adapter.add_state(self.PROOF_ID, "s_open", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s_open", "move", event_id="e2", status=MOVE_OPEN)

        self.adapter.add_state(self.PROOF_ID, "s_tainted", "goal2", event_id="e3")
        self.adapter.add_move(self.PROOF_ID, "m2", "s_tainted", "move2", event_id="e4", status=MOVE_OPEN)
        self.adapter.update_state_status(self.PROOF_ID, "s_tainted", STATE_TAINTED, event_id="e5")

        frontier_ids = [m["id"] for m in self.adapter.eligible_frontier(self.PROOF_ID)]
        self.assertIn("m1", frontier_ids)
        self.assertNotIn("m2", frontier_ids)
    def test_eligible_frontier_includes_reopened_moves(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="e1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "move", event_id="e2", status=MOVE_OPEN)
        self.adapter.update_move_status("m1", MOVE_CLOSED, proof_id=self.PROOF_ID, event_id="e3")
        from neo4j_adapter.constants import MOVE_REOPENED
        self.adapter.update_move_status("m1", MOVE_REOPENED, proof_id=self.PROOF_ID, event_id="e4")

        frontier_ids = [m["id"] for m in self.adapter.eligible_frontier(self.PROOF_ID)]
        self.assertIn("m1", frontier_ids)

    # -- cycle detection --------------------------------------------------

    def test_would_create_cycle_false_for_acyclic_dependency(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "a", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "b", event_id="e2")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID, event_id="e3")

        self.assertFalse(
            self.adapter._would_create_cycle("c2", "c3", self.PROOF_ID)
        )

    def test_would_create_cycle_false_when_no_path_exists(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "a", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "b", event_id="e2")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID, event_id="e3")

        self.assertFalse(self.adapter._would_create_cycle("c1", "c2", self.PROOF_ID))

    def test_would_create_cycle_true_when_reverse_edge_exists(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "a", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "b", event_id="e2")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID, event_id="e3")

        self.assertTrue(self.adapter._would_create_cycle("c2", "c1", self.PROOF_ID))
    def test_would_create_cycle_true_for_longer_transitive_chain(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "a", event_id="e1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "b", event_id="e2")
        self.adapter.add_claim(self.PROOF_ID, "c3", "c", event_id="e3")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID, event_id="e4")
        self.adapter.add_claim_dependency("c2", "c3", proof_id=self.PROOF_ID, event_id="e5")

        self.assertTrue(self.adapter._would_create_cycle("c3", "c1", self.PROOF_ID))

if __name__ == "__main__":
    unittest.main()