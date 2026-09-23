"""A2: the fake adapter emits every documented disposition, deterministically."""

import unittest

from commit_gate.vocab import RunDisposition
from mathproof.formal_atp import (
    EMITTABLE_DISPOSITIONS,
    FakeFormalATP,
    FormalATPAdapter,
    build_result,
    stub_replay,
)
from mathproof.schemas import validate

REQUEST = {
    "proof_id": "p1",
    "claim_id": "p1/c-1",
    "formal_declaration_id": "p1/fd-1",
    "run_id": "p1/fr-1",
    "base_revision": 0,
    "lease_id": "lease-p1",
    "fencing_token": 1,
    "lean_source_artifact": "sha256:" + "b2" * 32,
    "environment_id": "lean-pinned",
    "environment_hash": "sha256:" + "c3" * 32,
}


class TestEmittableDispositions(unittest.TestCase):
    def test_all_seven_documented_dispositions_are_emittable(self):
        self.assertEqual(
            EMITTABLE_DISPOSITIONS,
            {
                RunDisposition.PROVED_PENDING_REPLAY.value,
                RunDisposition.BUDGET_EXHAUSTED.value,
                RunDisposition.STAGNATED.value,
                RunDisposition.COUNTEREXAMPLE.value,
                RunDisposition.INVALID_REQUEST.value,
                RunDisposition.ENVIRONMENT_ERROR.value,
                RunDisposition.INTERNAL_ERROR.value,
            },
        )

    def test_build_result_emits_each_disposition_schema_valid(self):
        for disposition in sorted(EMITTABLE_DISPOSITIONS):
            with self.subTest(disposition=disposition):
                result = build_result(disposition, REQUEST)
                self.assertEqual(validate("formal-search-result", result), [])

    def test_unknown_disposition_is_refused(self):
        with self.assertRaises(ValueError):
            build_result("searching", REQUEST)

    def test_results_are_deterministic(self):
        for disposition in sorted(EMITTABLE_DISPOSITIONS):
            with self.subTest(disposition=disposition):
                self.assertEqual(
                    build_result(disposition, REQUEST),
                    build_result(disposition, dict(REQUEST)),
                )


class TestFakeFormalATP(unittest.TestCase):
    def test_satisfies_the_adapter_protocol(self):
        self.assertIsInstance(FakeFormalATP(), FormalATPAdapter)

    def test_scripted_start_returns_first_step(self):
        atp = FakeFormalATP({"p1/fr-1": ["stagnated", "proved-pending-replay"]})
        first = atp.formal_search_start(REQUEST)
        self.assertEqual(first["disposition"], "stagnated")

    def test_resume_consumes_the_script_in_order(self):
        atp = FakeFormalATP({"p1/fr-1": ["budget-exhausted", "proved-pending-replay"]})
        atp.formal_search_start(REQUEST)
        second = atp.formal_search_resume("p1/fr-1")
        self.assertEqual(second["disposition"], "proved-pending-replay")

    def test_status_does_not_consume(self):
        atp = FakeFormalATP({"p1/fr-1": ["counterexample"]})
        atp.formal_search_start(REQUEST)
        self.assertEqual(
            atp.formal_search_status("p1/fr-1")["disposition"], "counterexample"
        )
        self.assertEqual(
            atp.formal_search_status("p1/fr-1")["disposition"], "counterexample"
        )

    def test_cancel_stops_the_run(self):
        atp = FakeFormalATP({"p1/fr-1": ["proved-pending-replay"]})
        atp.formal_search_cancel("p1/fr-1")
        self.assertEqual(
            atp.formal_search_resume("p1/fr-1")["disposition"], "cancelled"
        )

    def test_request_missing_required_fields_is_invalid_request(self):
        atp = FakeFormalATP({"p1/fr-1": ["proved-pending-replay"]})
        broken = {k: v for k, v in REQUEST.items() if k != "fencing_token"}
        result = atp.formal_search_start(broken)
        self.assertEqual(result["disposition"], "invalid-request")

    def test_unscripted_run_is_invalid_request(self):
        atp = FakeFormalATP()
        result = atp.formal_search_start(REQUEST)
        self.assertEqual(result["disposition"], "invalid-request")

    def test_verbatim_payloads_pass_through(self):
        payload = {"run_id": "p1/fr-9", "disposition": "internal-error"}
        atp = FakeFormalATP({"p1/fr-9": [payload]})
        request = {**REQUEST, "run_id": "p1/fr-9"}
        self.assertEqual(atp.formal_search_start(request), payload)


class TestStubReplay(unittest.TestCase):
    def test_fixed_verified(self):
        replay = stub_replay("verified")
        verdict = replay({"artifact_hash": "x"}, "env-hash")
        self.assertEqual(verdict, {"status": "verified", "environment_hash": "env-hash"})

    def test_fixed_rejected(self):
        verdict = stub_replay("rejected")({}, "env-hash")
        self.assertEqual(verdict["status"], "rejected")

    def test_other_outcomes_are_refused(self):
        with self.assertRaises(ValueError):
            stub_replay("probably-fine")


if __name__ == "__main__":
    unittest.main()
