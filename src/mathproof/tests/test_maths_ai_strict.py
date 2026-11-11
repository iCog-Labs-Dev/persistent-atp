"""Strict production translation rejects incomplete evidence and retains bytes."""
from unittest.mock import patch

import pytest

from artifacts.hashing import hash_content
from commit_gate.canon import canonical_json
from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP
from mathproof.tests.test_maths_ai_http import REQUEST, TRACE, provider_result
from mathproof.witness import validate_witness


def terminal(adapter, request=REQUEST):
    result = provider_result()
    result["summary"]["nodes"][0]["hypotheses"] = request.get("hypotheses", [])
    result["certificate_hash"] = hash_content(canonical_json(TRACE))
    result["budget_used"] = {"tactic_steps": 1, "wall_clock_ms": 5}
    return {"run_id": request["run_id"], "status": "found", "result": result,
            "request": adapter._envelopes[request["run_id"]],
            "environment": adapter._envelopes[request["run_id"]]["environment"],
            "budget": adapter._envelopes[request["run_id"]]["budget"],
            "execution_environment_hash": request["environment_hash"]}


def adapter_for(request=REQUEST):
    adapter = MathsAIHTTPFormalATP("http://127.0.0.1:8000", strict=True)
    adapter.restore_request(request)
    return adapter


def test_exact_certificate_bytes_and_witness_survive_a_new_adapter():
    adapter = adapter_for()
    snapshot = terminal(adapter)
    with patch.object(adapter, "_call", return_value=snapshot):
        result = adapter.formal_search_status(REQUEST["run_id"])
    certificate = result["certificate"]
    assert adapter.artifact_bytes[certificate["artifact_hash"]] == canonical_json(TRACE)
    validate_witness(certificate["witness"], result)
    restored = adapter_for()
    with patch.object(restored, "_call", return_value=snapshot):
        assert restored.formal_search_status(REQUEST["run_id"]) == result
    assert restored.artifact_bytes == adapter.artifact_bytes


@pytest.mark.parametrize("mutation", ["context", "environment", "budget", "certificate"])
def test_missing_or_changed_evidence_is_never_ingested(mutation):
    adapter = adapter_for()
    snapshot = terminal(adapter)
    if mutation == "context": del snapshot["result"]["summary"]["nodes"][0]["hypotheses"]
    if mutation == "environment": snapshot["execution_environment_hash"] = "sha256:" + "0" * 64
    if mutation == "budget": snapshot["result"]["budget_used"]["tactic_steps"] = REQUEST["budget"]["max_steps"] + 1
    if mutation == "certificate": snapshot["result"]["certificate_hash"] = "sha256:" + "0" * 64
    with patch.object(adapter, "_call", return_value=snapshot), pytest.raises(MathsAIAPIError):
        adapter.formal_search_status(REQUEST["run_id"])


def test_separate_runs_do_not_reuse_state_or_tactic_ids():
    results = []
    for serial in (1, 2):
        request = {**REQUEST, "run_id": f"p1/fr-{serial}"}
        adapter = adapter_for(request)
        with patch.object(adapter, "_call", return_value=terminal(adapter, request)):
            results.append(adapter.formal_search_status(request["run_id"]))
    assert results[0]["root_state_id"] != results[1]["root_state_id"]
    assert results[0]["tactic_edges"][0]["tactic_id"] != results[1]["tactic_edges"][0]["tactic_id"]


def test_budget_exhaustion_is_not_a_dead_proof_or_a_counterexample():
    adapter = adapter_for()
    snapshot = terminal(adapter)
    snapshot["status"] = "exhausted"
    result = snapshot["result"]
    result.update(solved=False, exhausted=True, proof_trace=None, termination_reason="budget_exhausted")
    result["summary"].update(solved=False, exhausted=True, budget_exhausted=True)
    result["summary"]["nodes"][0]["status"] = "open"
    result["summary"]["edges"] = []
    with patch.object(adapter, "_call", return_value=snapshot):
        translated = adapter.formal_search_status(REQUEST["run_id"])
    assert translated["disposition"] == "budget-exhausted"
    assert translated["states"][0]["status"] == "open"
    assert not translated["obstructions"] and "certificate" not in translated


def test_interrupted_run_has_real_diagnostic_bytes_and_no_fabricated_goals():
    adapter = adapter_for()
    snapshot = terminal(adapter)
    snapshot.update(status="interrupted", result=None)
    with patch.object(adapter, "_call", return_value=snapshot):
        result = adapter.formal_search_status(REQUEST["run_id"])
    assert result["disposition"] == "environment-error"
    assert not result["states"] and not result["tactic_edges"]
    for artifact in result["artifacts"]:
        assert hash_content(adapter.artifact_bytes[artifact["sha256"]]) == artifact["sha256"]
