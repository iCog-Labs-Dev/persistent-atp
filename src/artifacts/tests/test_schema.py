import os
import unittest

import psycopg

from artifacts.schema import apply_schema, table_exists

_TEST_DB_URL = os.environ.get(
    "ARTIFACT_DB_URL",
    "postgresql://atp_dev:devpassword@localhost:5432/persistent_atp_artifacts",
)


class TestSchema(unittest.TestCase):
    def test_apply_schema_creates_table(self):
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS artifacts")

        self.assertFalse(table_exists(_TEST_DB_URL))
        apply_schema(_TEST_DB_URL)
        self.assertTrue(table_exists(_TEST_DB_URL))

    def test_apply_schema_is_idempotent(self):
        apply_schema(_TEST_DB_URL)
        apply_schema(_TEST_DB_URL)  # must not raise
        self.assertTrue(table_exists(_TEST_DB_URL))

    def test_hash_check_constraint_rejects_bad_hash(self):
        apply_schema(_TEST_DB_URL)
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                with self.assertRaises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO artifacts (hash, content, size_bytes) "
                        "VALUES (%s, %s, %s)",
                        ("not-a-valid-hash", b"x", 1),
                    )
            conn.rollback()  # clear the aborted transaction before conn closes


if __name__ == "__main__":
    unittest.main()