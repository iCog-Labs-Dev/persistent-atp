"""Contract tests for the Mathematics-ai-atp HTTP translation adapter."""

import unittest
import os
from unittest.mock import call, patch

from artifacts.hashing import hash_content
from mathproof.formal_atp import FormalATPAdapter
from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP
from mathproof.schemas import validate
from mathproof.soundness import validate_formal_search_result
from mathproof.tests.test_maths_ai_adapter import REQUEST as BASE_REQUEST


REQUEST = {
    **BASE_REQUEST,
    "hypotheses": ["h : Or p q"],
    "budget": {"max_steps": 50, "wall_clock_ms": 5000},
}

TRACE = {
    "goal": REQUEST["goal_text"],
    "tactic": "exact",
    "arguments": ["Or.comm"],
    "subgoals": [],
}


def summary(status="solved"):
    solved = status == "solved"
    dead = status == "dead"
    return {
        "root_id": 0,
        "solved": solved,
        "exhausted": dead,
        "num_nodes": 1,
        "num_edges": 1 if solved else 0,
        "nodes": [
            {
                "id": 0,
                "expression": REQUEST["goal_text"],
                "depth": 0,
                "status": status,
                "gnn_probability": 1.0,
                "stv": None,
                "combined_rank": 1.0,
                "note": "no candidates" if dead else None,
            }
        ],
        "edges": (
            [
                {
                    "id": 0,
                    "source_id": 0,
                    "tactic": "exact",
                    "arguments": ["Or.comm"],
                    "probability": 0.9,
                    "child_ids": [],
                    "status": "solved",
                }
            ]
            if solved
            else []
        ),
    }


def provider_result(status="solved", trace=TRACE):
    return {
        "solved": status == "solved",
        "exhausted": status == "dead",
        "termination_reason": (
            "solved" if status == "solved" else "exhausted" if status == "dead" else "open"
        ),
        "proof_trace": trace if status == "solved" else None,
        "summary": summary(status),
    }


def snapshot(atp, status, *, result=None, error=None):
    envelope = atp._provider_envelope(REQUEST)
    return {
        "run_id": REQUEST["run_id"],
        "status": status,
        "result": result,
        "error": error,
        "environment": envelope["environment"],
        "budget": envelope["budget"],
        "request": envelope,
    }


def started_adapter():
    atp = MathsAIHTTPFormalATP("http://localhost:8000")
    queued = snapshot(atp, "queued")
    with patch.object(atp, "_call", return_value=queued):
        result = atp.formal_search_start(REQUEST)
    assert result["disposition"] == "searching"
    return atp


class TestMathsAIHTTPFormalATP(unittest.TestCase):
    def test_satisfies_provider_boundary(self):
        self.assertIsInstance(
            MathsAIHTTPFormalATP("http://localhost:8000"),
            FormalATPAdapter,
        )

    def test_start_maps_canonical_request_to_provider(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        queued = snapshot(atp, "queued")
        with patch.object(atp, "_call", return_value=queued) as remote:
            result = atp.formal_search_start(REQUEST)

        expected = atp._provider_envelope(REQUEST)
        remote.assert_called_once_with(
            "POST",
            "/formal-search/start",
            expected,
            expected_statuses=frozenset({202}),
        )
        self.assertEqual(expected["problem"]["hypotheses"], REQUEST["hypotheses"])
        self.assertEqual(expected["budget"], {"timeout": 5.0})
        self.assertEqual(
            expected["environment"]["variables"]["persistent_atp_environment_hash"],
            REQUEST["environment_hash"],
        )
        self.assertEqual(result["disposition"], "searching")
        self.assertEqual(validate("formal-search-result", result), [])

    def test_status_translates_found_graph_and_candidate(self):
        atp = started_adapter()
        found = snapshot(atp, "found", result=provider_result())
        with patch.object(atp, "_call", return_value=found) as remote:
            result = atp.formal_search_status(REQUEST["run_id"])

        remote.assert_called_once_with("GET", "/formal-search/p1/fr-1")
        self.assertEqual(result["disposition"], "proved-pending-replay")
        self.assertEqual(result["certificate"]["status"], "candidate")
        self.assertEqual(validate("formal-search-result", result), [])
        self.assertEqual(validate_formal_search_result(result), ())

    def test_exhausted_run_becomes_stagnated(self):
        atp = started_adapter()
        exhausted = snapshot(atp, "exhausted", result=provider_result("dead"))
        with patch.object(atp, "_call", return_value=exhausted):
            result = atp.formal_search_status(REQUEST["run_id"])
        self.assertEqual(result["disposition"], "stagnated")
        self.assertEqual(validate_formal_search_result(result), ())

    def test_mismatched_trace_fails_closed(self):
        atp = started_adapter()
        bad_trace = {**TRACE, "tactic": "sorry"}
        found = snapshot(atp, "found", result=provider_result(trace=bad_trace))
        with patch.object(atp, "_call", return_value=found):
            result = atp.formal_search_status(REQUEST["run_id"])
        self.assertEqual(result["disposition"], "internal-error")
        self.assertNotIn("certificate", result)

    def test_changed_echoed_environment_fails_closed(self):
        atp = started_adapter()
        changed = snapshot(atp, "running")
        changed["environment"] = {"project": "other", "variables": {}}
        with patch.object(atp, "_call", return_value=changed):
            result = atp.formal_search_status(REQUEST["run_id"])
        self.assertEqual(result["disposition"], "internal-error")

    def test_resume_uses_retry_only_for_retryable_state(self):
        atp = started_adapter()
        interrupted = snapshot(atp, "interrupted")
        queued = snapshot(atp, "queued")
        with patch.object(atp, "_call", side_effect=[interrupted, queued]) as remote:
            result = atp.formal_search_resume(REQUEST["run_id"], REQUEST["budget"])

        self.assertEqual(result["disposition"], "searching")
        self.assertEqual(
            remote.call_args_list,
            [
                call("GET", "/formal-search/p1/fr-1"),
                call(
                    "POST",
                    "/formal-search/p1/fr-1/retry",
                    {},
                    expected_statuses=frozenset({202}),
                ),
            ],
        )

    def test_resume_rejects_budget_change_without_remote_call(self):
        atp = started_adapter()
        with patch.object(atp, "_call") as remote:
            result = atp.formal_search_resume(
                REQUEST["run_id"],
                {"max_steps": 100, "wall_clock_ms": 5000},
            )
        remote.assert_not_called()
        self.assertEqual(result["disposition"], "invalid-request")

    def test_cancel_translates_provider_snapshot(self):
        atp = started_adapter()
        cancelled = snapshot(atp, "cancelled")
        with patch.object(atp, "_call", return_value=cancelled) as remote:
            result = atp.formal_search_cancel(REQUEST["run_id"])
        remote.assert_called_once_with("POST", "/formal-search/p1/fr-1/cancel", {})
        self.assertEqual(result["disposition"], "cancelled")

    def test_replay_sends_bound_problem_and_trace(self):
        atp = started_adapter()
        found = snapshot(atp, "found", result=provider_result())
        with patch.object(atp, "_call", return_value=found):
            result = atp.formal_search_status(REQUEST["run_id"])

        with patch.object(
            atp,
            "_call",
            return_value={"verified": True, "status": "verified",
                          "run_id": REQUEST["run_id"],
                          "certificate_hash": result["certificate"]["artifact_hash"],
                          "environment_hash": REQUEST["environment_hash"]},
        ) as remote:
            verdict = atp.formal_replay(
                result["certificate"],
                REQUEST["environment_hash"],
            )

        remote.assert_called_once_with(
            "POST",
            "/formal-replay",
            {
                "run_id": REQUEST["run_id"],
                "problem": {
                    "goal": REQUEST["goal_text"],
                    "hypotheses": REQUEST["hypotheses"],
                },
                "proof_trace": TRACE,
            },
        )
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(
            atp._latest[REQUEST["run_id"]]["certificate"]["status"],
            "replay-accepted",
        )
        self.assertEqual(
            validate_formal_search_result(atp._latest[REQUEST["run_id"]]),
            (),
        )

    def test_new_certificate_does_not_inherit_old_replay(self):
        atp = started_adapter()
        first = snapshot(atp, "found", result=provider_result())
        with patch.object(atp, "_call", return_value=first):
            certificate = atp.formal_search_status(REQUEST["run_id"])["certificate"]
        with patch.object(atp, "_call", return_value={
            "verified": True, "status": "verified", "run_id": REQUEST["run_id"],
            "certificate_hash": certificate["artifact_hash"],
            "environment_hash": REQUEST["environment_hash"],
        }):
            atp.formal_replay(certificate, REQUEST["environment_hash"])

        other = provider_result()
        other["proof_trace"] = {**TRACE, "tactic": "simp", "arguments": []}
        other["summary"]["edges"][0].update(tactic="simp", arguments=[])
        with patch.object(atp, "_call", return_value=snapshot(atp, "found", result=other)):
            new_result = atp.formal_search_status(REQUEST["run_id"])
        self.assertNotEqual(new_result["certificate"]["artifact_hash"], certificate["artifact_hash"])
        self.assertEqual(new_result["certificate"]["status"], "candidate")
        with self.assertRaisesRegex(MathsAIAPIError, "certificate artifact"):
            atp.formal_replay(certificate, REQUEST["environment_hash"])

    def test_unattested_replay_cannot_verify(self):
        atp = started_adapter()
        with patch.object(atp, "_call", return_value=snapshot(atp, "found", result=provider_result())):
            certificate = atp.formal_search_status(REQUEST["run_id"])["certificate"]
        with patch.object(atp, "_call", side_effect=[
            {"verified": True, "status": "verified"},
            snapshot(atp, "found", result=provider_result()),
        ]):
            with self.assertRaisesRegex(MathsAIAPIError, "verification unavailable"):
                atp.formal_replay(certificate, REQUEST["environment_hash"])
        self.assertEqual(atp._latest[REQUEST["run_id"]]["certificate"]["status"], "candidate")

    def test_replay_loads_durable_trace_after_adapter_restart(self):
        class Blobs:
            def __init__(self):
                self.data = {}

            def put(self, content):
                digest = hash_content(content)
                self.data[digest] = content
                return digest

            def get(self, digest):
                return self.data[digest]

        db_url = os.environ.get("ARTIFACT_TEST_DB_URL")
        if db_url:
            from artifacts.store import ArtifactStore
            blobs = ArtifactStore(db_url)
        else:
            blobs = Blobs()
        first = MathsAIHTTPFormalATP("http://localhost:8000", artifact_store=blobs)
        with patch.object(first, "_call", return_value=snapshot(first, "queued")):
            first.formal_search_start(REQUEST)
        with patch.object(first, "_call", return_value=snapshot(first, "found", result=provider_result())):
            certificate = first.formal_search_status(REQUEST["run_id"])["certificate"]
        self.assertEqual(hash_content(blobs.get(certificate["artifact_hash"])),
                         certificate["artifact_hash"])

        if db_url:
            blobs.close()
            blobs = ArtifactStore(db_url)

        restarted = MathsAIHTTPFormalATP("http://localhost:8000", artifact_store=blobs)
        with patch.object(restarted, "_call", return_value=snapshot(restarted, "found", result=provider_result())):
            restored = restarted.restore_run(REQUEST)
        self.assertEqual(restored["certificate"]["artifact_hash"], certificate["artifact_hash"])
        verified = snapshot(restarted, "verified", result=provider_result())
        with patch.object(restarted, "_call", side_effect=[
            {"verified": True, "status": "verified"}, verified,
        ]) as remote:
            verdict = restarted.formal_replay(certificate, REQUEST["environment_hash"])
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(remote.call_args_list[0].args[:2], ("POST", "/formal-replay"))

        if db_url:
            import psycopg
            original = blobs.get(certificate["artifact_hash"])
            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE artifacts SET content = %s WHERE hash = %s",
                                (b"tampered", certificate["artifact_hash"]))
        else:
            blobs.data[certificate["artifact_hash"]] = b"tampered"
        with self.assertRaisesRegex(MathsAIAPIError, "artifact hash"):
            restarted.formal_replay(certificate, REQUEST["environment_hash"])
        if db_url:
            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE artifacts SET content = %s WHERE hash = %s",
                                (original, certificate["artifact_hash"]))
            blobs.close()

    def test_replay_rejects_wrong_attested_environment_or_certificate(self):
        atp = started_adapter()
        with patch.object(atp, "_call", return_value=snapshot(atp, "found", result=provider_result())):
            certificate = atp.formal_search_status(REQUEST["run_id"])["certificate"]
        for field in ("environment_hash", "certificate_hash", "run_id"):
            attestation = {
                "verified": True, "status": "verified",
                "run_id": REQUEST["run_id"],
                "certificate_hash": certificate["artifact_hash"],
                "environment_hash": REQUEST["environment_hash"],
            }
            attestation[field] = "wrong"
            with self.subTest(field=field), patch.object(atp, "_call", return_value=attestation):
                with self.assertRaisesRegex(MathsAIAPIError, "verification unavailable"):
                    atp.formal_replay(certificate, REQUEST["environment_hash"])
        self.assertEqual(atp._latest[REQUEST["run_id"]]["certificate"]["status"], "candidate")

    def test_lost_response_with_different_remote_request_is_not_adopted(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        changed = snapshot(atp, "queued")
        changed["request"] = {**changed["request"], "problem": {"goal": "False"}}
        with patch.object(atp, "_call", side_effect=[MathsAIAPIError("response lost"), changed]):
            with self.assertRaisesRegex(MathsAIAPIError, "bound request"):
                atp.formal_search_start(REQUEST)
        self.assertNotIn(REQUEST["run_id"], atp._admitted)

    def test_start_failure_before_admission_can_retry(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        absent = MathsAIAPIError("missing", status_code=404)
        with patch.object(atp, "_call", side_effect=[MathsAIAPIError("connection lost"), absent]):
            with self.assertRaisesRegex(MathsAIAPIError, "connection lost"):
                atp.formal_search_start(REQUEST)
        with patch.object(atp, "_call", return_value=snapshot(atp, "queued")) as remote:
            result = atp.formal_search_start(REQUEST)
        self.assertEqual(result["disposition"], "searching")
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[:2], ("POST", "/formal-search/start"))

    def test_lost_start_response_adopts_remote_run(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        with patch.object(atp, "_call", side_effect=[
            MathsAIAPIError("response lost"), snapshot(atp, "queued")
        ]) as remote:
            result = atp.formal_search_start(REQUEST)
        self.assertEqual(result["disposition"], "searching")
        self.assertEqual([c.args[0] for c in remote.call_args_list], ["POST", "GET"])

    def test_uncertain_admission_never_reposts_until_resolved(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        with patch.object(atp, "_call", side_effect=MathsAIAPIError("network down")):
            with self.assertRaises(MathsAIAPIError):
                atp.formal_search_start(REQUEST)
        with patch.object(atp, "_call", return_value=snapshot(atp, "queued")) as remote:
            result = atp.formal_search_start(REQUEST)
        self.assertEqual(result["disposition"], "searching")
        remote.assert_called_once_with("GET", "/formal-search/p1/fr-1")

    def test_provider_http_400_from_bound_replay_is_rejection(self):
        atp = started_adapter()
        found = snapshot(atp, "found", result=provider_result())
        with patch.object(atp, "_call", return_value=found):
            result = atp.formal_search_status(REQUEST["run_id"])
        error = MathsAIAPIError("Lean rejected tactic", status_code=400)
        with patch.object(atp, "_call", side_effect=error):
            verdict = atp.formal_replay(
                result["certificate"],
                REQUEST["environment_hash"],
            )
        self.assertEqual(verdict["status"], "rejected")
        self.assertIn("Lean rejected", verdict["rejection_reason"])

    def test_replay_rejects_environment_mismatch_locally(self):
        atp = started_adapter()
        found = snapshot(atp, "found", result=provider_result())
        with patch.object(atp, "_call", return_value=found):
            result = atp.formal_search_status(REQUEST["run_id"])
        with self.assertRaisesRegex(MathsAIAPIError, "environment"):
            atp.formal_replay(result["certificate"], "sha256:" + "00" * 32)

    def test_invalid_canonical_request_never_reaches_provider(self):
        atp = MathsAIHTTPFormalATP("http://localhost:8000")
        invalid = {**REQUEST, "unknown": True}
        with patch.object(atp, "_call") as remote:
            result = atp.formal_search_start(invalid)
        remote.assert_not_called()
        self.assertEqual(result["disposition"], "invalid-request")


if __name__ == "__main__":
    unittest.main()
