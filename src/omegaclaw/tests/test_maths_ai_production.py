"""Actual companion API, native MORK/Postgres, synthetic search, real Lean replay."""
import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

import pytest

from omegaclaw.api import serve
from omegaclaw.factory import build_service
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.verify import verify_run
from omegaclaw.tests.test_production import seed_reviewed
from persistent_atp_client import ActionRequest, OperationRef, AsyncClient, Budget, FormalSearch, Preconditions
from persistent_atp_client.adapters.http import HTTPReceiverAdapter

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("scenario", ["search-and-replay", "restart-before-commit", "rejected-replay",
                                      "omega-search-and-replay", "replay-commit-recovery"])
def test_maths_ai_native_pipeline(request, tmp_path, monkeypatch, scenario):
    backend = os.environ.get("MATHS_AI_BACKEND_ROOT")
    if not backend or not os.environ.get("OMEGACLAW_PRODUCTION_TEST_DB_URL"):
        pytest.skip("requires companion backend and dedicated production test database")
    if os.environ.get("MATHS_AI_PIPELINE_CHILD") != "1":
        # Each native deployment owns a separate authoritative journal/process.
        env = {**os.environ, "MATHS_AI_PIPELINE_CHILD": "1"}
        result = subprocess.run([sys.executable, "-m", "pytest", "-q",
            f"{Path(__file__).resolve()}::{request.node.originalname}[{scenario}]", "--tb=short"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    backend = Path(backend).resolve()
    python = os.environ.get("MATHS_AI_BACKEND_PYTHON", str(backend / ".venv/bin/python"))
    env = os.environ.copy()
    if scenario == "rejected-replay": env["MATHS_AI_FIXTURE_BAD_PROOF"] = "1"
    errors = (tmp_path / "provider.log").open("w")
    provider = subprocess.Popen([python, str(ROOT / "tests/maths_ai_runtime/provider_server.py"),
        str(backend), str(tmp_path / "backend")], stdout=subprocess.PIPE, stderr=errors, text=True, env=env)
    service = receiver = server = None
    try:
        ready, _, _ = select.select([provider.stdout], [], [], 20)
        assert ready, "provider startup timed out"
        line = provider.stdout.readline()
        assert line, (tmp_path / "provider.log").read_text()
        endpoint = json.loads(line)["url"]
        monkeypatch.setenv("ARTIFACT_DB_URL", os.environ["OMEGACLAW_PRODUCTION_TEST_DB_URL"])
        monkeypatch.setenv("PERSISTENT_ATP_FORMAL_URL", endpoint)
        monkeypatch.setenv("PERSISTENT_ATP_FORMAL_TOKEN", "explicit-test-fixture")
        config = json.loads((ROOT / "src/omegaclaw/config/mathematics-ai.yaml").read_text())
        root = tmp_path / "receiver"
        service = build_service(config, root)
        proof = seed_reviewed(service, service.executor.environment_hash)
        token = "maths-ai-receiver-test-token"
        prepared = reference = None
        if scenario == "restart-before-commit":
            service.close()
            service = None
            config_path, marker = tmp_path / "config.json", tmp_path / "boundary"
            config_path.write_text(json.dumps(config))
            child = subprocess.Popen([sys.executable, str(ROOT / "tests/maths_ai_runtime/crash_worker.py"),
                str(config_path), str(root), proof, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([child.stdout], [], [], 15)
                assert ready, "receiver admission timed out"
                admission = json.loads(child.stdout.readline())
                end = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < end: time.sleep(.02)
                assert marker.exists()
            finally:
                child.kill()
                child.communicate(timeout=5)
            prepared = ActionRequest.from_dict(admission["request"])
            reference = OperationRef.from_dict(admission["reference"])
            service = build_service(config, root)

        def start_receiver():
            nonlocal receiver, server
            receiver = ReferenceReceiver(service)
            asyncio.run(receiver.put_artifact(b"True", workspace_id="production", media_type="text/plain"))
            server = serve(service, token, receiver=receiver)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return f"http://127.0.0.1:{server.server_port}"

        url = start_receiver()
        async def submit():
            async with AsyncClient(HTTPReceiverAdapter(url, token)) as client:
                request = client.prepare(FormalSearch(proof, proof + "/c-1", Budget(3, 5000)),
                    workspace_id="production", request_id="maths-ai-" + proof,
                    preconditions=Preconditions(expected_revision=2))
                handle = await client.submit(request)
                outcome = await handle.wait(timeout=10)
                assert outcome.lifecycle == "succeeded", outcome.result
                return request, handle.ref
        if scenario == "omega-search-and-replay":
            from omegaclaw import playground
            plan = [{"name": "formal-search", "arguments": {"proof": proof, "claim": proof + "/c-1",
                     "budget": '{"max_steps":3,"wall_clock_ms":5000}', "workspace": "production"}},
                    {"name": "proof-status", "arguments": {"proof": proof, "workspace": "production"}},
                    {"name": "proof-audit", "arguments": {"proof": proof, "workspace": "production"}}]
            with playground.fixture_model(plan=plan) as (model, calls):
                for name, value in {"PERSISTENT_ATP_OMEGA_URL": url, "PERSISTENT_ATP_OMEGA_TOKEN": token,
                    "PERSISTENT_ATP_OMEGA_RECEIVER_ID": "maths-ai-native-test",
                    "OMEGACLAW_CUSTOM_BASE_URL": model, "OMEGACLAW_CUSTOM_API_KEY": "fixture-only",
                    "OMEGACLAW_CUSTOM_MODEL": "fixture"}.items(): monkeypatch.setenv(name, value)
                output = tmp_path / "playground"
                code = playground.main(["live", "--proof-id", proof, "--atp-kind", "mathematics-ai-http",
                    "--max-steps", "3", "--wall-clock-ms", "5000", "--output", str(output)])
                assert code == 0, (output / "report.json").read_text()
                assert len(calls) == 3
                observations = json.loads((output / "observations.json").read_text())
                record = next(item for item in observations["operations"] if item["request"]["action"]["kind"] == "formal-search")
                prepared = ActionRequest.from_dict(record["request"])
                reference = OperationRef.from_dict(record["snapshot"]["ref"])
        elif prepared is None:
            prepared, reference = asyncio.run(submit())
        async def observe():
            async with AsyncClient(HTTPReceiverAdapter(url, token)) as client:
                snapshot = await client.attach(reference).wait(timeout=10)
                assert snapshot.lifecycle == "succeeded", snapshot.result
                assert (await client.submit(prepared)).ref == reference
                assert snapshot.result["commit_revision"] == 3
                return snapshot
        snapshot = asyncio.run(observe())
        assert service.view.projected_revision(proof) == service.store.verify_chain(proof) == 3
        assert (tmp_path / "backend/search-count").read_text() == "1"
        server.shutdown(); server.server_close(); server = None
        receiver.close(); receiver = None
        if scenario == "replay-commit-recovery":
            from commit_gate.canon import content_hash
            result = json.loads(service.artifacts.get(snapshot.result["result_ref"]))
            cert_id = result["certificate"]["witness"]["certificate_id"]
            verdict_lease = f"{proof}/replay-{content_hash([cert_id, 'verdict']).split(':')[1]}"
            release = service.store.release_lease
            def crash_after_verdict(proof_id, lease_id, **kwargs):
                if lease_id == verdict_lease:
                    raise RuntimeError("test interruption after journal commit before lease release")
                return release(proof_id, lease_id, **kwargs)
            service.store.release_lease = crash_after_verdict
            with pytest.raises(RuntimeError, match="test interruption"):
                verify_run(service, snapshot.result["run_id"], promote=True)
            service.store.release_lease = release
        replay = verify_run(service, snapshot.result["run_id"], promote=True)
        if scenario == "rejected-replay":
            assert replay["replay_status"] == "rejected" and replay["formal_verified"] is False
            assert service.view.node(replay["certificate_id"]).fields["status"] == "replay-rejected"
        else:
            assert replay["replay_status"] == "verified" and replay["formal_verified"] is True
        before = service.store.head(proof)
        assert verify_run(service, snapshot.result["run_id"], promote=True)["replay_id"] == replay["replay_id"]
        assert service.store.head(proof) == before
        assert service.artifacts.exists(replay["replay_artifact"])
        assert (tmp_path / "backend/replay-count").read_text() == "1"
        assert service.store.verify_chain(proof) == service.view.projected_revision(proof)
    finally:
        if server is not None: server.shutdown(); server.server_close()
        if receiver is not None: receiver.close()
        if service is not None: service.close()
        provider.terminate()
        try: provider.communicate(timeout=5)
        except subprocess.TimeoutExpired: provider.kill(); provider.communicate(timeout=5)
        errors.close()
