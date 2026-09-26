import os
import unittest

import psycopg


from artifacts.errors import ArtifactNotFoundError
from artifacts.hashing import hash_content
from artifacts.schema import apply_schema
from artifacts.store import ArtifactStore
from artifacts.errors import ArtifactStoreUnavailable

_TEST_DB_URL = os.environ.get(
    "ARTIFACT_DB_URL",
    "postgresql://atp_dev:devpassword@localhost:5432/persistent_atp_artifacts",
)


class TestArtifactStoreUnavailable(unittest.TestCase):
    def test_unreachable_host_raises_store_unavailable(self):
        # Port 1 is not a real Postgres port on any reasonable machine —
        # this should fail fast with a connection error, not hang.
        bad_url = "postgresql://atp_dev:devpassword@localhost:1/nope"
        with self.assertRaises(ArtifactStoreUnavailable):
            ArtifactStore(bad_url)

    def test_wrong_database_raises_store_unavailable(self):
        bad_url = "postgresql://atp_dev:devpassword@localhost:5432/this_db_does_not_exist"
        with self.assertRaises(ArtifactStoreUnavailable):
            ArtifactStore(bad_url)

class TestArtifactStorePut(unittest.TestCase):
    def setUp(self):
        apply_schema(_TEST_DB_URL)
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")
        self.store = ArtifactStore(_TEST_DB_URL)

    def tearDown(self):
        self.store.close()
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")

    def _row_count(self) -> int:
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM artifacts")
                (count,) = cur.fetchone()
                return count

    def test_put_returns_expected_hash(self):
        content = b"a Lean trace, or something like it"
        result_hash = self.store.put(content)
        from artifacts.hashing import hash_content
        self.assertEqual(result_hash, hash_content(content))

    def test_put_stores_exactly_one_row(self):
        self.store.put(b"hello")
        self.assertEqual(self._row_count(), 1)

    def test_put_same_content_twice_is_deduplicated(self):
        content = b"identical content"
        h1 = self.store.put(content)
        h2 = self.store.put(content)
        self.assertEqual(h1, h2)
        self.assertEqual(self._row_count(), 1)

    def test_put_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            self.store.put("not bytes")


class TestArtifactStoreGet(unittest.TestCase):
    def setUp(self):
        apply_schema(_TEST_DB_URL)
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")
        self.store = ArtifactStore(_TEST_DB_URL)

    def tearDown(self):
        self.store.close()
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")

    def test_get_returns_exact_bytes_round_trip(self):
        content = b"the exact bytes of a Lean trace"
        h = self.store.put(content)
        self.assertEqual(self.store.get(h), content)

    def test_get_round_trips_binary_content(self):
        content = bytes(range(256))  # not valid text, deliberately
        h = self.store.put(content)
        self.assertEqual(self.store.get(h), content)

    def test_get_unknown_hash_raises_not_found(self):
        unknown = hash_content(b"never stored")
        with self.assertRaises(ArtifactNotFoundError) as ctx:
            self.store.get(unknown)
        self.assertEqual(ctx.exception.artifact_hash, unknown)

    def test_get_malformed_hash_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.store.get("not-a-hash")


class TestArtifactStoreExists(unittest.TestCase):
    def setUp(self):
        apply_schema(_TEST_DB_URL)
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")
        self.store = ArtifactStore(_TEST_DB_URL)

    def tearDown(self):
        self.store.close()
        with psycopg.connect(_TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE artifacts")

    def test_exists_true_after_put(self):
        h = self.store.put(b"something")
        self.assertTrue(self.store.exists(h))

    def test_exists_false_for_unknown_hash(self):
        unknown = hash_content(b"never stored")
        self.assertFalse(self.store.exists(unknown))

    def test_exists_malformed_hash_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.store.exists("not-a-hash")



if __name__ == "__main__":
    unittest.main()