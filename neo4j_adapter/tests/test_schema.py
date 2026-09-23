
import unittest
from neo4j_adapter.adapter import Neo4jAdapter
from neo4j_adapter.schema import LABELS


class TestSchema(unittest.TestCase):
    """Constraint/index setup runs once in Neo4jAdapter.__init__, so we just
    inspect what's actually registered on the live database.
    """

    def setUp(self):
        self.adapter = Neo4jAdapter()  # __init__ calls ensure_constraints

    def tearDown(self):
        self.adapter.close()

    def test_a_unique_constraint_exists_for_every_label(self):
        with self.adapter._driver.session() as s:
            names = {r["name"] for r in s.run("SHOW CONSTRAINTS YIELD name")}
        for label in LABELS:
            self.assertIn(f"{label.lower()}_key", names)
        self.assertEqual(len(LABELS), 13)  # matches the issue's acceptance criteria

    def test_status_indexes_exist_for_state_move_claim(self):
        with self.adapter._driver.session() as s:
            names = {r["name"] for r in s.run("SHOW INDEXES YIELD name")}
        for expected in ("state_status", "move_status", "claim_status"):
            self.assertIn(expected, names)

    def test_unique_constraint_actually_rejects_a_duplicate(self):
        pid = "test-proof-schema-uniqueness"
        try:
            with self.adapter._driver.session() as s:
                s.run("CREATE (:State {proof_id: $pid, id: 's1'})", pid=pid)
                with self.assertRaises(Exception):
                    s.run("CREATE (:State {proof_id: $pid, id: 's1'})", pid=pid)
        finally:
            with self.adapter._driver.session() as s:
                s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=pid)


if __name__ == "__main__":
    unittest.main()