"""Provider-neutral HTTP adapter contract checks."""

import unittest
from unittest.mock import patch

from mathproof.formal_atp import FormalATPAdapter, build_result
from mathproof.http_atp import ATPServiceError, HTTPFormalATP
from mathproof.tests.test_fake_adapter import REQUEST


class TestHTTPFormalATP(unittest.TestCase):
    def setUp(self):
        self.atp = HTTPFormalATP("https://atp.example.test/api", token="secret")
        self.result = build_result("budget-exhausted", REQUEST)

    def test_satisfies_provider_boundary(self):
        self.assertIsInstance(self.atp, FormalATPAdapter)

    def test_lifecycle_uses_canonical_routes(self):
        with patch.object(self.atp, "_call", return_value=self.result) as call:
            self.atp.formal_search_start(REQUEST)
            call.assert_called_with("POST", "/formal-search/start", REQUEST)

            self.atp.formal_search_resume(REQUEST["run_id"], {"max_steps": 5})
            call.assert_called_with(
                "POST",
                "/formal-search/p1%2Ffr-1/resume",
                {"budget": {"max_steps": 5}},
            )

            self.atp.formal_search_status(REQUEST["run_id"])
            call.assert_called_with("GET", "/formal-search/p1%2Ffr-1")

        cancelled = {"run_id": REQUEST["run_id"], "disposition": "cancelled"}
        with patch.object(self.atp, "_call", return_value=cancelled):
            self.assertEqual(
                self.atp.formal_search_cancel(REQUEST["run_id"]),
                cancelled,
            )

    def test_rejects_result_identity_mismatch(self):
        bad = {**self.result, "proof_id": "other"}
        with patch.object(self.atp, "_call", return_value=bad):
            with self.assertRaisesRegex(ATPServiceError, "proof_id"):
                self.atp.formal_search_start(REQUEST)

    def test_rejects_unbound_replay(self):
        wrong_environment = "sha256:" + "00" * 32
        with patch.object(
            self.atp,
            "_call",
            return_value={"status": "verified", "environment_hash": wrong_environment},
        ):
            with self.assertRaisesRegex(ATPServiceError, "unbound"):
                self.atp.formal_replay(
                    {"artifact_hash": "sha256:" + "11" * 32},
                    REQUEST["environment_hash"],
                )

    def test_rejects_invalid_base_url(self):
        with self.assertRaises(ValueError):
            HTTPFormalATP("file:///tmp/provider")


if __name__ == "__main__":
    unittest.main()
