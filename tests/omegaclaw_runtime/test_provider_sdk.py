"""Real OpenAI SDK and Omega registry integration against a local provider."""
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


ROOT = Path(__file__).resolve().parents[2]


class CompatibleHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        if self.path not in {"/v1/chat/completions", "/deepseek/chat/completions",
                             "/openai/chat/completions", "/openrouter/chat/completions"}:
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "body": body,
                              "auth": self.headers.get("Authorization")})
        payload = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                   "model": body["model"], "choices": [{"index": 0,
                       "finish_reason": "tool_calls", "message": {"role": "assistant",
                           "content": None, "tool_calls": [{"id": "call-" + str(len(self.requests)),
                               "type": "function", "function": {"name": "proof-status",
                                   "arguments": json.dumps({"proof": 12 if len(self.requests) == 3 else "p1",
                                                            "workspace": "reference"})}}]}}]}
        wire = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def log_message(self, _format, *_args):
        pass


def test_openai_sdk_multiple_providers_in_omega_process(tmp_path):
    if importlib.util.find_spec("openai") is None:
        pytest.skip("OpenAI SDK unavailable; install persistent-atp[provider-sdk]")
    omega = Path(os.environ.get("OMEGA_PINNED_ROOT", str(ROOT / "tmp/repos/Omega")))
    if not (omega / "src/providers.py").is_file():
        pytest.skip("pinned Omega checkout unavailable")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    shutil.copy2(ROOT / "src/omegaclaw/providers.py", plugin_dir / "persistent_atp_providers.py")
    CompatibleHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CompatibleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.update({"OMEGA_TEST_ROOT": str(omega), "OMEGA_PINNED_ROOT": str(omega),
                "OMEGA_PROVIDER_PLUGIN_DIR": str(plugin_dir),
                "OMEGA_TEST_GATEWAY": endpoint, "OMEGACLAW_CUSTOM_API_KEY": "local-test-key",
                "OMEGACLAW_CUSTOM_BASE_URL": endpoint + "/v1",
                "OMEGACLAW_CUSTOM_MODEL": "fixture-model", "DEEPSEEK_MODEL": "deepseek-flash",
                "OMEGACLAW_OPENAI_MODEL": "fixture-openai",
                "OMEGACLAW_OPENROUTER_MODEL": "fixture-openrouter"})
    try:
        completed = subprocess.run([sys.executable, str(ROOT / "tests/omegaclaw_runtime/providers_process.py")],
            env=env, capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        result_line = next(line for line in completed.stdout.splitlines()
                           if line.startswith("SDK_PROVIDER_RESULT="))
        assert json.loads(result_line.split("=", 1)[1]) == {
            "registered": 4, "custom_roundtrips": 2, "rejected_malformed": True,
            "gateway_profiles": ["deepseek", "openai", "openrouter"]}
        probe = subprocess.run([sys.executable, "-m", "omegaclaw.deepseek_probe",
                                "--provider", "custom", "--proof", "p1"],
                               env=env, capture_output=True, text=True, timeout=20)
        assert probe.returncode == 0, probe.stderr + probe.stdout
        probe_result = json.loads(probe.stdout)
        assert probe_result["mode"] == "live-custom"
        assert probe_result["calls"][0]["name"] == "proof-status"
        assert len(CompatibleHandler.requests) == 7
        first, second, malformed, deepseek, openai, openrouter = CompatibleHandler.requests[:6]
        assert first["auth"] == "Bearer local-test-key"
        assert first["body"]["model"] == "fixture-model"
        assert first["body"]["tool_choice"] == "required"
        assert second["body"]["messages"][-1]["content"] == '{"event_hash": "sha256:committed"}'
        assert second["body"]["messages"][-1]["tool_call_id"] == "call-1"
        assert malformed["path"] == "/v1/chat/completions"
        assert deepseek["path"] == "/deepseek/chat/completions"
        assert deepseek["body"]["thinking"] == {"type": "disabled"}
        assert openai["path"] == "/openai/chat/completions"
        assert openai["body"]["max_completion_tokens"] == 128
        assert openrouter["path"] == "/openrouter/chat/completions"
        assert openrouter["body"]["max_tokens"] == 128
        assert CompatibleHandler.requests[-1]["path"] == "/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
