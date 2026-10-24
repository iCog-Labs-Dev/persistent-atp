"""Proof-scoped enumeration on the live MORK view.

`MorkView` runs the same contract as `MemoryView`
(`commit_gate/tests/enumeration_contract.py`), and a parity test builds one
graph in both backends and compares what they return. Everything that needs the
native library is skipped without it; point `MORK_LIBRARY` at libmork_ffi.so
and `LD_PRELOAD` at the same path (or use `scripts/with-mork.sh`) to run it.
"""

import unittest
import uuid

from commit_gate.state import MemoryView, ReadView, belongs_to_proof
from commit_gate.tests.enumeration_contract import (
    EnumerationContract,
    edge_rows,
    node_rows,
    populate_graph,
)
from mork.backend.ffi import MorkSpace, MorkUnavailable
from mork.backend.view import MorkView
from mork.projector.core import extract_proof_id

try:
    _SPACE = MorkSpace()
except MorkUnavailable:
    _SPACE = None

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")


class TestProofScopeRuleMatchesMork(unittest.TestCase):
    """`MemoryView` and MORK must agree on which proof an id belongs to."""

    def test_the_rule_agrees_with_the_projectors_id_split(self):
        for identity in ("p1/c1", "p1/a/b", "p10/c1", "p1-other/c1", "p2/e9"):
            with self.subTest(identity=identity):
                for proof in ("p1", "p10", "p1-other", "p2"):
                    self.assertEqual(
                        belongs_to_proof(identity, proof),
                        extract_proof_id(identity) == proof,
                    )


@needs_mork
class TestMorkViewEnumeration(EnumerationContract, unittest.TestCase):
    def make_view(self):
        return MorkView(_SPACE)

    def test_satisfies_the_read_view_protocol(self):
        self.assertIsInstance(self.view, ReadView)

    def test_list_methods_return_what_the_older_names_return(self):
        self.assertEqual(self.view.list_nodes(self.pa), self.view.nodes(self.pa))
        self.assertEqual(self.view.list_nodes(self.pa, "Claim"), self.view.nodes(self.pa, "Claim"))
        self.assertEqual(self.view.list_edges(self.pa), self.view.edges(self.pa))
        self.assertEqual(
            self.view.list_edges(self.pa, "DEPENDS_ON"), self.view.edges(self.pa, "DEPENDS_ON")
        )


@needs_mork
class TestBackendsReturnEquivalentRecords(unittest.TestCase):
    def setUp(self):
        tag = uuid.uuid4().hex[:8]
        self.pa, self.pb = f"pa{tag}", f"pb{tag}"
        self.memory, self.mork = MemoryView(), MorkView(_SPACE)
        for view in (self.memory, self.mork):
            populate_graph(view, self.pa, self.pb)

    def test_nodes_match_with_and_without_a_label(self):
        for proof in (self.pa, self.pb):
            for label in (None, "Claim", "FormalState", "NoSuchLabel"):
                with self.subTest(proof=proof, label=label):
                    self.assertEqual(
                        node_rows(self.memory.list_nodes(proof, label)),
                        node_rows(self.mork.list_nodes(proof, label)),
                    )

    def test_edges_match_with_and_without_a_relationship(self):
        for proof in (self.pa, self.pb):
            for rel in (None, "DEPENDS_ON", "HAS_TARGET", "NO_SUCH_REL"):
                with self.subTest(proof=proof, rel=rel):
                    self.assertEqual(
                        edge_rows(self.memory.list_edges(proof, rel)),
                        edge_rows(self.mork.list_edges(proof, rel)),
                    )

    def test_an_unknown_proof_matches(self):
        self.assertEqual(self.memory.list_nodes("nobody"), self.mork.list_nodes("nobody"))
        self.assertEqual(self.memory.list_edges("nobody"), self.mork.list_edges("nobody"))


if __name__ == "__main__":
    unittest.main()