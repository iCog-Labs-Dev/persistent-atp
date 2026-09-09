import unittest

from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.state import MemoryView

class TestApply(unittest.TestCase):
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
