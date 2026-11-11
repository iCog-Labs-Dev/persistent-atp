"""Actual Omega/PeTTa bounded driver survives death after a tool commits."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omegaclaw.api import serve
from omegaclaw.installer import install
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService

ROOT = Path(__file__).resolve().parents[2]


def test_bounded_omega_driver_reuses_tool_intent_after_restart(tmp_path):
    if importlib.util.find_spec("janus_swi") is None or importlib.util.find_spec("openai") is None:
        pytest.skip("requires Omega optional dependencies")
    omega = Path(os.environ.get("OMEGA_PINNED_ROOT", str(ROOT / "tmp/repos/Omega")))
    petta = Path(os.environ.get("PETTA_PINNED_ROOT", str(ROOT / "tmp/repos/PeTTa")))
    if not (omega / "src/plugin.py").exists() or not (petta / "src/main.pl").exists():
        pytest.skip("requires pinned Omega and PeTTa checkouts")
    derived = tmp_path / "Omega"
    shutil.copytree(omega, derived, ignore=shutil.ignore_patterns(".git"))
    install(derived)
    service = OperationService(tmp_path / "service")
    committed, release = threading.Event(), threading.Event()
    original_create = service._create
    def delayed_create(*args):
        result = original_create(*args)
        committed.set()
        assert release.wait(timeout=15)
        return result
    service._create = delayed_create
    receiver = ReferenceReceiver(service)
    server = serve(service, "bounded-experiment-test-token", receiver=receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    calls = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request)
            name = "proof-create" if len(calls) == 1 else "proof-status"
            arguments = ({"spec": json.dumps({"proof_id": "bounded", "literal_target": "True", "title": "Bounded fixture"}),
                          "workspace": "reference"} if name == "proof-create" else {"proof": "bounded", "workspace": "reference"})
            body = json.dumps({"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "call-" + str(len(calls)), "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments)}}]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env.update({"PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}",
        "PERSISTENT_ATP_OMEGA_TOKEN": "bounded-experiment-test-token",
        "PERSISTENT_ATP_OMEGA_INTENTS_DB": str(tmp_path / "intents.sqlite3"),
        "PERSISTENT_ATP_OMEGA_RECEIVER_ID": "bounded-test-principal",
        "OMEGACLAW_CUSTOM_API_KEY": "local-fixture-key", "OMEGACLAW_CUSTOM_MODEL": "fixture",
        "OMEGACLAW_CUSTOM_BASE_URL": f"http://127.0.0.1:{model.server_port}/v1"})
    state = tmp_path / "experiment.json"
    command = [sys.executable, "-m", "omegaclaw.experiment", "--omega-root", str(derived),
        "--petta-root", str(petta), "--state", str(state), "--provider", "custom",
        "--turns", "2", "--prompt", "Create the fixture target and read committed status."]
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not committed.wait(timeout=10):
            child.kill()
            output, error = child.communicate(timeout=5)
            pytest.fail(output[-2000:] + error[-4000:])
        child.kill()
        child.communicate(timeout=5)
        assert json.loads(state.read_text())["pending"]
        release.set()
        recovered = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        assert recovered.returncode == 0, recovered.stdout[-2000:] + recovered.stderr[-4000:]
        assert service.store.verify_chain("bounded") == 1
        assert json.loads(state.read_text())["turn"] == 2
        assert len(calls) == 2
        assert "event_hash" in calls[1]["messages"][-1]["content"]
        repeated = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        assert repeated.returncode == 0, repeated.stderr
        assert len(calls) == 2
        assert service.store.verify_chain("bounded") == 1
    finally:
        release.set()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        model.shutdown()
        model.server_close()
        server.shutdown()
        server.server_close()
        receiver.close()
