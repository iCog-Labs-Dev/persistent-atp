import unittest

from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import ConcurrencyError, JournalStore

class TestApply(unittest.TestCase):
    @staticmethod
    def _journal(*ops, event_proof_id="p1"):
        payload = Proposal(event_proof_id, "test", "coordinator", tuple(ops)).to_dict()

        class Journal:
            def read_events_between(self, proof_id, after_revision, through_revision):
                if (proof_id, after_revision, through_revision) != ("p1", 0, 1):
                    raise ValueError("missing journal revision")
                return [payload]

        return Journal()

    def test_apply_journal_event_publishes_state_and_revision_together(self):
        view = MemoryView()
        journal = self._journal(UpsertNode("FormalState", "p1/fs1", {"status": "open"}))
        view.apply_journal_event(journal, "p1", 1)
        revision, snapshot = view.snapshot("p1")
        self.assertEqual(revision, 1)
        self.assertEqual(snapshot.node("p1/fs1").fields["status"], "open")
        self.assertFalse(hasattr(view, "mark_revision"))
        self.assertFalse(hasattr(view, "apply_event"))

    def test_failed_journal_event_keeps_state_and_revision(self):
        view = MemoryView()
        with self.assertRaises(ValueError):
            view.apply_journal_event(
                self._journal(
                    UpsertNode("FormalState", "p1/fs1", {"status": "open"}),
                    event_proof_id="p2",
                ),
                "p1", 1,
            )
        with self.assertRaises(ValueError):
            view.apply_journal_event(self._journal(), "p1", 1)
        ops = [
            UpsertNode("FormalState", "p1/fs1", {"status": "open"}),
            SetField("FormalState", "p1/missing", "status", "closed"),
        ]
        with self.assertRaises(ValueError):
            view.apply_journal_event(self._journal(*ops), "p1", 1)
        revision, snapshot = view.snapshot("p1")
        self.assertEqual(revision, 0)
        self.assertIsNone(snapshot.node("p1/fs1"))

    def test_missing_journal_event_cannot_advance_checkpoint(self):
        view = MemoryView()
        with self.assertRaises(ConcurrencyError):
            view.apply_journal_event(JournalStore(), "p1", 1)
        self.assertEqual(view.snapshot("p1")[0], 0)

    def test_snapshot_isolated_from_mutable_fields(self):
        view = MemoryView()
        fields = {"status": "open", "metadata": {"source": "original"}}
        view.add_node("p1/fs1", "FormalState", fields)
        view.add_edge("HAS_TACTIC", "p1/fs1", "p1/ta1", "p1/e1", fields)
        revision, snapshot = view.snapshot("p1")
        fields["metadata"]["source"] = "caller changed"
        view.nodes["p1/fs1"].fields["metadata"]["source"] = "live changed"
        view.edges["p1/e1"].fields["metadata"]["source"] = "live changed"
        self.assertEqual(revision, 0)
        self.assertEqual(snapshot.node("p1/fs1").fields["metadata"]["source"], "original")
        self.assertEqual(snapshot.edge("p1/e1").fields["metadata"]["source"], "original")
        with self.assertRaises(TypeError):
            snapshot.node("p1/fs1").fields["status"] = "closed"

    def test_apply_upsert_node(self):
        view = MemoryView()
        apply_ops(view, [UpsertNode("FormalState", "fs1", {"status": "open"})])
        node = view.node("fs1")
        self.assertIsNotNone(node)
        self.assertEqual(node.label, "FormalState")
        self.assertEqual(node.fields["status"], "open")

    def test_apply_set_field(self):
        view = MemoryView()
        view.add_node("fs1", "FormalState", {"status": "open"})
        apply_ops(view, [SetField("FormalState", "fs1", "status", "closed")])
        self.assertEqual(view.node("fs1").fields["status"], "closed")

    def test_apply_set_field_unknown_node(self):
        view = MemoryView()
        with self.assertRaises(ValueError):
            apply_ops(view, [SetField("FormalState", "fs1", "status", "closed")])

    def test_apply_add_edge(self):
        view = MemoryView()
        view.add_node("ta1", "TacticApplication", {})
        view.add_node("fs1", "FormalState", {})
        apply_ops(view, [AddEdge("HAS_TACTIC", "fs1", "ta1", "e1")])
        edge = view.edge("e1")
        self.assertIsNotNone(edge)
        self.assertEqual(edge.rel_type, "HAS_TACTIC")
        self.assertEqual(edge.src_id, "fs1")
        self.assertEqual(edge.dst_id, "ta1")
        
        edges_from = view.edges_from("fs1", "HAS_TACTIC")
        self.assertEqual(len(edges_from), 1)

    def test_apply_remove_edge(self):
        view = MemoryView()
        view.add_edge("HAS_TACTIC", "fs1", "ta1", "e1")
        apply_ops(view, [RemoveEdge("HAS_TACTIC", "e1")])
        self.assertIsNone(view.edge("e1"))
        self.assertEqual(len(view.edges_from("fs1", "HAS_TACTIC")), 0)

    def test_idempotent_apply(self):
        view = MemoryView()
        ops = [
            UpsertNode("FormalState", "fs1", {"status": "open"}),
            AddEdge("HAS_TACTIC", "fs1", "ta1", "e1")
        ]
        apply_ops(view, ops)
        apply_ops(view, ops) # apply again
        
        self.assertIsNotNone(view.node("fs1"))
        self.assertIsNotNone(view.edge("e1"))
        self.assertEqual(len(view.edges_from("fs1", "HAS_TACTIC")), 1)

    def test_apply_upsert_existing_node_matching(self):
        view = MemoryView()
        view.add_node("fs1", "FormalState", {"status": "open"})
        apply_ops(view, [UpsertNode("FormalState", "fs1", {"status": "open"})])
        node = view.node("fs1")
        self.assertIsNotNone(node)
        self.assertEqual(node.fields["status"], "open")

if __name__ == "__main__":
    unittest.main()
