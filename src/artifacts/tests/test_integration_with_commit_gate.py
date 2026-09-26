"""End-to-end demonstration that the artifact store and the commit gate
actually work together, not just in isolation.

This is the scenario from the architecture doc, made real: a piece of
content (standing in for a Lean trace) is stored, referenced from a
proposal by its hash alone, accepted by the gate's validators, and then
retrieved again by that same hash — proving the full chain the rest of the
system will lean on.
"""

import os
import unittest

import psycopg

from artifacts.schema import apply_schema
from artifacts.store import ArtifactStore
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal

_TEST_DB_URL = os.environ.get(
    "ARTIFACT_DB_URL",
    "postgresql://atp_dev:devpassword@localhost:5432/persistent_atp_artifacts",
)


class TestArtifactReferencedFromAProposal(unittest.TestCase):
    def setUp(self):
        apply_schema(_TEST_DB_URL)
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")
        self.store = ArtifactStore(_TEST_DB_URL)

    def tearDown(self):
        self.store.close()
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")

    def test_stored_artifact_is_accepted_and_retrievable_via_the_gate(self):
        # 1. Real content, standing in for a Lean trace.
        trace = b"theorem foo : 1 + 1 = 2 := by decide\n-- trace diagnostics here"

        # 2. Store it before anything is proposed, exactly as the paper's
        #    flow describes: bytes go into the artifact store first, the
        #    graph only ever sees the hash.
        artifact_hash = self.store.put(trace)

        # 3. A committed FormalState node already exists in the graph.
        view = MemoryView()
        view.add_node("p17/fs1", "FormalState", {"goal_text": "1 + 1 = 2"})

        # 4. A proposal references the artifact by hash only, via the
        #    already-supported HAS_ARTIFACT / content-addressed pattern.
        proposal = Proposal(
            proof_id="p17",
            actor="atp-worker-3",
            worker_class="formal-atp",
            ops=(AddEdge("HAS_ARTIFACT", "p17/fs1", artifact_hash, "p17/e2"),),
            base_revision=0,
            lease_id="lease-1",
            fencing_token=1,
        )

        # 5. The gate accepts it: no rejections.
        findings = validate_proposal(proposal, view)
        self.assertEqual(findings, [])

        # 6. Independent replay, later: given only the hash from the
        #    committed graph, the exact original bytes come back.
        self.assertEqual(self.store.get(artifact_hash), trace)


if __name__ == "__main__":
    unittest.main()