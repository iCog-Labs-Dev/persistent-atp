"""Bench workload contracts: vocab literals, and bulk/live parity per shape.

The bulk generators must emit exactly what the live projection path writes,
or bench numbers describe atoms no real commit produces. These tests pin that
for the frontier shape; the formal shape predates the layer-emitting view and
is covered by the experiments' own counts.
"""

import unittest

from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, SetField, UpsertNode
from mork.backend.ffi import MorkSpace, MorkUnavailable
from mork.backend.view import MorkView, decode, tokens
from mork.bench.workload import frontier_atoms, proof_atoms, seeded, load
from shared.vocab import StateKind

try:
    _SPACE = MorkSpace()
except MorkUnavailable as exc:
    _SPACE = None
    _WHY = str(exc)

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")

_N = 25


class TestVocabularyLiterals(unittest.TestCase):
    """Generated field values are shared.vocab literals, not ad-hoc strings."""

    def test_proof_shape_uses_vocab_statuses_and_kinds(self):
        atoms = list(proof_atoms("p", 8, seeded()))
        statuses = {
            decode(tokens(atom)[4])
            for atom in atoms
            if len(tokens(atom)) >= 5 and decode(tokens(atom)[3]) == "status"
        }
        self.assertEqual(statuses, {"open"})
        kinds = {
            decode(tokens(atom)[4])
            for atom in atoms
            if len(tokens(atom)) >= 5 and decode(tokens(atom)[3]) == "kind"
        }
        self.assertTrue(kinds)
        self.assertTrue(kinds <= {member.value for member in StateKind})


@needs_mork
class TestFrontierWorkload(unittest.TestCase):
    """The frontier.metta shape: bulk load is indistinguishable from live."""

    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"wf-{self._testMethodName}"

    def _ops_from(self, source_proof, target_proof):
        """Replay generated atoms as live ops, remapped to another proof."""
        labels: dict[str, str] = {}
        ops = []
        for atom in frontier_atoms(source_proof, _N, seeded()):
            parts = tokens(atom)
            head = parts[0].lstrip("(")
            slots = [decode(part) for part in parts[1:]]
            if head == "node":
                _, local, label = slots
                labels[local] = label
                ops.append(UpsertNode(label, f"{target_proof}/{local}", {}))
            elif head == "field":
                _, local, name, value = slots
                ops.append(
                    SetField(
                        labels[local], f"{target_proof}/{local}", name, value
                    )
                )
            elif head == "edge":
                _, edge, rel, src, dst = slots
                ops.append(
                    AddEdge(
                        rel,
                        f"{target_proof}/{src}",
                        f"{target_proof}/{dst}",
                        f"{target_proof}/{edge}",
                    )
                )
        return ops

    def test_bulk_matches_live_projection_for_the_same_graph(self):
        bulk, live = f"{self.proof}-bulk", f"{self.proof}-live"
        load(_SPACE, frontier_atoms(bulk, _N, seeded()))
        apply_ops(self.view, self._ops_from(bulk, live))

        shapes = [
            '(node "{p}" $id $label)',
            '(field "{p}" $id $name $value)',
            '(edge "{p}" $eid $rel $src $dst)',
            '(rev-edge "{p}" $dst $rel $src $eid)',
            '(out-edge "{p}" $src $rel $dst $eid)',
            '(efield "{p}" $eid $name $value)',
            '(layer "{p}" $entity $kind)',
        ]
        for shape in shapes:
            bulk_atoms = _SPACE.match(shape.format(p=bulk), shape.format(p=bulk))
            live_atoms = _SPACE.match(shape.format(p=live), shape.format(p=live))
            self.assertEqual(bulk_atoms, live_atoms, shape)

    def test_frontier_shape_answers_frontier_queries(self):
        load(_SPACE, frontier_atoms(self.proof, _N, seeded()))

        # The bindings frontier-for-state builds its candidates from.
        moves = {
            decode(row)
            for row in _SPACE.match(
                f'(node "{self.proof}" $mid "Move")', "$mid"
            )
        }
        self.assertEqual(moves, {f"m{i}" for i in range(1, _N)})
        proposed = {
            decode(row)
            for row in _SPACE.match(
                f'(edge "{self.proof}" $eid "PROPOSES" "s1" $mid)', "$mid"
            )
        }
        self.assertEqual(proposed, {"m1"})
        # Move m1 is open, so it is eligible; s1 is not blocked.
        self.assertTrue(
            _SPACE.match(
                f'(field "{self.proof}" "m1" "status" "open")',
                "(field)",
            )
        )
        # Every move after the first requires one earlier state (the AND side).
        required = _SPACE.match(
            f'(edge "{self.proof}" $eid "REQUIRES" $mid $sg)', "($mid $sg)"
        )
        self.assertEqual(len(required), _N - 1)


if __name__ == "__main__":
    unittest.main()
