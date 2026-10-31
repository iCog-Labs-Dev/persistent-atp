"""The five-mechanism answer contract (Section 10.5)."""

import unittest

from commit_gate.state import MemoryView

from coordinator.mechanisms import MECHANISM_FIELDS, check_mechanisms, mechanism_moves

TECHNIQUES = ("induction", "generating-functions", "probabilistic", "extremal", "algebraic")


def mechanism(tag: str, **overrides) -> dict:
    answer = {field: f"{field} via {tag}" for field in MECHANISM_FIELDS}
    answer.update(
        technique_tags=[tag],
        prerequisites=[],
        promise=0.6,
        cost=0.3,
        novelty=0.5,
        risk=0.4,
        stance="proves",
    )
    answer.update(overrides)
    return answer


def answer() -> list[dict]:
    return [mechanism(tag) for tag in TECHNIQUES]


class TestContract(unittest.TestCase):
    def test_five_complete_and_diverse_mechanisms_hold(self):
        self.assertEqual(check_mechanisms(answer()), [])

    def test_the_answer_needs_exactly_five(self):
        self.assertIn("expected 5 mechanisms, got 4", check_mechanisms(answer()[:4]))

    def test_every_field_is_required(self):
        incomplete = answer()
        del incomplete[2]["falsification_test"]
        self.assertEqual(
            check_mechanisms(incomplete), ["mechanism 3 is missing falsification_test"]
        )

    def test_one_technique_twice_is_not_diverse(self):
        repeated = answer()
        repeated[4] = mechanism("induction")
        (problem,) = check_mechanisms(repeated)
        self.assertIn("repeats technique 'induction' from mechanism 1", problem)

    def test_stance_and_scores_are_closed(self):
        odd = answer()
        odd[0] = mechanism("induction", stance="hopes", risk=1.5)
        self.assertEqual(
            check_mechanisms(odd),
            ["mechanism 1 has stance 'hopes'", "mechanism 1 scores risk outside [0, 1]"],
        )

    def test_prerequisites_must_be_committed_claims(self):
        view = MemoryView()
        view.add_node("p1/c-1", "Claim", {"status": "critic-accepted"})
        view.add_node("p1/fd-1", "FormalDeclaration", {"status": "aligned"})
        cited = answer()
        cited[0] = mechanism("induction", prerequisites=["p1/c-1", "p1/fd-1", "p1/c-9"])
        self.assertEqual(
            check_mechanisms(cited, view),
            [
                "mechanism 1 depends on 'p1/fd-1', which is not a claim",
                "mechanism 1 depends on 'p1/c-9', which is not a claim",
            ],
        )

    def test_each_mechanism_becomes_a_move_starting_at_its_first_step(self):
        moves = mechanism_moves(answer())
        self.assertEqual(len(moves), 5)
        self.assertEqual(moves[0]["detail"], "first_step via induction")
        self.assertEqual(moves[0]["technique_tags"], ["induction"])


if __name__ == "__main__":
    unittest.main()
