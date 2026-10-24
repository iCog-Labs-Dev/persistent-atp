"""Proof-scoped enumeration on the in-memory views."""

import unittest

from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.state import JournalOverlayView, MemoryView, ReadView, belongs_to_proof

from .enumeration_contract import EnumerationContract, edge_rows, node_rows, populate_graph


class TestMemoryViewEnumeration(EnumerationContract, unittest.TestCase):
    def make_view(self):
        return MemoryView()

    def test_satisfies_the_read_view_protocol(self):
        self.assertIsInstance(self.view, ReadView)

    def test_listing_returns_immutable_copies(self):
        record = self.view.list_nodes(self.pa)[0]
        with self.assertRaises(TypeError):
            record.fields["status"] = "tampered"
        self.assertNotEqual(self.view.node(record.node_id).fields.get("status"), "tampered")
        self.assertIsInstance(self.view.list_nodes(self.pa), tuple)
        self.assertIsInstance(self.view.list_edges(self.pa), tuple)


class TestBelongsToProof(unittest.TestCase):
    def test_ids_of_the_proof_belong_to_it(self):
        self.assertTrue(belongs_to_proof("p1/c1", "p1"))
        self.assertTrue(belongs_to_proof("p1/a/b", "p1"))

    def test_other_proofs_and_lookalike_prefixes_do_not(self):
        self.assertFalse(belongs_to_proof("p10/c1", "p1"))
        self.assertFalse(belongs_to_proof("p2/c1", "p1"))
        self.assertFalse(belongs_to_proof("p1-other/c1", "p1"))

    def test_ids_without_a_slash_belong_to_no_proof(self):
        self.assertFalse(belongs_to_proof("p1", "p1"))
        self.assertFalse(belongs_to_proof("sha256:" + "a" * 64, "p1"))

    def test_an_empty_or_slashed_proof_id_matches_nothing(self):
        self.assertFalse(belongs_to_proof("/c1", ""))
        self.assertFalse(belongs_to_proof("a/b/c1", "a/b"))


class TestJournalOverlayEnumeration(unittest.TestCase):
    def setUp(self):
        self.base = MemoryView()
        populate_graph(self.base, "pa", "pb")
        self.ops = (
            UpsertNode("Claim", "pa/c3", {"status": "provisional"}),
            UpsertNode("Claim", "pb/c9", {"status": "refuted"}),
            SetField("Claim", "pa/c1", "status", "critic-accepted"),
            AddEdge("DEPENDS_ON", "pa/c3", "pa/c1", "pa/e4"),
            AddEdge("DEPENDS_ON", "pb/c9", "pb/c1", "pb/e9"),
            RemoveEdge("DEPENDS_ON", "pa/e2"),
        )
        self.overlay = JournalOverlayView(self.base, 7, self.ops)

    def test_satisfies_the_read_view_protocol(self):
        self.assertIsInstance(self.overlay, ReadView)

    def test_pending_upserts_and_field_changes_are_listed(self):
        rows = node_rows(self.overlay.list_nodes("pa"))
        self.assertEqual([r[0] for r in rows], ["pa/c1", "pa/c2", "pa/c3", "pa/fs1"])
        self.assertEqual(rows[0][2]["status"], "critic-accepted")

    def test_pending_edge_changes_are_listed(self):
        ids = [r.edge_id for r in self.overlay.list_edges("pa")]
        self.assertEqual(ids, ["pa/e1", "pa/e3", "pa/e4"])

    def test_label_and_relationship_filters_apply_after_the_overlay(self):
        self.assertEqual(
            [r.node_id for r in self.overlay.list_nodes("pa", "Claim")],
            ["pa/c1", "pa/c2", "pa/c3"],
        )
        self.assertEqual(
            [r.edge_id for r in self.overlay.list_edges("pa", "DEPENDS_ON")],
            ["pa/e1", "pa/e4"],
        )

    def test_pending_ops_of_another_proof_do_not_leak(self):
        self.assertEqual([r.node_id for r in self.overlay.list_nodes("pb")], ["pb/c1", "pb/c9"])
        self.assertEqual([r.edge_id for r in self.overlay.list_edges("pb")], ["pb/e1", "pb/e9"])
        self.assertNotIn("pb/c9", [r.node_id for r in self.overlay.list_nodes("pa")])

    def test_listing_agrees_with_point_reads(self):
        for record in self.overlay.list_nodes("pa"):
            self.assertEqual(node_rows([record]), node_rows([self.overlay.node(record.node_id)]))
        for record in self.overlay.list_edges("pa"):
            self.assertEqual(edge_rows([record]), edge_rows([self.overlay.edge(record.edge_id)]))

    def test_the_base_view_is_left_untouched(self):
        self.overlay.list_nodes("pa")
        self.assertEqual(
            [r.node_id for r in self.base.list_nodes("pa")], ["pa/c1", "pa/c2", "pa/fs1"]
        )


if __name__ == "__main__":
    unittest.main()