"""A1: every schema validates its documented example payload.

Enum literals are checked against ``shared.vocab`` so the wire format cannot
drift from the gate and graph vocabularies.
"""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from commit_gate import vocab as gate_vocab
from mathproof.schemas import SCHEMAS_DIR, load_schema, validate, validator_for

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES_DIR = REPO_ROOT / "fixtures" / "schema_examples"

NINE_SCHEMAS = [
    "proof-spec",
    "worker-result",
    "formal-search-request",
    "formal-search-result",
    "formal-state",
    "tactic-application",
    "statement-alignment",
    "obstruction",
    "score-vector",
]


class TestSchemaSet(unittest.TestCase):
    def test_all_nine_schemas_exist(self):
        for name in NINE_SCHEMAS:
            with self.subTest(name=name):
                self.assertTrue(
                    (SCHEMAS_DIR / f"{name}.schema.json").exists(), name
                )

    def test_every_schema_file_is_valid_draft_2020_12(self):
        for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
            with self.subTest(schema=path.name):
                Draft202012Validator.check_schema(json.loads(path.read_text()))


class TestExamplePayloads(unittest.TestCase):
    """Each schema validates the example payload shipped next to it."""

    def _assert_example(self, schema_name: str):
        example = EXAMPLES_DIR / f"{schema_name}.json"
        self.assertTrue(example.exists(), f"missing example {example}")
        payload = json.loads(example.read_text(encoding="utf-8"))
        errors = validate(schema_name, payload)
        self.assertEqual(errors, [], f"{schema_name}: {errors}")

    def test_proof_spec_example(self):
        self._assert_example("proof-spec")

    def test_worker_result_example(self):
        self._assert_example("worker-result")

    def test_formal_search_request_example(self):
        self._assert_example("formal-search-request")

    def test_formal_search_result_example(self):
        self._assert_example("formal-search-result")

    def test_formal_state_example(self):
        self._assert_example("formal-state")

    def test_tactic_application_example(self):
        self._assert_example("tactic-application")

    def test_statement_alignment_example(self):
        self._assert_example("statement-alignment")

    def test_obstruction_example(self):
        self._assert_example("obstruction")

    def test_score_vector_example(self):
        self._assert_example("score-vector")


class TestNoTruthFields(unittest.TestCase):
    """Acceptance criterion: no schema field implies a scalar truth value."""

    FORBIDDEN = {"truth", "is_true", "proved", "valid", "is_valid"}

    def test_score_vector_has_no_truth_field(self):
        props = set(load_schema("score-vector")["properties"])
        self.assertFalse(props & self.FORBIDDEN)
        # And nothing smuggled in under another name:
        self.assertEqual(
            props,
            set(gate_vocab.ANNOTATION_FIELDS),
            "score-vector features must mirror shared ANNOTATION_FIELDS (2.4)",
        )

    def test_embedded_score_vectors_match_the_standalone_one(self):
        standalone = set(load_schema("score-vector")["properties"])
        for name in ("formal-search-result", "tactic-application"):
            embedded = set(
                load_schema(name)["$defs"]["score_vector"]["properties"]
            )
            with self.subTest(name=name):
                self.assertEqual(embedded, standalone)
        # formal-state reuses the standalone vocabulary by reference.
        ref = load_schema("formal-state")["$defs"]["score_vector"]
        self.assertEqual(
            ref,
            {"$ref": load_schema("score-vector")["$id"]},
        )


class TestVocabularyParity(unittest.TestCase):
    """Schema enums must mirror the shared vocabulary, never fork it."""

    PARITY = {
        ("formal-search-request", None): None,  # checked via dedicated tests
    }

    def _enum_of(self, schema_name, *path):
        node = load_schema(schema_name)
        for key in path:
            node = node[key]
        return set(node["enum"])

    def test_run_dispositions_mirror_shared_vocab(self):
        expected = {m.value for m in gate_vocab.RunDisposition}
        self.assertEqual(
            self._enum_of("formal-search-result", "properties", "disposition"),
            expected,
        )

    def test_executor_results_mirror_shared_vocab(self):
        expected = {m.value for m in gate_vocab.ExecutorResult}
        self.assertEqual(
            self._enum_of(
                "tactic-application", "properties", "executor_result"
            ),
            expected,
        )
        result_defs = load_schema("formal-search-result")["$defs"]
        self.assertEqual(
            set(result_defs["tactic_edge"]["properties"]["executor_result"]["enum"]),
            expected,
        )

    def test_obstruction_kinds_mirror_shared_vocab(self):
        expected = {m.value for m in gate_vocab.ObstructionKind}
        self.assertEqual(
            self._enum_of("obstruction", "properties", "kind"), expected
        )
        result_defs = load_schema("formal-search-result")["$defs"]
        self.assertEqual(
            set(result_defs["obstruction"]["properties"]["kind"]["enum"]), expected
        )

    def test_evidence_kinds_mirror_shared_vocab(self):
        expected = {m.value for m in gate_vocab.EvidenceKind}
        self.assertEqual(
            self._enum_of("worker-result", "properties", "evidence_kind"), expected
        )
        obstruction_evidence = load_schema("obstruction")["properties"]["evidence"][
            "items"
        ]["properties"]["kind"]
        self.assertEqual(set(obstruction_evidence["enum"]), expected)

    def test_worker_classes_mirror_shared_vocab(self):
        expected = {m.value for m in gate_vocab.WorkerClass}
        self.assertEqual(
            self._enum_of("worker-result", "properties", "worker_class"), expected
        )


if __name__ == "__main__":
    unittest.main()
