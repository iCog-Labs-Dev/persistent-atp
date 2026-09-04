
import unittest
from neo4j_adapter.adapter import Neo4jAdapter


class TestReplay(unittest.TestCase):
    PROOF_ID = "test-proof-replay"

    SAMPLE_EVENTS = [
        {"id": "ev1", "type": "project_init",
         "payload": {"theorem_kernel": "n + 0 = n"}},
        {"id": "ev2", "type": "state_added",
         "payload": {"state": {"id": "root", "description": "root goal",
                                "parent": None, "kind": "or", "status": "open"}}},
        {"id": "ev3", "type": "claim_added",
         "payload": {"claim": {"id": "c1", "statement": "base case holds",
                                "status": "conjectural"}}},
        {"id": "ev4", "type": "move_added",
         "payload": {"move": {"id": "m1", "state_id": "root",
                               "move_summary": "induction", "status": "open"}}},
        {"id": "ev5", "type": "attempt_recorded",
         "payload": {"attempt": {"id": "a1", "state_id": "root",
                                  "move_summary": "induction", "worker": "explorer"}}},
        {"id": "ev6", "type": "attempt_updated",
         "payload": {"attempt": {"id": "a1", "status": "supported"}}},
    ]

    def setUp(self):
        self.adapter = Neo4jAdapter()

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
        self.adapter.close()

    def _graph_snapshot(self):
        """A comparable summary of everything under this proof_id."""
        with self.adapter._driver.session() as s:
            nodes = s.run(
                "MATCH (n {proof_id: $pid}) RETURN labels(n) AS labels, n.id AS id "
                "ORDER BY id", pid=self.PROOF_ID,
            ).data()
            rels = s.run(
                "MATCH (a {proof_id: $pid})-[r]->(b {proof_id: $pid}) "
                "RETURN type(r) AS type, a.id AS src, b.id AS dst "
                "ORDER BY type, src, dst", pid=self.PROOF_ID,
            ).data()
        return nodes, rels

    def test_wipe_and_rebuild_deletes_existing_nodes_first(self):
        self.adapter.add_state(self.PROOF_ID, "stray", "leftover from a prior run", event_id="ev0")
        self.adapter.wipe_and_rebuild(self.PROOF_ID, self.SAMPLE_EVENTS)

        state_ids = [n["id"] for n in self._graph_snapshot()[0] if "State" in n["labels"]]
        self.assertNotIn("stray", state_ids)
        self.assertIn("root", state_ids)

    def test_replay_dispatches_project_init_and_state_added(self):
        self.adapter.wipe_and_rebuild(self.PROOF_ID, self.SAMPLE_EVENTS[:2])
        root = self.adapter.get_state("root", self.PROOF_ID)
        self.assertIsNotNone(root)
        self.assertEqual(root["description"], "root goal")

    def test_replay_dispatches_claim_move_and_attempt_events(self):
        self.adapter.wipe_and_rebuild(self.PROOF_ID, self.SAMPLE_EVENTS)

        claims = self.adapter.get_all_claims(self.PROOF_ID)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["id"], "c1")

        moves = self.adapter.get_moves_for_state("root", self.PROOF_ID)
        self.assertEqual(len(moves), 1)

        attempts = self.adapter.get_attempts_for_state("root", self.PROOF_ID)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "supported")

    def test_replay_is_idempotent(self):
        self.adapter.wipe_and_rebuild(self.PROOF_ID, self.SAMPLE_EVENTS)
        first = self._graph_snapshot()

        self.adapter.wipe_and_rebuild(self.PROOF_ID, self.SAMPLE_EVENTS)
        second = self._graph_snapshot()

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()