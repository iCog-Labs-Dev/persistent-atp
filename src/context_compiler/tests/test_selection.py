"""Tests for the shared V1 selection helpers and their integration points."""

import hashlib
import unittest

from context_compiler.budgeting import (
    CharHeuristicTokenCounter,
    Section,
    fit_sections,
)
from context_compiler.contracts import (
    ContextPacket,
    PacketKind,
    SelectionDecision,
    TextBudget,
)
from context_compiler.errors import ContextValidationError
from context_compiler.finalize import finalize_packet
from context_compiler.selection import (
    REASON_OVER_FAILED_STATE_LIMIT,
    REASON_OVER_PREMISE_LIMIT,
    REASON_OVER_PRIOR_RUN_LIMIT,
    REASON_OVER_TEXT_BUDGET,
    REASON_OVER_TRANSPOSITION_LIMIT,
    apply_item_limit,
    duplicate_reason,
    trailing_ordinal,
)


class ApplyItemLimitTests(unittest.TestCase):
    def test_none_means_unlimited(self):
        sel = apply_item_limit(["a", "b", "c"], None)
        self.assertEqual((sel.kept, sel.dropped), (("a", "b", "c"), ()))

    def test_keeps_the_first_n_in_order(self):
        sel = apply_item_limit(["a", "b", "c", "d"], 2)
        self.assertEqual((sel.kept, sel.dropped), (("a", "b"), ("c", "d")))

    def test_zero_drops_everything(self):
        sel = apply_item_limit(["a", "b"], 0)
        self.assertEqual((sel.kept, sel.dropped), ((), ("a", "b")))

    def test_limit_larger_than_input(self):
        sel = apply_item_limit(["a"], 9)
        self.assertEqual((sel.kept, sel.dropped), (("a",), ()))

    def test_empty_input(self):
        sel = apply_item_limit([], 3)
        self.assertEqual((sel.kept, sel.dropped), ((), ()))

    def test_invalid_limits_rejected(self):
        for bad in (-1, True, 1.5, "2"):
            with self.assertRaises(ContextValidationError):
                apply_item_limit(["a"], bad)


class TrailingOrdinalTests(unittest.TestCase):
    def test_extracts_trailing_integer(self):
        self.assertEqual(trailing_ordinal("p1/fr-12"), 12)
        self.assertEqual(trailing_ordinal("p1/fr-2"), 2)
        self.assertEqual(trailing_ordinal("tr-007"), 7)

    def test_no_ordinal(self):
        self.assertEqual(trailing_ordinal("env"), -1)
        self.assertEqual(trailing_ordinal("p1/fr-x"), -1)

    def test_numeric_not_lexicographic(self):
        self.assertGreater(trailing_ordinal("p/fr-10"), trailing_ordinal("p/fr-9"))


class ReasonTests(unittest.TestCase):
    def test_reasons_are_distinct_and_explicit(self):
        reasons = {
            REASON_OVER_TEXT_BUDGET,
            REASON_OVER_PREMISE_LIMIT,
            REASON_OVER_PRIOR_RUN_LIMIT,
            REASON_OVER_TRANSPOSITION_LIMIT,
            REASON_OVER_FAILED_STATE_LIMIT,
        }
        self.assertEqual(len(reasons), 5)
        self.assertTrue(all(r.startswith("dropped: over ") for r in reasons))

    def test_duplicate_reason(self):
        self.assertEqual(duplicate_reason("x/1"), "duplicate of x/1")


def _render_alt(sections):
    return " | ".join(f"{s.heading}={s.body}" for s in sections)


class FitSectionsRendererTests(unittest.TestCase):
    def sections(self):
        return [
            Section("a", "A", "mandatory body", mandatory=True),
            Section("b", "B", "x" * 80, priority=2),
            Section("c", "C", "y" * 20, priority=1),
        ]

    def test_default_renderer_is_unchanged(self):
        fit = fit_sections(self.sections(), TextBudget(1000), CharHeuristicTokenCounter())
        self.assertTrue(fit.text.startswith("## A\nmandatory body"))

    def test_custom_renderer_is_used_for_text_and_for_fitting(self):
        counter = CharHeuristicTokenCounter()
        fit = fit_sections(self.sections(), TextBudget(1000), counter, render=_render_alt)
        self.assertEqual(fit.text, _render_alt(fit.kept))
        self.assertNotIn("##", fit.text)
        # The budget is measured on the custom rendering, not the default one.
        budget = counter.count(_render_alt([self.sections()[0], self.sections()[2]]))
        tight = fit_sections(self.sections(), TextBudget(budget), counter, render=_render_alt)
        self.assertEqual([s.key for s in tight.kept], ["a", "c"])
        self.assertEqual([s.key for s in tight.dropped], ["b"])
        self.assertLessEqual(counter.count(tight.text), budget)

    def test_mandatory_overflow_raises_with_custom_renderer(self):
        with self.assertRaisesRegex(ContextValidationError, "mandatory sections exceed"):
            fit_sections(self.sections(), TextBudget(1), CharHeuristicTokenCounter(),
                         render=_render_alt)

    def test_priority_then_position_decides_who_is_dropped(self):
        counter = CharHeuristicTokenCounter()
        sections = [
            Section("a", "A", "m", mandatory=True),
            Section("low", "L", "l" * 40, priority=5),
            Section("high", "H", "h" * 40, priority=1),
        ]
        one_optional = counter.count(_render_alt([sections[0], sections[2]]))
        fit = fit_sections(sections, TextBudget(one_optional), counter, render=_render_alt)
        self.assertEqual([s.key for s in fit.kept], ["a", "high"])
        self.assertEqual([s.key for s in fit.dropped], ["low"])


def _packet(**overrides):
    values = dict(task_id="t", proof_id="p", base_revision=1,
                  packet_kind=PacketKind.FORMAL, content="body")
    values.update(overrides)
    return ContextPacket(**values)


def _decision():
    return SelectionDecision("s1", "v1", "open", True, "because")


class FinalizeExtraBudgetsTests(unittest.TestCase):
    def finalize(self, **kw):
        return finalize_packet(_packet(), (_decision(),), compiler_version="c",
                               rendering_version="r", **kw)

    def test_extra_budgets_reach_the_manifest_only(self):
        plain = self.finalize()
        extra = self.finalize(extra_budgets={"context_limits": {"max_premises": 3}})
        self.assertEqual(extra.manifest["budgets"], {"context_limits": {"max_premises": 3}})
        self.assertEqual(plain.manifest["budgets"], {})
        self.assertEqual(plain.packet_digest, extra.packet_digest)  # digest = packet only

    def test_extra_budgets_merge_with_packet_budgets(self):
        packet = _packet(text_budget=TextBudget(50))
        out = finalize_packet(packet, (_decision(),), compiler_version="c",
                              rendering_version="r",
                              extra_budgets={"context_limits": {"max_premises": 1}})
        self.assertEqual(out.manifest["budgets"],
                         {"text_tokens": 50, "context_limits": {"max_premises": 1}})

    def test_none_and_empty_extra_budgets_change_nothing(self):
        self.assertEqual(self.finalize(extra_budgets=None).manifest,
                         self.finalize(extra_budgets={}).manifest)


if __name__ == "__main__":
    unittest.main()
