import unittest

from artifacts.hashing import hash_content, is_valid_hash, HASH_PATTERN


class TestHashContent(unittest.TestCase):
    def test_known_vector(self):
        # sha256 of the empty byte string is a well-known constant.
        self.assertEqual(
            hash_content(b""),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_deterministic(self):
        content = b"a Lean trace or whatever, doesn't matter"
        self.assertEqual(hash_content(content), hash_content(content))

    def test_distinct_content_distinct_hash(self):
        self.assertNotEqual(hash_content(b"foo"), hash_content(b"bar"))

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            hash_content("not bytes")

    def test_matches_own_pattern(self):
        self.assertRegex(hash_content(b"x"), HASH_PATTERN)


class TestIsValidHash(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid_hash(hash_content(b"x")))

    def test_wrong_prefix(self):
        self.assertFalse(is_valid_hash("md5:" + "a" * 64))

    def test_wrong_length(self):
        self.assertFalse(is_valid_hash("sha256:abc"))

    def test_uppercase_hex_rejected(self):
        self.assertFalse(is_valid_hash("sha256:" + "A" * 64))

    def test_non_string(self):
        self.assertFalse(is_valid_hash(12345))


if __name__ == "__main__":
    unittest.main()