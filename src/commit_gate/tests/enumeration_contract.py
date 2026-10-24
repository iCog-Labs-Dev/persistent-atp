"""Behaviour every ``ReadView`` must share for proof-scoped enumeration.

``EnumerationContract`` is a mixin, not a test case, so this module is not
collected on its own. Mix it into a ``unittest.TestCase`` and implement
``make_view()`` to return an empty, writable view of the backend under test.
``MemoryView`` and the live ``MorkView`` run the same tests, which is what
makes "equivalent records from both backends" a checked claim.

Every test builds its graph under fresh proof ids. MORK holds one space per
process with no way to clear it, so isolation has to come from the ids.
"""

from __future__ import annotations

import uuid

SCALARS = {"text": "open", "count": 0, "ratio": 3.5, "flag": True, "nothing": None}
NESTED = {"steps": [1, "two", {"three": None}], "meta": {"ok": False}}


def populate_graph(view, proof_a: str, proof_b: str) -> None:
    """The same small two-proof graph, written in a deliberately unsorted order."""
    view.add_node(f"{proof_a}/c2", "Claim", {"status": "provisional", **SCALARS})
    view.add_node(f"{proof_a}/c1", "Claim", {"status": "conjectural"})
    view.add_node(f"{proof_a}/fs1", "FormalState", {"goal_text": "x = x"})
    view.add_node(f"{proof_b}/c1", "Claim", {"status": "refuted"})
    view.add_edge("DEPENDS_ON", f"{proof_a}/c2", f"{proof_a}/c1", f"{proof_a}/e2", {"weight": 3})
    view.add_edge("DEPENDS_ON", f"{proof_a}/c1", f"{proof_a}/c2", f"{proof_a}/e1")
    view.add_edge("HAS_TARGET", f"{proof_a}/fs1", f"{proof_a}/c1", f"{proof_a}/e3")
    view.add_edge("DEPENDS_ON", f"{proof_b}/c1", f"{proof_b}/c1", f"{proof_b}/e1")


def node_rows(records):
    return [(r.node_id, r.label, dict(r.fields)) for r in records]


def edge_rows(records):
    return [(r.edge_id, r.rel_type, r.src_id, r.dst_id, dict(r.fields)) for r in records]


class EnumerationContract:
    def make_view(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        tag = uuid.uuid4().hex[:8]
        self.pa, self.pb = f"pa{tag}", f"pb{tag}"
        self.view = self.make_view()
        populate_graph(self.view, self.pa, self.pb)

    # -- nodes -----------------------------------------------------------

    def test_nodes_come_back_in_id_order(self):
        ids = [r.node_id for r in self.view.list_nodes(self.pa)]
        self.assertEqual(ids, [f"{self.pa}/c1", f"{self.pa}/c2", f"{self.pa}/fs1"])

    def test_nodes_can_be_filtered_by_label(self):
        ids = [r.node_id for r in self.view.list_nodes(self.pa, "Claim")]
        self.assertEqual(ids, [f"{self.pa}/c1", f"{self.pa}/c2"])
        self.assertEqual(self.view.list_nodes(self.pa, "NoSuchLabel"), ())

    def test_one_proof_never_lists_anothers_nodes(self):
        for proof, other in ((self.pa, self.pb), (self.pb, self.pa)):
            with self.subTest(proof=proof):
                for record in self.view.list_nodes(proof):
                    self.assertTrue(record.node_id.startswith(f"{proof}/"))
                    self.assertFalse(record.node_id.startswith(f"{other}/"))
        self.assertEqual([r.node_id for r in self.view.list_nodes(self.pb)], [f"{self.pb}/c1"])

    def test_unknown_proof_lists_nothing(self):
        self.assertEqual(self.view.list_nodes(f"nope-{self.pa}"), ())
        self.assertEqual(self.view.list_edges(f"nope-{self.pa}"), ())

    def test_scalar_values_keep_their_type(self):
        record = next(r for r in self.view.list_nodes(self.pa) if r.node_id.endswith("/c2"))
        for key, expected in SCALARS.items():
            with self.subTest(field=key):
                self.assertEqual(record.fields[key], expected)
                self.assertIs(type(record.fields[key]), type(expected))

    def test_nested_values_keep_their_shape(self):
        self.view.add_node(f"{self.pa}/nested", "Claim", dict(NESTED))
        record = next(r for r in self.view.list_nodes(self.pa) if r.node_id.endswith("/nested"))
        self.assertEqual(dict(record.fields), NESTED)

    def test_content_addressed_ids_are_not_listed_under_a_proof(self):
        self.view.add_node("sha256:" + "a" * 64, "Artifact", {"kind": "trace"})
        for proof in (self.pa, self.pb):
            self.assertFalse(
                any(r.node_id.startswith("sha256:") for r in self.view.list_nodes(proof))
            )

    def test_listed_nodes_equal_point_reads(self):
        for record in self.view.list_nodes(self.pa):
            self.assertEqual(node_rows([record]), node_rows([self.view.node(record.node_id)]))

    # -- edges -----------------------------------------------------------

    def test_edges_come_back_in_id_order(self):
        ids = [r.edge_id for r in self.view.list_edges(self.pa)]
        self.assertEqual(ids, [f"{self.pa}/e1", f"{self.pa}/e2", f"{self.pa}/e3"])

    def test_edges_can_be_filtered_by_relationship(self):
        ids = [r.edge_id for r in self.view.list_edges(self.pa, "DEPENDS_ON")]
        self.assertEqual(ids, [f"{self.pa}/e1", f"{self.pa}/e2"])
        self.assertEqual(self.view.list_edges(self.pa, "NO_SUCH_REL"), ())

    def test_one_proof_never_lists_anothers_edges(self):
        for proof, other in ((self.pa, self.pb), (self.pb, self.pa)):
            with self.subTest(proof=proof):
                for record in self.view.list_edges(proof):
                    self.assertTrue(record.edge_id.startswith(f"{proof}/"))
                    self.assertFalse(record.edge_id.startswith(f"{other}/"))
        self.assertEqual([r.edge_id for r in self.view.list_edges(self.pb)], [f"{self.pb}/e1"])

    def test_edge_records_keep_endpoints_and_fields(self):
        record = next(r for r in self.view.list_edges(self.pa) if r.edge_id.endswith("/e2"))
        self.assertEqual(
            edge_rows([record]),
            [(f"{self.pa}/e2", "DEPENDS_ON", f"{self.pa}/c2", f"{self.pa}/c1", {"weight": 3})],
        )

    def test_listed_edges_equal_point_reads(self):
        for record in self.view.list_edges(self.pa):
            self.assertEqual(edge_rows([record]), edge_rows([self.view.edge(record.edge_id)]))

    def test_a_removed_edge_is_no_longer_listed(self):
        self.view.remove_edge(f"{self.pa}/e2")
        ids = [r.edge_id for r in self.view.list_edges(self.pa)]
        self.assertEqual(ids, [f"{self.pa}/e1", f"{self.pa}/e3"])