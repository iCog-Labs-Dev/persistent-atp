import unittest

from context_compiler.budgeting import (
    CharHeuristicTokenCounter,
    Section,
    TokenCounter,
    fit_sections,
    render_sections,
    truncate_at_whitespace,
)
from context_compiler.contracts import TextBudget
from context_compiler.errors import ContextValidationError


class BudgetingTests(unittest.TestCase):
    def test_truncates_on_whitespace(self):
        self.assertEqual(truncate_at_whitespace("alpha beta gamma", 8), "alpha")
        self.assertEqual(truncate_at_whitespace("alpha beta", 5), "alpha")

    def test_mandatory_overflow_raises(self):
        sections = [Section("a", "A", "x" * 400, mandatory=True)]
        with self.assertRaises(ContextValidationError):
            fit_sections(sections, TextBudget(10), CharHeuristicTokenCounter())

    def test_drop_order_follows_priority_not_position(self):
        sections = [
            Section("m", "M", "must", mandatory=True),
            Section("low", "L", "y" * 200, priority=5),
            Section("high", "H", "z" * 200, priority=1),
        ]
        fit = fit_sections(sections, TextBudget(70), CharHeuristicTokenCounter())
        self.assertEqual([s.key for s in fit.kept], ["m", "high"])
        self.assertEqual([s.key for s in fit.dropped], ["low"])

    def test_truncatable_section_is_shortened(self):
        sections = [Section("t", "T", "word " * 100, truncatable=True)]
        fit = fit_sections(sections, TextBudget(30), CharHeuristicTokenCounter())
        self.assertIn("t", fit.truncated_keys)
        self.assertLessEqual(fit.token_count, 30)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ContextValidationError):
            fit_sections([Section("a", "A", "x"), Section("a", "B", "y")],
                         TextBudget(50), CharHeuristicTokenCounter())

    def test_counter_matches_obstruction_heuristic(self):
        counter = CharHeuristicTokenCounter()
        for n in (0, 1, 3, 4, 5, 400):
            self.assertEqual(counter.count("x" * n), (n + 3) // 4 if n else 0)
        self.assertIsInstance(counter, TokenCounter)

    def test_counter_rejects_bad_chars_per_token(self):
        with self.assertRaises(ContextValidationError):
            CharHeuristicTokenCounter(0)

    def test_fit_is_deterministic(self):
        sections = [
            Section("m", "M", "must", mandatory=True),
            Section("a", "A", "y" * 120, priority=2),
            Section("b", "B", "z" * 120, priority=1),
        ]
        counter = CharHeuristicTokenCounter()
        first = fit_sections(sections, TextBudget(60), counter)
        second = fit_sections(sections, TextBudget(60), counter)
        self.assertEqual(first.text, second.text)
        self.assertLessEqual(first.token_count, 60)

    def test_weighted_mmr_chooses_relevant_and_diverse_context(self):
        mandatory = Section("must", "Target", "original theorem", mandatory=True,
            similarity_text="original theorem")
        feature_a = {"failure": 1.0, "verification": 0.9, "novelty": 0.8,
            "semantic_similarity": 0.9, "ancestry": 1.0}
        first = Section("first", "Failure A", "common algebra bridge", features=feature_a,
            similarity_text="common algebra bridge")
        duplicate = Section("duplicate", "Failure A2", "common algebra bridge", features=feature_a,
            similarity_text="common algebra bridge")
        diverse = Section("diverse", "Failure B", "induction boundary", features={
            "failure": 0.8, "verification": 0.8, "novelty": 0.8,
            "semantic_similarity": 0.7, "ancestry": 0.9,
        }, similarity_text="induction boundary")
        counter = CharHeuristicTokenCounter()
        budget = counter.count(render_sections((mandatory, first, diverse)))

        fit = fit_sections((mandatory, first, duplicate, diverse), TextBudget(budget), counter)

        kept = {section.key for section in fit.kept}
        assert {"must", "first", "diverse"} <= kept
        assert "duplicate" not in kept
        assert fit.score_breakdowns["duplicate"]["redundancy"] > 0
        assert fit.score_breakdowns["diverse"]["mmr_score"] > 0
        assert fit.reasons["duplicate"] == "dropped: over text budget"


if __name__ == "__main__":
    unittest.main()
