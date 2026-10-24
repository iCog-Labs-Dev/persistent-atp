"""O01/O02: real PeTTa process, pinned Omega plugin and HTTP gate path."""
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
from omegaclaw.service import OperationService

ROOT = Path(__file__).resolve().parents[2]


class DeepSeekMockHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        if self.path not in {"/chat/completions", "/deepseek/chat/completions"}:
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append({"body": json.loads(body), "path": self.path,
                              "auth": self.headers.get("Authorization")})
        n = len(self.requests)
        if n == 5:
            self.send_error(401)
            return
        name = "proof-status" if n == 1 else "proof-audit"
        if n == 3:
            response = {"choices": [{"finish_reason": "stop", "message":
                {"role": "assistant", "content": "I think it is proved"}}]}
        else:
            response = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"deepseek-call-{n}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps({
                        "proof": 12 if n == 4 else "omegaproc", "workspace": "reference"})}}]}}]}
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format, *_args):
        pass


def test_o01_o02_real_omega_petta_process(tmp_path):
    omega_source = Path(os.environ.get("OMEGA_PINNED_ROOT", str(ROOT / "tmp" / "repos" / "Omega")))
    petta_source = Path(os.environ.get("PETTA_PINNED_ROOT", str(ROOT / "tmp" / "repos" / "PeTTa")))
    if not (omega_source / "src/plugin.py").is_file() or not (petta_source / "src/main.pl").is_file():
        pytest.skip("pinned Omega/PeTTa checkouts unavailable; omega tier requires them")
    process_python = os.environ.get("PETTA_PYTHON", "/usr/bin/python3")
    if subprocess.run([process_python, "-c", "import janus_swi"], capture_output=True).returncode != 0:
        pytest.skip("janus_swi unavailable; omega tier requires it")
    petta_root = tmp_path / "petta"
    petta_root.mkdir()
    for folder in ("src", "lib", "python"):
        shutil.copytree(petta_source / folder, petta_root / folder)
    omega_root = petta_root / "repos" / "Omega"
    omega_root.parent.mkdir()
    shutil.copytree(omega_source, omega_root, ignore=shutil.ignore_patterns(".git"))
    plugin_dir = omega_root / "plugins" / "persistent_atp"
    catalogue = omega_root / "config" / "plugins.yaml"
    original_catalogue = catalogue.read_text()
    installer = ROOT / "scripts" / "omegaclaw-install-omega.py"
    for _ in range(2):
        installed = subprocess.run([sys.executable, str(installer), "--omega-root", str(omega_root)],
            capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
    assert original_catalogue in catalogue.read_text()
    assert catalogue.read_text().count("- name: deepseek_provider") == 1
    assert "location = /deepseek/chat/completions {" in (omega_root / "proxy/nginx.conf.template").read_text()
    assert (omega_root / "proxy/nginx.conf.template").read_text().count(
        "location = /deepseek/chat/completions {") == 1
    assert subprocess.run(["git", "-C", str(omega_source), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip() == "63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5"
    service = OperationService(tmp_path / "service")
    server = serve(service, "omega-process-test-token", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    DeepSeekMockHandler.requests = []
    deepseek_server = ThreadingHTTPServer(("127.0.0.1", 0), DeepSeekMockHandler)
    deepseek_thread = threading.Thread(target=deepseek_server.serve_forever, daemon=True)
    deepseek_thread.start()
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(petta_root / "python"), "PETTA_TEST_ROOT": str(petta_root),
        "OMEGA_TEST_ROOT": str(omega_root), "PERSISTENT_ATP_OMEGA_TOKEN": "omega-process-test-token",
        "PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}",
        "DEEPSEEK_API_KEY": "mock-transport-only", "DEEPSEEK_TEST_MODE": "1",
        "DEEPSEEK_API_URL": f"http://127.0.0.1:{deepseek_server.server_port}/chat/completions",
        "DEEPSEEK_GATEWAY_TEST_URL": f"http://127.0.0.1:{deepseek_server.server_port}"})
    try:
        completed = subprocess.run([process_python, str(ROOT / "tests/omegaclaw_runtime/process_call.py")],
            cwd=petta_root, env=env, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr[-6000:] + completed.stdout[-6000:]
        result_line = next(line for line in completed.stdout.splitlines() if line.startswith("PROCESS_RESULT="))
        result = json.loads(result_line.split("=", 1)[1])
        assert result["registered_skills"] == 9
        assert result["next_model_request_saw_digest"] is True
        assert service.store.head("omegaproc")[0] == 1
        assert service.store.verify_chain("omegaproc") == 1
        assert service.artifacts.exists(result["create"]["artifact_pointers"][0])
        assert result["deepseek_mock_roundtrip"] is True
        assert len(DeepSeekMockHandler.requests) == 6
        first, second = (record["body"] for record in DeepSeekMockHandler.requests[:2])
        for record in DeepSeekMockHandler.requests[:5]:
            assert record["auth"] == "Bearer mock-transport-only"
        assert DeepSeekMockHandler.requests[-1]["auth"] is None
        assert DeepSeekMockHandler.requests[-1]["path"] == "/deepseek/chat/completions"
        assert DeepSeekMockHandler.requests[-1]["body"]["model"] == "deepseek-v4-pro"
        assert first["model"] == "deepseek-flash"
        assert first["thinking"] == {"type": "disabled"}
        assert first["tool_choice"] == "required"
        assert len(first["tools"]) == 9
        assert all("strict" not in t["function"] for t in first["tools"])
        assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "proof-status"
        assert second["messages"][-1]["tool_call_id"] == "deepseek-call-1"
        assert result["create"]["event_hash"] in second["messages"][-1]["content"]
    finally:
        server.shutdown()
        server.server_close()
        deepseek_server.shutdown()
        deepseek_server.server_close()
