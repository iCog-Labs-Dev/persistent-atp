"""O01/O02: real PeTTa process, pinned Omega plugin and HTTP gate path."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omegaclaw.api import serve
from omegaclaw.service import OperationService
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.runtime import DEFAULT_OMEGA_ROOT, OMEGA_REVISION
from omegaclaw.runtime import verify_manifest
from omegaclaw.installer import bundle_runtime

ROOT = Path(__file__).resolve().parents[2]
SDK_PATH = Path(os.environ.get("OMEGACLAW_PROVIDER_SDK_PATH",
                       sysconfig.get_paths()["purelib"]))


def unavailable(reason):
    if os.environ.get("OMEGA_RUNTIME_REQUIRED") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


class DeepSeekMockHandler(BaseHTTPRequestHandler):
    requests = []
    loop_request_id = None
    loop_verified = False

    def do_POST(self):
        if self.path not in {"/chat/completions", "/deepseek/chat/completions", "/loop/chat/completions"}:
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        decoded = json.loads(body)
        is_loop = "PROMPT: " in decoded["messages"][0]["content"] and " SKILLS: " in decoded["messages"][0]["content"]
        self.requests.append({"body": decoded, "path": self.path, "loop": is_loop,
                              "auth": self.headers.get("Authorization")})
        if is_loop:
            return self._loop_response(decoded)
        n = len(self.requests)
        messages = self.requests[-1]["body"]["messages"]
        user = next(message["content"] for message in messages if message["role"] == "user")
        parsed_user = json.loads(user)
        custom = parsed_user.get("next_call")
        if n == 5:
            self.send_error(401)
            return
        name = custom["name"] if custom else "proof-status" if n == 1 else "proof-audit"
        proof = parsed_user["proof"]
        if n == 3:
            response = {"choices": [{"finish_reason": "stop", "message":
                {"role": "assistant", "content": "I think it is proved"}}]}
        else:
            response = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"deepseek-call-{n}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(custom["arguments"] if custom else {
                        "proof": 12 if n == 4 else proof, "workspace": "reference"})}}]}}]}
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _loop_response(self, body):
        cls = type(self)
        loop_rows = [row for row in self.requests if row.get("loop")]
        proof = json.loads(next(message["content"] for message in self.requests[0]["body"]["messages"]
                                if message["role"] == "user"))["proof"]
        if len(loop_rows) == 1:
            name, arguments = "proof-status", {"proof": proof, "workspace": "reference"}
        else:
            for message in body["messages"]:
                if message["role"] == "tool":
                    match = re.search(r'"request_id":\s*"([a-f0-9]+)"', message["content"].replace('\\"', '"'))
                    if match: cls.loop_request_id = match[1]
            system = body["messages"][0]["content"].replace('\\"', '"').replace("_quote_", '"').replace("_newline_", "\n")
            for match in re.finditer("PERSISTENT_ATP_RESULT ", system):
                try:
                    record, _ = json.JSONDecoder().raw_decode(system[match.end():])
                except ValueError:
                    continue
                if (record.get("request_id") == cls.loop_request_id and record.get("proof_id") == proof
                        and record.get("revision") and not record.get("formal_verified")):
                    cls.loop_verified = True
            name, arguments = ("send", {"content": "OMEGA_LOOP_CONFIRMED"}) if cls.loop_verified else ("nop", {})
        data = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": f"loop-{len(loop_rows)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format, *_args):
        pass


@pytest.mark.parametrize("sdk_enabled", [
    pytest.param(False, id="legacy-provider"),
    pytest.param(True, id="sdk-provider", marks=pytest.mark.skipif(
        not (SDK_PATH / "openai/__init__.py").is_file(),
        reason="OpenAI SDK unavailable in PeTTa Python; set OMEGACLAW_PROVIDER_SDK_PATH")),
])
def test_o01_o02_real_omega_petta_process(tmp_path, sdk_enabled):
    omega_source = Path(os.environ.get("OMEGA_PINNED_ROOT", str(DEFAULT_OMEGA_ROOT)))
    petta_source = Path(os.environ.get("PETTA_PINNED_ROOT", str(ROOT / "tmp" / "repos" / "PeTTa")))
    if not (omega_source / "src/plugin.py").is_file() or not (petta_source / "src/main.pl").is_file():
        unavailable("pinned Omega/PeTTa checkouts unavailable; omega tier requires them")
    process_python = os.environ.get("PETTA_PYTHON", sys.executable)
    version = subprocess.run([process_python, "-c", "import sys; print(sys.version_info >= (3, 11))"], capture_output=True, text=True)
    assert version.stdout.strip() == "True", "PETTA_PYTHON must be Python >=3.11"
    if subprocess.run([process_python, "-c", "import janus_swi"], capture_output=True).returncode != 0:
        unavailable("janus_swi unavailable; omega tier requires it")
    petta_root = tmp_path / "petta"
    petta_root.mkdir()
    for folder in ("src", "lib", "python"):
        shutil.copytree(petta_source / folder, petta_root / folder)
    omega_root = petta_root / "repos" / "Omega"
    omega_root.parent.mkdir()
    shutil.copytree(omega_source, omega_root)
    plugin_dir = omega_root / "plugins" / "persistent_atp"
    catalogue = omega_root / "config" / "plugins.yaml"
    original_catalogue = catalogue.read_text()
    installer = ROOT / "scripts" / "omegaclaw-install-omega.py"
    for _ in range(2):
        installed = subprocess.run([sys.executable, str(installer), "--omega-root", str(omega_root)],
            capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
    assert original_catalogue in catalogue.read_text()
    bundle = tmp_path / "container-context"
    bundle_runtime(omega_root, bundle, "omega@sha256:" + "a" * 64)
    verify_manifest(bundle / "omega")
    container_catalogue = (bundle / "omega/config/plugins.yaml").read_text()
    assert str(plugin_dir) not in container_catalogue
    assert container_catalogue.count("/PeTTa/repos/Omega/plugins/persistent_atp") == 3
    assert (bundle / "omega/entrypoint.sh").read_bytes() == (omega_root / "entrypoint.sh").read_bytes()
    assert (plugin_dir / "coordinator-policy.txt").read_bytes() == (
        ROOT / "src/omegaclaw/omega_plugin/coordinator-policy.txt").read_bytes()
    assert catalogue.read_text().count("- name: deepseek_provider") == 1
    assert catalogue.read_text().count("- name: persistent_atp_providers") == 1
    assert (plugin_dir / "persistent_atp_providers.py").read_bytes() == (
        ROOT / "src/omegaclaw/providers.py").read_bytes()
    assert "location = /deepseek/chat/completions {" in (omega_root / "proxy/nginx.conf.template").read_text()
    assert (omega_root / "proxy/nginx.conf.template").read_text().count(
        "location = /deepseek/chat/completions {") == 1
    assert subprocess.run(["git", "-C", str(omega_source), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip() == OMEGA_REVISION
    service = OperationService(tmp_path / "service")
    receiver = ReferenceReceiver(service)
    server = serve(service, "omega-process-test-token", port=0, receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    DeepSeekMockHandler.requests = []
    DeepSeekMockHandler.loop_request_id = None
    DeepSeekMockHandler.loop_verified = False
    deepseek_server = ThreadingHTTPServer(("127.0.0.1", 0), DeepSeekMockHandler)
    deepseek_thread = threading.Thread(target=deepseek_server.serve_forever, daemon=True)
    deepseek_thread.start()
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(petta_root / "python"), "PETTA_TEST_ROOT": str(petta_root),
        "OMEGA_TEST_ROOT": str(omega_root), "PERSISTENT_ATP_OMEGA_TOKEN": "omega-process-test-token",
        "PERSISTENT_ATP_OMEGA_STATE": str(tmp_path / "omega-state.json"),
        "OMEGA_persistentAtpEnabled": "enabled", "OMEGA_persistentAtpWorkspace": "reference",
        "PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}",
        "DEEPSEEK_API_KEY": "mock-transport-only", "DEEPSEEK_TEST_MODE": "1",
        "DEEPSEEK_API_URL": f"http://127.0.0.1:{deepseek_server.server_port}/chat/completions",
        "DEEPSEEK_GATEWAY_TEST_URL": f"http://127.0.0.1:{deepseek_server.server_port}"})
    if sdk_enabled:
        env["PYTHONPATH"] += os.pathsep + str(SDK_PATH)
        env["OMEGACLAW_SDK_PROCESS_TEST"] = "1"
    try:
        try:
            completed = subprocess.run([process_python, str(ROOT / "tests/omegaclaw_runtime/process_call.py")],
                cwd=petta_root, env=env, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            (tmp_path / "process-output.log").write_bytes(exc.stdout or b"")
            (tmp_path / "process-error.log").write_bytes(exc.stderr or b"")
            pytest.fail(f"Omega loop timed out; {len(DeepSeekMockHandler.requests)} model requests; logs: {tmp_path}")
        assert completed.returncode == 0, completed.stderr[-6000:] + completed.stdout[-6000:]
        result_line = next(line for line in completed.stdout.splitlines() if line.startswith("PROCESS_RESULT="))
        result = json.loads(result_line.split("=", 1)[1])
        assert result["registered_skills"] == 11
        assert result["actual_loop_verified"] and DeepSeekMockHandler.loop_verified
        assert result["zero_turn_task_acknowledged"]
        assert result["next_model_request_saw_digest"] is True
        proof = result["create"]["proof_id"]
        assert service.store.head(proof)[0] >= 2
        assert service.store.verify_chain(proof) == service.store.head(proof)[0]
        assert service.artifacts.exists(result["create"]["artifact_pointers"][0])
        assert result["deepseek_mock_roundtrip"] is True
        assert result["sdk_provider_roundtrip"] is sdk_enabled
        transport_rows = [row for row in DeepSeekMockHandler.requests if not row.get("loop")]
        assert len(transport_rows) == (18 if result["sdk_provider_roundtrip"] else 17)
        first, second = (record["body"] for record in transport_rows[:2])
        for record in DeepSeekMockHandler.requests[:5]:
            assert record["auth"] == "Bearer mock-transport-only"
        gateway_record = DeepSeekMockHandler.requests[5]
        assert gateway_record["auth"] is None
        assert gateway_record["path"] == "/deepseek/chat/completions"
        assert gateway_record["body"]["model"] == "deepseek-v4-pro"
        if result["sdk_provider_roundtrip"]:
            sdk_record = DeepSeekMockHandler.requests[6]
            assert sdk_record["path"] == "/deepseek/chat/completions"
            assert sdk_record["body"]["thinking"] == {"type": "disabled"}
            assert result["create"]["event_hash"] in sdk_record["body"]["messages"][-1]["content"]
        assert first["model"] == "deepseek-flash"
        assert first["thinking"] == {"type": "disabled"}
        assert first["tool_choice"] == "required"
        assert len(first["tools"]) == result["total_tools"]
        assert all("strict" not in t["function"] for t in first["tools"])
        assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "proof-status"
        assert second["messages"][-1]["tool_call_id"] == "deepseek-call-1"
        assert result["create"]["event_hash"] in second["messages"][-1]["content"]
    finally:
        server.shutdown()
        server.server_close()
        deepseek_server.shutdown()
        deepseek_server.server_close()
        receiver.close()
