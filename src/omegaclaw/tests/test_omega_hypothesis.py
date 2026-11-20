"""Explicit candidate selection through Omega's revision-bound SDK action."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from commit_gate.ops import UpsertNode
from omegaclaw.api import serve
from omegaclaw.frameworks.omega import OmegaAdapter
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.tests.test_formal_v2_receiver import provider
from omegaclaw.tests.test_hypothesis_recovery import hypothesis, HypothesisCase
from omegaclaw.tests.test_research_recovery import ResearchCase
from omegaclaw.tests.test_result_artifact_grants import test_research_grants_are_available_before_snapshot_and_scoped as check_grants
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client import ArtifactRef


def arguments(case, revision=None):
    return {"proof": case.proof, "hypothesis": case.hypothesis,
            "expected_revision": str(case.revision if revision is None else revision)}


def load_bridge():
    path = Path(__file__).resolve().parents[1] / "omega_plugin/omega_persistent_atp_bridge.py"
    spec = importlib.util.spec_from_file_location("hypothesis_bridge_test", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


@pytest.mark.parametrize("transport", ["local", "http"])
def test_omega_tool_formalizes_one_selected_candidate(hypothesis, tmp_path, transport):
    check_formalization(hypothesis, tmp_path, transport)


def check_formalization(case, tmp_path, transport):
    receiver = ReferenceReceiver(case.service)
    server = thread = None
    if transport == "http":
        server = serve(case.service, "omega-hypothesis-test-token", receiver=receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = HTTPReceiverAdapter(f"http://127.0.0.1:{server.server_port}", "omega-hypothesis-test-token")
    else:
        port = LocalReceiverAdapter(receiver)
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "omega.json")
    bridge = load_bridge()
    bridge.configure_adapter(adapter)
    try:
        revision = adapter.status(case.proof)["revision"]
        receipt = json.loads(bridge.hypothesis_formalize(case.proof, case.hypothesis, str(revision), "reference"))
        assert receipt["durable_admission"] is False and receipt["phase"] == "dispatching"
        deadline = time.monotonic() + 10
        result = None
        while time.monotonic() < deadline:
            for line in bridge.take_completed().splitlines():
                report = json.loads(line.removeprefix("PERSISTENT_ATP_RESULT "))
                if report.get("request_id") == receipt["request_id"] and report.get("lifecycle") == "succeeded":
                    result = report["result"]
            if result is not None:
                break
            time.sleep(.02)
        assert result is not None, adapter.pending_reports()
        row = adapter.state.intent(receipt["request_id"])
        assert row["request"]["contract_version"] == "2.2"
        assert row["request"]["action"] == {"kind": "hypothesis-formalize", "proof_id": case.proof,
                                             "hypothesis_id": case.hypothesis}
        assert row["request"]["preconditions"]["expected_revision"] == revision
        assert row["arguments"]["expected_revision"] == str(revision)
        case.assert_complete(result)
        assert adapter.artifact_excerpt(ArtifactRef(result["report_artifact"]))["text"]
        assert not adapter.status(case.proof)["formal_verified"]
        assert "independent replay" in bridge.reporting_policy()
    finally:
        bridge.disable()
        bridge._pool.shutdown(wait=True)
        if server:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        receiver.close()
        adapter.close()


@pytest.mark.parametrize("revision", ["-1", "1.0", "", "revision", "٣"])
def test_omega_requires_an_explicit_decimal_revision(hypothesis, tmp_path, revision):
    adapter = OmegaAdapter(None, workspace="reference", cache_path=tmp_path / "invalid.json")
    with pytest.raises(ValueError, match="expected_revision"):
        adapter.reserve("hypothesis-formalize", "reference", {**arguments(hypothesis), "expected_revision": revision})
    assert not hypothesis.llm.history


def test_omega_rejects_hypotheses_outside_selected_proof(hypothesis, tmp_path):
    adapter = OmegaAdapter(None, workspace="reference", cache_path=tmp_path / "invalid.json")
    with pytest.raises(ValueError, match="outside"):
        adapter.reserve("hypothesis-formalize", "reference", {**arguments(hypothesis), "hypothesis": "foreign/hyp-" + "a" * 32})


def test_omega_does_not_fall_back_when_formalization_is_unavailable(hypothesis, tmp_path):
    receiver = ReferenceReceiver(hypothesis.service)
    class Unsupported(LocalReceiverAdapter):
        async def capabilities(self, *, contract_version="2.0"):
            caps = await self.receiver.capabilities(contract_version=contract_version)
            return replace(caps, actions=caps.actions - {"hypothesis-formalize"})
    adapter = OmegaAdapter(Unsupported(receiver), workspace="reference", cache_path=tmp_path / "unsupported.json")
    try:
        identity = adapter.reserve("hypothesis-formalize", "reference", arguments(hypothesis))
        assert adapter.execute(identity)["error_code"] == "UNSUPPORTED_CAPABILITY"
        assert receiver._db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert not hypothesis.llm.history
    finally:
        receiver.close()
        adapter.close()


def test_stale_selection_keeps_original_prepared_request_until_new_intent(hypothesis, tmp_path):
    receiver = ReferenceReceiver(hypothesis.service)
    class ChangeBeforeAdmission(LocalReceiverAdapter):
        changed = False
        async def submit(self, request):
            if not self.changed:
                self.changed = True
                with hypothesis.service.lock:
                    hypothesis.commit([UpsertNode("Claim", hypothesis.proof + "/c-99",
                        {"status": "provisional", "claim_text": "Concurrent change"})])
            return await self.receiver.submit(request)
    adapter = OmegaAdapter(ChangeBeforeAdmission(receiver), workspace="reference", cache_path=tmp_path / "stale.json")
    try:
        identity = adapter.reserve("hypothesis-formalize", "reference", arguments(hypothesis))
        result = adapter.execute(identity)
        assert result["error_code"] == "STALE_PRECONDITION" and not result["automatic_retry"]
        saved = adapter.state.intent(identity)["request"]
        assert saved["preconditions"]["expected_revision"] == hypothesis.revision
        assert adapter.execute(identity) == result
        assert adapter.state.intent(identity)["request"] == saved
        assert not hypothesis.llm.history
        assert not hypothesis.service.view.edges_from(hypothesis.hypothesis, "PROMOTED_TO")
        fresh = adapter.reserve("hypothesis-formalize", "reference", arguments(hypothesis,
            hypothesis.service.store.head(hypothesis.proof)[0]))
        assert fresh != identity
        assert adapter.execute(fresh)["durable_admission"]
        deadline = time.monotonic() + 5
        completed = None
        while time.monotonic() < deadline:
            rows = adapter.observe(fresh)
            completed = next((row["result"] for row in rows if row.get("lifecycle") == "succeeded"), None)
            if completed is not None:
                break
            time.sleep(.01)
        assert completed is not None and completed["formalization_ready"]
        assert len(hypothesis.llm.history) == 2
    finally:
        receiver.close()
        adapter.close()


@pytest.mark.parametrize("feature", ["artifact-grants", "omega-formalization"])
def test_native_omega_research_features(provider, tmp_path, monkeypatch, feature):
    if not os.environ.get("MORK_LIBRARY") or not os.environ.get("ARTIFACT_TEST_DB_URL"):
        if os.environ.get("OMEGA_PERSISTENCE_REQUIRED") == "1":
            pytest.fail("native Omega checks require MORK and a dedicated PostgreSQL database")
        pytest.skip("native MORK and dedicated PostgreSQL required")
    if os.environ.get("OMEGA_RESULT_FEATURE_CASE") != feature:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q",
            f"{__file__}::test_native_omega_research_features[{feature}]", "--basetemp", str(tmp_path / "native-process")],
            capture_output=True, text=True, timeout=60, env={**os.environ, "OMEGA_RESULT_FEATURE_CASE": feature})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ["ARTIFACT_TEST_DB_URL"])
    case = (ResearchCase(tmp_path / "receiver", profile="production") if feature == "artifact-grants"
        else HypothesisCase(tmp_path / "receiver", monkeypatch, profile="production"))
    try:
        if feature == "artifact-grants":
            check_grants(case, "http")
        else:
            check_formalization(case, tmp_path, "http")
    finally:
        case.service.close()
