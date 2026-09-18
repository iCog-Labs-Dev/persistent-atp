"""End-to-end: CommitGate -> JournalStore -> Neo4jProjector -> Neo4j.

Exercises the real integration path (no mocks for the store), committing
proposals through the gate and projecting them onto a live Neo4j instance.
"""

import unittest

import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode, AddEdge
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore

from neo4j_adapter.adapter import Neo4jAdapter
from neo4j_adapter.projector import Neo4jProjector


class TestEndToEnd(unittest.TestCase):
    PROOF_ID = "test-e2e-flow"

    def setUp(self):
        self.store = JournalStore(":memory:")
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.store)
        self.adapter = Neo4jAdapter()
        self.projector = Neo4jProjector(self.adapter, self.store)

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
            s.run("MATCH (w:ProjectionWatermark {proof_id: $pid}) DELETE w",
                  pid=self.PROOF_ID)
        self.adapter.close()
        self.store._conn.close()

    def _commit(self, ops, base_revision=0):
        result = self.gate.commit(Proposal(
            proof_id=self.PROOF_ID,
            actor="explorer",
            worker_class="search",
            ops=tuple(ops),
            base_revision=base_revision,
        ))
        self.assertTrue(result.accepted, result.rejections)
        return result.revision

    def test_commit_init_project_and_state_then_project(self):
        # 1. Worker proposes: create Proof + root State + HAS_STATE edge
        rev = self._commit([
            UpsertNode("Proof", f"{self.PROOF_ID}/{self.PROOF_ID}",
                       {"theorem_kernel": "n + 0 = n"}),
            UpsertNode("State", f"{self.PROOF_ID}/s1",
                       {"description": "root goal", "status": "open", "kind": "or"}),
            AddEdge("HAS_STATE", f"{self.PROOF_ID}/{self.PROOF_ID}",
                    f"{self.PROOF_ID}/s1",
                    f"{self.PROOF_ID}/e1"),
        ])
        self.assertEqual(rev, 1)

        # 2. Projector catches up
        applied = self.projector.catch_up(self.PROOF_ID)
        self.assertEqual(applied, 1)
        self.assertEqual(self.projector.watermark(self.PROOF_ID), 1)

        # 3. Assert the adapter graph has the proof + state
        state = self.adapter.get_state(f"{self.PROOF_ID}/s1", self.PROOF_ID)
        self.assertIsNotNone(state)
        self.assertEqual(state["description"], "root goal")

    def test_commit_claim_then_query_through_adapter(self):
        self._commit([
            UpsertNode("Proof", f"{self.PROOF_ID}/{self.PROOF_ID}",
                       {"theorem_kernel": "n + 0 = n"}),
            UpsertNode("Claim", f"{self.PROOF_ID}/c1",
                       {"statement": "n+0=n", "status": "conjectural"}),
            AddEdge("HAS_CLAIM", f"{self.PROOF_ID}/{self.PROOF_ID}",
                    f"{self.PROOF_ID}/c1",
                    f"{self.PROOF_ID}/ec1"),
        ])
        applied = self.projector.catch_up(self.PROOF_ID)
        self.assertEqual(applied, 1)

        # The claim node must exist on the graph under its proof scope.
        with self.adapter._driver.session() as s:
            row = s.run(
                "MATCH (c:Claim {proof_id: $pid, id: $cid}) RETURN c.statement AS stmt",
                pid=self.PROOF_ID, cid=f"{self.PROOF_ID}/c1",
            ).single()
        self.assertIsNotNone(row)
        self.assertEqual(row["stmt"], "n+0=n")


if __name__ == "__main__":
    unittest.main()
