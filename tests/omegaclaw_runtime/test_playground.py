"""Exercise the delivered runner, not a parallel implementation of its flow."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import threading

import pytest

from omegaclaw import playground
from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.tests.test_production import deployment, seed_reviewed, service_for


def runtime_roots():
    try:
        return playground.source_roots(SimpleNamespace(omega_root=None, petta_root=None))
    except playground.PlaygroundError as exc:
        pytest.skip(str(exc))


def test_local_playground_restarts_receiver_and_checks_export(tmp_path):
    omega, petta = runtime_roots()
    output = tmp_path / "local"
    assert playground.main(["local", "--output", str(output), "--omega-root", str(omega),
                            "--petta-root", str(petta)]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "passed" and report["revision"] == 2
    assert report["formal_verified"] is False
    assert len(json.loads((output / "fixture-model-calls.json").read_text())) == 5
    observation = json.loads((output / "observations.json").read_text())
    assert observation["export_check"]["revision"] == 2
    assert len(observation["operations"]) == 3


@pytest.mark.parametrize("wrong_scope", [False, True])
def test_live_playground_requires_real_commit_and_rejects_model_scope_change(
        request, tmp_path, monkeypatch, wrong_scope):
    omega, petta = runtime_roots()
    if not os.environ.get("OMEGACLAW_PRODUCTION_TEST_DB_URL"):
        pytest.skip("requires dedicated production test database")
    if os.environ.get("OMEGACLAW_PLAYGROUND_TEST_CHILD") != "1":
        # Native MORK binds a process to one authoritative journal. The core
        # production suite owns another journal, so this deployment is isolated.
        env = {**os.environ, "OMEGACLAW_PLAYGROUND_TEST_CHILD": "1"}
        selected = f"{Path(__file__).resolve()}::{request.node.originalname}[{wrong_scope}]"
        child = subprocess.run([sys.executable, "-m", "pytest", "-q", selected, "--tb=short"],
                               cwd=playground.ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert child.returncode == 0, child.stdout + child.stderr
        return
    deployment = request.getfixturevalue("deployment")
    service = service_for(deployment)
    proof = seed_reviewed(service)
    receiver = ReferenceReceiver(service)
    # The seed is a trusted test fixture, so explicitly grant its target bytes.
    asyncio.run(receiver.put_artifact(b"True", workspace_id="production", media_type="text/plain"))
    server = serve(service, "playground-live-fixture-token", receiver=receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    plan = [{"name": "formal-search", "arguments": {"proof": "unapproved" if wrong_scope else proof,
        "claim": f"{proof}/c-1", "budget": '{"max_steps":100,"wall_clock_ms":10000}', "workspace": "production"}},
        {"name": "proof-status", "arguments": {"proof": proof, "workspace": "production"}},
        {"name": "proof-audit", "arguments": {"proof": proof, "workspace": "production"}}]
    output = tmp_path / "live"
    try:
        with playground.fixture_model(plan=plan) as (url, calls):
            for name, value in {"PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}",
                "PERSISTENT_ATP_OMEGA_TOKEN": "playground-live-fixture-token",
                "PERSISTENT_ATP_OMEGA_RECEIVER_ID": "production-playground-fixture",
                "OMEGACLAW_CUSTOM_BASE_URL": url, "OMEGACLAW_CUSTOM_API_KEY": "local-fixture",
                "OMEGACLAW_CUSTOM_MODEL": "fixture"}.items():
                monkeypatch.setenv(name, value)
            command = ["live", "--output", str(output), "--proof-id", proof,
                       "--omega-root", str(omega), "--petta-root", str(petta)]
            result = playground.main(command)
            assert result == (2 if wrong_scope else 0), (output / "report.json").read_text()
            if wrong_scope:
                assert service.store.verify_chain(proof) == 2
                assert not any(item["proof_id"] == proof for item in deployment[3]["requests"].values())
                assert not (output / "intents.sqlite3").exists()
            else:
                assert service.store.verify_chain(proof) == 3
                observation = json.loads((output / "observations.json").read_text())
                formal = observation["operations"][0]["snapshot"]["result"]
                bundle = json.loads((output / "proof-export.json").read_text())
                assert formal["request_ref"] in bundle["artifacts"]
                assert formal["result_ref"] in bundle["artifacts"]
                assert deployment[3]["starts"][formal["run_id"]] == 1
                assert playground.main(command) == 0
                assert service.store.verify_chain(proof) == 3 and len(calls) == 3
    finally:
        server.shutdown()
        server.server_close()
        receiver.close()
        service.close()
