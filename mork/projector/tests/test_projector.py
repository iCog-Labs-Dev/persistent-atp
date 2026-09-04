"""
test_projector.py — Tests for the MORK projector (journal -> <proof_id>.metta)
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from projector import project_event_journal, project_journal_to_file
from projector.writer import DEFAULT_OUTPUT_DIR

JOURNAL_PATH = (
    Path(__file__).resolve().parents[2] / "event_journals" / "event_journal.json"


)


def make_journal(*events):
    """Build a journal dict from (revision, ops) pairs."""
    return {
        "proof_id": "test",
        "events": [
            {"revision": revision, "payload": {"ops": ops}}
            for revision, ops in events
        ],
    }


class TestProjectorCore(unittest.TestCase):
    """Most significant projection behaviors."""

    def test_move_and_attempt_infer_state_id_from_edges(self):
        journal = make_journal(
            (
                1,
                [
                    {
                        "op": "upsert_node",
                        "label": "Move",
                        "id": "test/m1",
                        "fields": {"summary": "s", "status": "open", "kind": "reduction"},
                    },
                    {
                        "op": "add_edge",
                        "rel": "PROPOSES",
                        "src": "test/s1",
                        "dst": "test/m1",
                        "edge_id": "test/e1",
                    },
                    {
                        "op": "upsert_node",
                        "label": "Attempt",
                        "id": "test/a1",
                        "fields": {"move_summary": "a", "status": "supported", "worker": "w"},
                    },
                    {
                        "op": "add_edge",
                        "rel": "ON_STATE",
                        "src": "test/a1",
                        "dst": "test/s1",
                        "edge_id": "test/e2",
                    },
                ],
            )
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertIn('!(add-atom &mork (node "test" "m1" "Move"))', blob)
        self.assertIn('!(add-atom &mork (field "test" "m1" "state_id" "s1"))', blob)
        self.assertIn('!(add-atom &mork (node "test" "a1" "Attempt"))', blob)
        self.assertIn('!(add-atom &mork (field "test" "a1" "state_id" "s1"))', blob)

    def test_events_processed_in_revision_order(self):
        journal = make_journal(
            (3, [{"op": "upsert_node", "label": "Claim", "id": "test/c2", "fields": {"statement": "Second", "status": "conjectural"}}]),
            (1, [{"op": "upsert_node", "label": "State", "id": "test/s1", "fields": {"description": "First", "status": "open", "kind": "or"}}]),
            (2, [{"op": "upsert_node", "label": "Claim", "id": "test/c1", "fields": {"statement": "Middle", "status": "conjectural"}}]),
        )
        commands = project_event_journal(journal)
        self.assertLess(commands.index(next(c for c in commands if '(node "test" "s1"' in c)),
                        commands.index(next(c for c in commands if '"Middle"' in c)))
        self.assertLess(commands.index(next(c for c in commands if '"Middle"' in c)),
                        commands.index(next(c for c in commands if '"Second"' in c)))

    def test_set_field_overwrites_earlier_value(self):
        journal = make_journal(
            (1, [{"op": "upsert_node", "label": "Claim", "id": "test/c1", "fields": {"statement": "S", "status": "conjectural"}}]),
            (2, [{"op": "set_field", "label": "Claim", "id": "test/c1", "field": "status", "value": "proved"}]),
        )
        blob = "\n".join(project_event_journal(journal))
        self.assertIn('(field "test" "c1" "status" "proved")', blob)
        self.assertNotIn('"conjectural"', blob)

    def test_remove_edge_drops_edge_and_prevents_projection(self):
        journal = make_journal(
            (1, [
                {"op": "upsert_node", "label": "State", "id": "test/s1", "fields": {"description": "d", "status": "open"}},
                {"op": "upsert_node", "label": "Claim", "id": "test/c1", "fields": {"statement": "S", "status": "conjectural"}},
                {"op": "add_edge", "rel": "SUPPORTED_BY", "src": "test/s1", "dst": "test/c1", "edge_id": "test/e1"},
                {"op": "add_edge", "rel": "SUPPORTED_BY", "src": "test/s1", "dst": "test/c1", "edge_id": "test/e2"},
            ]),
            (2, [{"op": "remove_edge", "rel": "SUPPORTED_BY", "edge_id": "test/e1"}]),
        )
        blob = "\n".join(project_event_journal(journal))
        self.assertNotIn('(edge "test" "e1"', blob)
        self.assertIn('(edge "test" "e2" "SUPPORTED_BY" "s1" "c1")', blob)

    def test_add_edge_fields_projected_as_efields(self):
        journal = make_journal(
            (1, [{
                "op": "add_edge",
                "rel": "CHILD_OF",
                "src": "test/s2",
                "dst": "test/s1",
                "edge_id": "test/e1",
                "fields": {"child_index": 0},
            }]),
        )
        blob = "\n".join(project_event_journal(journal))
        self.assertIn('(edge "test" "e1" "CHILD_OF" "s2" "s1")', blob)
        self.assertIn('(efield "test" "e1" "child_index" 0)', blob)

    def test_unknown_op_is_ignored_not_fatal(self):
        journal = make_journal(
            (1, [
                {"op": "frobnicate", "id": "test/x"},
                {"op": "upsert_node", "label": "State", "id": "test/s1", "fields": {"description": "d"}},
            ]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertEqual(len(commands), 3)  # node atom + layer atom + 1 field atom
        self.assertIn('(node "test" "s1" "State")', blob)

    def test_all_node_atoms_have_uniform_arity(self):
        """Extra fields must not change atom shape (review: pattern matching)."""
        journal = make_journal(
            (1, [
                {"op": "upsert_node", "label": "State", "id": "test/s1", "fields": {"description": "d", "status": "open", "kind": "or"}},
                {"op": "upsert_node", "label": "State", "id": "test/s2", "fields": {"description": "d", "status": "open", "kind": "and", "note": "extra"}},
            ]),
        )
        commands = project_event_journal(journal)
        node_cmds = [c for c in commands if "(node " in c]
        self.assertEqual(
            sorted(node_cmds),
            [
                '!(add-atom &mork (node "test" "s1" "State"))',
                '!(add-atom &mork (node "test" "s2" "State"))',
            ],
        )
        # The extra field lives in its own atom instead of widening the node
        self.assertIn('(field "test" "s2" "note" "extra")', "\n".join(commands))

    def test_reverse_edge_emitted_for_every_forward_edge(self):
        """§4.3 / A.7: forward and reverse edge atoms are intentionally
        duplicated as generated indexes."""
        journal = make_journal(
            (1, [
                {"op": "add_edge", "rel": "PROPOSES", "src": "test/s1",
                 "dst": "test/m1", "edge_id": "test/e1"},
            ]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertIn(
            '(edge "test" "e1" "PROPOSES" "s1" "m1")', blob
        )
        self.assertIn(
            '(rev-edge "test" "m1" "PROPOSES" "s1" "e1")', blob
        )

    def test_reverse_edge_removed_when_forward_retracted(self):
        """A remove_edge must suppress both forward and reverse atoms."""
        journal = make_journal(
            (1, [
                {"op": "add_edge", "rel": "DEPENDS_ON", "src": "test/c2",
                 "dst": "test/c1", "edge_id": "test/e1"},
            ]),
            (2, [{"op": "remove_edge", "edge_id": "test/e1"}]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertNotIn('(edge "test" "e1"', blob)
        self.assertNotIn('(rev-edge "test" "c1" "DEPENDS_ON" "c2" "e1")', blob)

    def test_layer_atom_emitted_for_every_node(self):
        """§4.2 / §8.2: every projected node carries a layer atom."""
        journal = make_journal(
            (1, [
                {"op": "upsert_node", "label": "Claim", "id": "test/c1",
                 "fields": {"statement": "S", "status": "conjectural"}},
            ]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertIn(
            '(layer "test" "c1" "committed")', blob
        )

    def test_layer_atom_emitted_for_every_edge(self):
        """§4.2 / §8.2: edges also carry independent layer atoms."""
        journal = make_journal(
            (1, [
                {"op": "add_edge", "rel": "SUPPORTED_BY", "src": "test/s1",
                 "dst": "test/c1", "edge_id": "test/e1"},
            ]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertIn(
            '(layer "test" "edge:e1" "committed")', blob
        )

    def test_move_inferred_state_id_preserved_with_reverse_edge(self):
        """state_id inference still works when reverse edges are present."""
        journal = make_journal(
            (1, [
                {"op": "upsert_node", "label": "Move", "id": "test/m1",
                 "fields": {"summary": "s", "status": "open"}},
                {"op": "add_edge", "rel": "PROPOSES", "src": "test/s1",
                 "dst": "test/m1", "edge_id": "test/e1"},
            ]),
        )
        commands = project_event_journal(journal)
        blob = "\n".join(commands)
        self.assertIn(
            '(field "test" "m1" "state_id" "s1")', blob
        )
        self.assertIn(
            '(rev-edge "test" "m1" "PROPOSES" "s1" "e1")', blob
        )


class TestProjectJournalToFile(unittest.TestCase):
    """End-to-end: real journal -> .metta file named after the proof."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name)

    def test_even_sum_journal_writes_named_metta_file(self):
        out_path = project_journal_to_file(JOURNAL_PATH, self.out_dir)

        self.assertEqual(out_path.name, "even-sum-proof.metta")
        content = out_path.read_text(encoding="utf-8")

        add_atoms = [l for l in content.splitlines() if l.startswith("!(add-atom")]
        # 4 nodes + 4 layer atoms (nodes) + 13 field atoms (incl. inferred state_id)
        # + 4 edges + 4 reverse edges + 4 edge layer atoms + 4 efield atoms = 33
        self.assertEqual(len(add_atoms), 33)

        self.assertIn("!(mm2-exec &mork 1)", content)
        self.assertIn('(node "even-sum-proof" "s1" "State")', content)
        self.assertIn('(edge "even-sum-proof" "e2" "PROPOSES" "s1" "m1")', content)
        # Reverse edge emitted for every forward edge (§4.3 / A.7)
        self.assertIn(
            '(rev-edge "even-sum-proof" "m1" "PROPOSES" "s1" "e2")', content
        )
        # Layer scoping (§4.2 / §8.2)
        self.assertIn('(layer "even-sum-proof" "s1" "committed")', content)
        self.assertIn('(layer "even-sum-proof" "edge:e2" "committed")', content)

    def test_default_output_dir_is_mork_proofs(self):
        self.assertEqual(DEFAULT_OUTPUT_DIR.name, "proofs")
        with patch("projector.writer.DEFAULT_OUTPUT_DIR", self.out_dir):
            out_path = project_journal_to_file(JOURNAL_PATH)
        self.assertEqual(out_path.parent, self.out_dir)


if __name__ == "__main__":
    unittest.main()
