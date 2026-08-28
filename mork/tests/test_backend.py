"""Tests for the MORK-backed graph view.

The parsing and encoding helpers are tested directly. The rest needs a real
MORK library and is skipped without one; point `MORK_LIBRARY` at libmork_ffi.so
and `LD_PRELOAD` at the same path to run it.

MORK holds one space per process with no way to clear it, so each test writes
under its own proof id. Every atom carries its proof and every query is scoped
to one, which is what keeps these tests from seeing each other's writes.
"""

import unittest

from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.state import GraphView, MemoryView, ReadView, WriteView
from mork.backend.ffi import MorkSpace, MorkUnavailable
from mork.backend.view import MorkView, decode, encode, tokens
from mork.projector.core import extract_local_id, extract_proof_id

try:
    _SPACE = MorkSpace()
except MorkUnavailable as exc:
    _SPACE = None
    _WHY = str(exc)

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")


class TestExtractId(unittest.TestCase):
    """The id-splitting functions from the projector, used by the backend."""

    def test_splits_on_the_first_slash(self):
        self.assertEqual(extract_proof_id("p17/fs1"), "p17")
        self.assertEqual(extract_local_id("p17/fs1"), "fs1")

    def test_keeps_later_slashes_in_the_local_part(self):
        self.assertEqual(extract_proof_id("p17/a/b"), "p17")
        self.assertEqual(extract_local_id("p17/a/b"), "a/b")

    def test_id_without_a_slash_is_its_own_proof(self):
        self.assertEqual(extract_proof_id("s1"), "s1")
        self.assertEqual(extract_local_id("s1"), "s1")


class TestCodec(unittest.TestCase):
    def test_round_trips_values_as_their_own_type(self):
        for value in ["open", "0", 0, "true", True, None, 3.5, -1, ""]:
            with self.subTest(value=value):
                back = decode(encode(value))
                self.assertEqual(back, value)
                self.assertIs(type(back), type(value))

    def test_round_trips_text_that_would_break_an_s_expression(self):
        for value in ['quo"te', "back\\slash", "(paren)", "a\nb", "λ", "a b"]:
            with self.subTest(value=value):
                self.assertEqual(decode(encode(value)), value)

    def test_unparseable_token_falls_back_to_itself(self):
        self.assertEqual(decode("bare-symbol"), "bare-symbol")


class TestTokens(unittest.TestCase):
    def test_splits_top_level_slots(self):
        self.assertEqual(
            tokens('(field "p" "s1" "status" "open")'),
            ['field', '"p"', '"s1"', '"status"', '"open"'],
        )

    def test_space_inside_a_string_does_not_split(self):
        self.assertEqual(tokens('("a b" "c")'), ['"a b"', '"c"'])

    def test_paren_inside_a_string_does_not_open_a_group(self):
        self.assertEqual(tokens('("(" ")")'), ['"("', '")"'])

    def test_escaped_quote_does_not_end_a_string(self):
        self.assertEqual(tokens(r'("a\"b" "c")'), [r'"a\"b"', '"c"'])

    def test_nested_group_stays_one_token(self):
        self.assertEqual(tokens('(a (b c) d)'), ['a', '(b c)', 'd'])


@needs_mork
class TestMorkViewProtocols(unittest.TestCase):
    def test_satisfies_the_graph_contracts(self):
        view = MorkView(_SPACE)
        self.assertIsInstance(view, ReadView)
        self.assertIsInstance(view, WriteView)
        self.assertIsInstance(view, GraphView)


@needs_mork
class TestMorkViewNodes(unittest.TestCase):
    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"t-{self._testMethodName}"

    def id_for(self, local):
        return f"{self.proof}/{local}"

    def test_unwritten_node_reads_as_none(self):
        self.assertIsNone(self.view.node(self.id_for("nobody")))

    def test_add_node_then_read_it_back(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"status": "open"})
        node = self.view.node(self.id_for("s1"))
        self.assertEqual(node.node_id, self.id_for("s1"))
        self.assertEqual(node.label, "FormalState")
        self.assertEqual(node.fields, {"status": "open"})

    def test_node_without_fields_reads_back_empty(self):
        self.view.add_node(self.id_for("s1"), "FormalState")
        self.assertEqual(self.view.node(self.id_for("s1")).fields, {})

    def test_field_keeps_its_type(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"depth": 0, "done": False})
        fields = self.view.node(self.id_for("s1")).fields
        self.assertIs(fields["depth"], 0)
        self.assertIs(fields["done"], False)

    def test_set_field_replaces_rather_than_accumulates(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"status": "open"})
        self.view.set_field(self.id_for("s1"), "status", "closed")
        self.assertEqual(self.view.node(self.id_for("s1")).fields["status"], "closed")
        self.assertEqual(len(self.committed_statuses()), 1)

    def test_set_field_twice_with_the_same_value_leaves_one(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"status": "open"})
        self.view.set_field(self.id_for("s1"), "status", "closed")
        self.view.set_field(self.id_for("s1"), "status", "closed")
        self.assertEqual(len(self.committed_statuses()), 1)

    def test_set_field_adds_a_name_the_node_did_not_have(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"status": "open"})
        self.view.set_field(self.id_for("s1"), "note", "added later")
        self.assertEqual(
            self.view.node(self.id_for("s1")).fields,
            {"status": "open", "note": "added later"},
        )

    def test_add_node_over_an_existing_one_replaces_its_fields(self):
        self.view.add_node(self.id_for("s1"), "FormalState", {"status": "open", "old": 1})
        self.view.add_node(self.id_for("s1"), "Renamed", {"status": "closed"})
        node = self.view.node(self.id_for("s1"))
        self.assertEqual(node.label, "Renamed")
        self.assertEqual(node.fields, {"status": "closed"})

    def committed_statuses(self):
        """Every atom holding a status for s1, however many there are."""
        pattern = f'(field "{self.proof}" "s1" "status" $v)'
        return _SPACE.match(pattern, pattern)


@needs_mork
class TestMorkViewEdges(unittest.TestCase):
    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"t-{self._testMethodName}"
        self.view.add_node(self.id_for("s1"), "FormalState")
        self.view.add_node(self.id_for("m1"), "TacticApplication")

    def id_for(self, local):
        return f"{self.proof}/{local}"

    def test_unwritten_edge_reads_as_none(self):
        self.assertIsNone(self.view.edge(self.id_for("nobody")))

    def test_add_edge_then_read_it_back_with_full_ids(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1"),
            {"weight": 0.8},
        )
        edge = self.view.edge(self.id_for("e1"))
        self.assertEqual(edge.edge_id, self.id_for("e1"))
        self.assertEqual(edge.rel_type, "PROPOSES")
        self.assertEqual(edge.src_id, self.id_for("s1"))
        self.assertEqual(edge.dst_id, self.id_for("m1"))
        self.assertEqual(edge.fields, {"weight": 0.8})

    def test_edges_from_and_to(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")
        )
        self.assertEqual(
            [e.edge_id for e in self.view.edges_from(self.id_for("s1"), "PROPOSES")],
            [self.id_for("e1")],
        )
        self.assertEqual(
            [e.edge_id for e in self.view.edges_to(self.id_for("m1"), "PROPOSES")],
            [self.id_for("e1")],
        )

    def test_edges_of_another_type_are_not_returned(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")
        )
        self.assertEqual(self.view.edges_from(self.id_for("s1"), "ON_STATE"), ())

    def test_edges_from_the_wrong_end_are_not_returned(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")
        )
        self.assertEqual(self.view.edges_from(self.id_for("m1"), "PROPOSES"), ())

    def test_several_edges_from_one_node(self):
        self.view.add_node(self.id_for("m2"), "TacticApplication")
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")
        )
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m2"), self.id_for("e2")
        )
        self.assertEqual(
            sorted(e.edge_id for e in self.view.edges_from(self.id_for("s1"), "PROPOSES")),
            [self.id_for("e1"), self.id_for("e2")],
        )

    def test_remove_edge_takes_its_fields_with_it(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1"),
            {"weight": 0.8},
        )
        self.view.remove_edge(self.id_for("e1"))
        self.assertIsNone(self.view.edge(self.id_for("e1")))
        self.assertEqual(self.view.edges_from(self.id_for("s1"), "PROPOSES"), ())
        pattern = f'(efield "{self.proof}" "e1" $name $value)'
        self.assertEqual(_SPACE.match(pattern, pattern), [])

    def test_remove_edge_that_was_never_added_is_a_no_op(self):
        self.view.remove_edge(self.id_for("nobody"))
        self.assertIsNone(self.view.edge(self.id_for("nobody")))

    def test_remove_edge_twice_is_a_no_op(self):
        self.view.add_edge(
            "PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")
        )
        self.view.remove_edge(self.id_for("e1"))
        self.view.remove_edge(self.id_for("e1"))
        self.assertIsNone(self.view.edge(self.id_for("e1")))


@needs_mork
class TestProjection(unittest.TestCase):
    """`apply_ops` over MORK: the projection the commit gate performs."""

    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"t-{self._testMethodName}"

    def id_for(self, local):
        return f"{self.proof}/{local}"

    def journal(self):
        return [
            UpsertNode("FormalState", self.id_for("s1"), {"status": "open"}),
            UpsertNode("TacticApplication", self.id_for("m1"), {}),
            AddEdge("PROPOSES", self.id_for("s1"), self.id_for("m1"), self.id_for("e1")),
            SetField("FormalState", self.id_for("s1"), "status", "closed", "open"),
        ]

    def committed(self):
        """Every atom under this test's proof, sorted."""
        shapes = [
            f'(node "{self.proof}" $id $label)',
            f'(field "{self.proof}" $id $name $value)',
            f'(edge "{self.proof}" $eid $rel $src $dst)',
            f'(efield "{self.proof}" $eid $name $value)',
        ]
        return sorted(atom for shape in shapes for atom in _SPACE.match(shape, shape))

    def test_projects_a_journal(self):
        apply_ops(self.view, self.journal())
        self.assertEqual(
            self.committed(),
            [
                f'(edge "{self.proof}" "e1" "PROPOSES" "s1" "m1")',
                f'(field "{self.proof}" "s1" "status" "closed")',
                f'(node "{self.proof}" "m1" "TacticApplication")',
                f'(node "{self.proof}" "s1" "FormalState")',
            ],
        )

    def test_replaying_a_journal_changes_nothing(self):
        """What makes an interrupted projection repairable: replay is a no-op."""
        apply_ops(self.view, self.journal())
        once = self.committed()
        apply_ops(self.view, self.journal())
        self.assertEqual(self.committed(), once)

    def test_replaying_from_any_point_converges(self):
        """A crash leaves a prefix applied; finishing from the start still heals."""
        ops = self.journal()
        for cut in range(len(ops) + 1):
            apply_ops(self.view, ops[:cut])
            apply_ops(self.view, ops)
            self.assertEqual(
                self.view.node(self.id_for("s1")).fields["status"], "closed"
            )

    def test_set_field_on_an_unknown_node_is_refused(self):
        with self.assertRaises(ValueError):
            apply_ops(self.view, [SetField("FormalState", self.id_for("ghost"), "s", 1)])

    def test_remove_edge_after_add_edge_leaves_no_edge(self):
        apply_ops(self.view, self.journal())
        apply_ops(self.view, [RemoveEdge("PROPOSES", self.id_for("e1"))])
        self.assertIsNone(self.view.edge(self.id_for("e1")))

    def test_agrees_with_a_memory_view_on_the_same_journal(self):
        """The two backends are interchangeable, so replay can verify MORK."""
        ops = self.journal()
        memory = MemoryView()
        apply_ops(memory, ops)
        apply_ops(self.view, ops)

        for local in ("s1", "m1"):
            expected = memory.node(self.id_for(local))
            actual = self.view.node(self.id_for(local))
            self.assertEqual(actual.label, expected.label)
            self.assertEqual(dict(actual.fields), dict(expected.fields))

        expected_edge = memory.edge(self.id_for("e1"))
        actual_edge = self.view.edge(self.id_for("e1"))
        self.assertEqual(actual_edge.rel_type, expected_edge.rel_type)
        self.assertEqual(actual_edge.src_id, expected_edge.src_id)
        self.assertEqual(actual_edge.dst_id, expected_edge.dst_id)


class MarkContract:
    """The projection mark, held to the same rules on every backend.

    Mixed into one case per backend so a new backend cannot satisfy `WriteView`
    with a mark that behaves differently from the one the gate relies on.
    """

    def test_unrecorded_proof_reads_as_zero(self):
        self.assertEqual(self.view.projected_revision(self.proof), 0)

    def test_records_and_reads_back(self):
        self.view.record_projected(self.proof, 3)
        self.assertEqual(self.view.projected_revision(self.proof), 3)

    def test_advances(self):
        self.view.record_projected(self.proof, 1)
        self.view.record_projected(self.proof, 2)
        self.assertEqual(self.view.projected_revision(self.proof), 2)

    def test_never_moves_backwards(self):
        """Replaying older events must not un-record newer ones."""
        self.view.record_projected(self.proof, 5)
        self.view.record_projected(self.proof, 2)
        self.assertEqual(self.view.projected_revision(self.proof), 5)

    def test_is_kept_per_proof(self):
        self.view.record_projected(self.proof, 3)
        self.view.record_projected(f"{self.proof}-other", 7)
        self.assertEqual(self.view.projected_revision(self.proof), 3)
        self.assertEqual(self.view.projected_revision(f"{self.proof}-other"), 7)

    def test_recording_the_same_revision_twice_is_harmless(self):
        self.view.record_projected(self.proof, 2)
        self.view.record_projected(self.proof, 2)
        self.assertEqual(self.view.projected_revision(self.proof), 2)


class TestMemoryViewMark(MarkContract, unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()
        self.proof = "p1"


@needs_mork
class TestMorkViewMark(MarkContract, unittest.TestCase):
    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"m-{self._testMethodName}"

    def test_only_one_mark_atom_is_ever_held(self):
        for revision in (1, 2, 3):
            self.view.record_projected(self.proof, revision)
        pattern = f'(projected "{self.proof}" $revision)'
        self.assertEqual(_SPACE.match(pattern, pattern), [f'(projected "{self.proof}" 3)'])

    def test_the_mark_lives_in_the_space_it_describes(self):
        """Why it is not kept in the journal database.

        The space is process memory: it is empty on restart. A mark stored
        anywhere more durable would outlive the atoms it vouches for and claim
        an empty graph was up to date.
        """
        self.view.record_projected(self.proof, 2)
        self.assertIn(f'(projected "{self.proof}" 2)', self.view.atoms(self.proof))


if __name__ == "__main__":
    unittest.main()
