"""Guards against the gate and the neo4j projection drifting apart again.

These tests import both packages and assert they agree on every status
literal, label and separator convention, without needing a live database.
"""

import importlib.util
import unittest
from enum import StrEnum
from pathlib import Path

from commit_gate import vocab as gate_vocab
from commit_gate.transitions import STATUS_TRANSITIONS
from commit_gate.validate import ENUM_FIELDS
from shared import vocab as shared_vocab

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    """Load a module by path.

    The project's own ``neo4j/`` package shadows the driver distribution, so
    ``import neo4j.constants`` runs ``neo4j/__init__.py`` -> ``adapter`` ->
    ``from neo4j import GraphDatabase`` and fails. Loading the file directly
    keeps this suite driver-free.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


graph_constants = _load("graph_constants", REPO_ROOT / "neo4j_adapter" / "constants.py")


class TestSingleSourceOfTruth(unittest.TestCase):
    def test_gate_vocab_reexports_shared_definitions(self):
        for name in gate_vocab.__all__:
            self.assertIs(
                getattr(gate_vocab, name),
                getattr(shared_vocab, name),
                f"commit_gate.vocab.{name} is not the shared definition",
            )

    def test_graph_status_sets_are_derived_from_shared_enums(self):
        self.assertEqual(graph_constants.STATE_STATUSES, shared_vocab.values(shared_vocab.StateStatus))
        self.assertEqual(graph_constants.MOVE_STATUSES, shared_vocab.values(shared_vocab.MoveStatus))
        self.assertEqual(graph_constants.CLAIM_STATUSES, shared_vocab.values(shared_vocab.ClaimStatus))
        self.assertEqual(
            graph_constants.ATTEMPT_STATUSES, shared_vocab.values(shared_vocab.AttemptStatus)
        )
        self.assertEqual(graph_constants.STATE_KINDS, shared_vocab.values(shared_vocab.StateKind))

    def test_graph_literals_are_members_of_the_shared_vocabulary(self):
        literals = {
            name: value
            for name, value in vars(graph_constants).items()
            if name.startswith(("STATE_", "MOVE_", "CLAIM_", "ATTEMPT_"))
            and isinstance(value, str)
        }
        self.assertTrue(literals)
        for name, value in literals.items():
            group = name.split("_")[0]
            allowed = {
                "STATE": graph_constants.STATE_STATUSES | graph_constants.STATE_KINDS,
                "MOVE": graph_constants.MOVE_STATUSES,
                "CLAIM": graph_constants.CLAIM_STATUSES,
                "ATTEMPT": graph_constants.ATTEMPT_STATUSES,
            }[group]
            self.assertIn(value, allowed, f"{name} is not a {group.lower()} literal")


class TestSeparatorConvention(unittest.TestCase):
    def test_no_underscore_separated_literals(self):
        for name in dir(shared_vocab):
            member = getattr(shared_vocab, name)
            if isinstance(member, type) and issubclass(member, StrEnum):
                for literal in member:
                    self.assertNotIn(
                        "_", literal.value, f"{member.__name__}.{literal.name} uses an underscore"
                    )

    def test_legacy_spellings_resolve_to_canonical_literals(self):
        claim = graph_constants.CLAIM_STATUSES
        self.assertEqual(graph_constants._check("critic_accepted", claim, "claim status"), "critic-accepted")
        self.assertEqual(graph_constants._check("lean_verified", claim, "claim status"), "lean-verified")

    def test_legacy_state_closure_literal_resolves(self):
        self.assertEqual(
            graph_constants._check("closed", graph_constants.STATE_STATUSES, "state status"),
            shared_vocab.FormalStateStatus.FORMALLY_CLOSED.value,
        )

    def test_move_keeps_its_own_closed_literal(self):
        self.assertEqual(
            graph_constants._check("closed", graph_constants.MOVE_STATUSES, "move status"),
            shared_vocab.TacticStatus.CLOSED.value,
        )

    def test_unknown_literal_is_rejected(self):
        with self.assertRaises(ValueError):
            graph_constants._check("solved", graph_constants.STATE_STATUSES, "state status")


class TestLabels(unittest.TestCase):
    def test_every_graph_label_maps_to_a_gate_label(self):
        for label in graph_constants.GRAPH_LABELS:
            self.assertIn(label, shared_vocab.GRAPH_TO_GATE_LABEL, f"{label} has no gate mapping")
            self.assertIn(shared_vocab.gate_label(label), shared_vocab.GATE_LABELS)

    def test_label_mapping_is_bijective(self):
        self.assertEqual(
            len(shared_vocab.GRAPH_TO_GATE_LABEL), len(shared_vocab.GATE_TO_GRAPH_LABEL)
        )
        for graph, gate in shared_vocab.GRAPH_TO_GATE_LABEL.items():
            self.assertEqual(shared_vocab.graph_label(gate), graph)
            self.assertEqual(shared_vocab.gate_label(graph), gate)

    def test_gate_labels_cover_every_validated_label(self):
        for label, _field in ENUM_FIELDS:
            self.assertIn(label, shared_vocab.GATE_LABELS)

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(ValueError):
            shared_vocab.gate_label("Nonexistent")


class TestTransitionCoverage(unittest.TestCase):
    def test_every_unified_status_has_a_transition_entry(self):
        for (label, field), enum_class in ENUM_FIELDS.items():
            table = STATUS_TRANSITIONS.get((label, field))
            if table is None:
                continue
            for member in enum_class:
                self.assertIn(member.value, table, f"{label}.{field}: {member.value} unreachable")


if __name__ == "__main__":
    unittest.main()
