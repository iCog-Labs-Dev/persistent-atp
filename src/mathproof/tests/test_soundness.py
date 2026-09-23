"""A3/A4: the soundness validator accepts the clean trace corpus.

The four hand-authored traces cover the documented shapes: a solved leaf, a
dead edge, a multi-child tactic application, and a stagnation-to-obstruction
path. Each must be schema-valid *and* structurally clean. Broken variants are
the C-branch's job; here we pin the happy path.
"""

import json
import unittest
from pathlib import Path

from mathproof.formal_atp import build_result
from mathproof.schemas import validate
from mathproof.soundness import (
    SoundnessReason,
    SoundnessViolation,
    validate_formal_search_result,
    violation_counts,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TRACES_DIR = REPO_ROOT / "fixtures" / "formal_traces"

CLEAN_TRACES = [
    "solved_leaf",
    "dead_edge",
    "multi_child",
    "stagnation_obstruction",
]


def load_trace(stem: str) -> dict:
    return json.loads((TRACES_DIR / f"{stem}.json").read_text(encoding="utf-8"))


class TestTraceCorpus(unittest.TestCase):
    def test_corpus_covers_the_documented_shapes(self):
        self.assertEqual(
            {p.stem for p in TRACES_DIR.glob("*.json")}, set(CLEAN_TRACES)
        )

    def test_every_trace_is_schema_valid(self):
        for stem in CLEAN_TRACES:
            with self.subTest(trace=stem):
                errors = validate("formal-search-result", load_trace(stem))
                self.assertEqual(errors, [], f"{stem}: {errors}")

    def test_every_trace_is_structurally_clean(self):
        for stem in CLEAN_TRACES:
            with self.subTest(trace=stem):
                violations = validate_formal_search_result(load_trace(stem))
                self.assertEqual(
                    violations, (), f"{stem}: {violations}"
                )

    def test_corpus_exercises_distinct_dispositions(self):
        dispositions = {load_trace(stem)["disposition"] for stem in CLEAN_TRACES}
        self.assertEqual(
            dispositions,
            {"proved-pending-replay", "budget-exhausted", "stagnated"},
        )

    def test_multi_child_trace_has_a_complete_three_child_list(self):
        trace = load_trace("multi_child")
        edge = trace["tactic_edges"][0]
        self.assertEqual(edge["subgoal_count"], 3)
        self.assertEqual(len(edge["produced_goal_ids"]), 3)


class TestValidatorBasics(unittest.TestCase):
    """Smoke checks: the validator rejects what the fake can synthesize badly."""

    REQUEST = {
        "run_id": "p1/fr-1",
        "proof_id": "p1",
        "environment_hash": "sha256:" + "c3" * 32,
        "goal_text": "g",
    }

    def test_synthesized_results_are_clean(self):
        from commit_gate.vocab import RunDisposition

        for disposition in (
            RunDisposition.PROVED_PENDING_REPLAY.value,
            RunDisposition.BUDGET_EXHAUSTED.value,
            RunDisposition.STAGNATED.value,
        ):
            with self.subTest(disposition=disposition):
                result = build_result(disposition, self.REQUEST)
                self.assertEqual(validate_formal_search_result(result), ())

    def test_missing_environment_hash_is_reported_once_per_defect_site(self):
        result = build_result("proved-pending-replay", self.REQUEST)
        del result["environment_hash"]
        result["obstructions"] = [{}]
        violations = validate_formal_search_result(result)
        missing = [
            v for v in violations
            if v.reason == SoundnessReason.MISSING_ENVIRONMENT_HASH
        ]
        self.assertEqual(len(missing), 2)  # the run itself + the obstruction

    def test_violation_counts_feed_metrics_directly(self):
        counts = violation_counts(
            [
                SoundnessViolation(SoundnessReason.OMITTED_SUBGOAL, "x"),
                SoundnessViolation(SoundnessReason.OMITTED_SUBGOAL, "y"),
                SoundnessViolation(SoundnessReason.SCORE_AS_TRUTH, "z"),
            ]
        )
        self.assertEqual(counts["omitted-subgoal"], 2)
        self.assertEqual(counts["score-as-truth"], 1)


if __name__ == "__main__":
    unittest.main()
