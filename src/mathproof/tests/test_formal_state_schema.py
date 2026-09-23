"""E2: exact-state hash and semantic signature stay separate fields."""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "formal-state.schema.json"

EXAMPLE_STATE = {
    "state_id": "p1/fs-1",
    "kind": "or",
    "goal_text": "a + b = b + a",
    "environment_id": "env-lean-mathlib-pinned",
    "environment_hash": "sha256:" + "11" * 32,
    "serialization_version": 1,
    "exact_state_hash": "sha256:" + "22" * 32,
    "semantic_signature": "sha256:" + "33" * 32,
    "is_theorem": True,
    "status": "open",
}


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class TestFormalStateSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.validator = Draft202012Validator(cls.schema)

    def validate(self, payload):
        return list(self.validator.iter_errors(payload))

    def test_example_state_validates(self):
        self.assertEqual(self.validate(EXAMPLE_STATE), [])

    def test_exact_hash_and_semantic_signature_are_separate_required_fields(self):
        """Section 5.9: identity and similarity are never collapsed into one field."""
        props = self.schema["properties"]
        self.assertIn("exact_state_hash", self.schema["required"])
        self.assertIn("semantic_signature", self.schema["required"])
        self.assertNotIn("exact_semantic_hash", props)
        for name in ("exact_state_hash", "semantic_signature"):
            self.assertNotEqual(props[name].get("$ref"), None, name)

    def test_hashes_must_be_sha256_prefixed(self):
        bad = {**EXAMPLE_STATE, "exact_state_hash": "md5:deadbeef"}
        errors = self.validate(bad)
        self.assertTrue(any("exact_state_hash" in str(e.schema_path) for e in errors))

    def test_no_field_carries_truth(self):
        """Status is lifecycle vocabulary; nothing here asserts mathematical truth."""
        props = set(self.schema["properties"])
        for forbidden in ("truth", "is_true", "proved", "valid"):
            self.assertNotIn(forbidden, props)


if __name__ == "__main__":
    unittest.main()
