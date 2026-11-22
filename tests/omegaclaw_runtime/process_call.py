"""Child interpreter: pinned PeTTa evaluates Omega's real MeTTa plugin."""
import json
import os
import sys
import time
from pathlib import Path

from petta import PeTTa
import janus_swi


def execute(expr):
    values = runtime.process_metta_string(expr)
    if not values:
        raise AssertionError(f"MeTTa expression returned no result: {expr[:90]}")
    return values


petta_root = os.environ["PETTA_TEST_ROOT"]
omega_root = os.environ["OMEGA_TEST_ROOT"]
runtime = PeTTa(petta_path=petta_root)
janus_swi.query_once("asserta(working_dir(Dir))", {"Dir": petta_root})
execute("!(import! &self (library lib_import))")
for expression in (Path(omega_root) / "run.metta").read_text().splitlines()[1:3]:
    execute(expression)
try:
    execute('!(git-import! "https://github.com/singnet/Omega.git" "" "./repos" wrongrevision)')
except janus_swi.PrologError as exc:
    assert "pinned_omega_revision" in str(exc), exc
else:
    raise AssertionError("launcher accepted an incorrect Omega revision")
metadata = Path(omega_root) / ".git"
hidden_metadata = Path(omega_root) / ".git-hidden"
metadata.rename(hidden_metadata)
try:
    execute(expression)  # Sealed images do not need Git metadata at runtime.
    core = Path(omega_root) / "src/loop.metta"
    original_core = core.read_bytes()
    core.write_bytes(original_core + b"\n; altered core\n")
    try:
        execute(expression)
    except janus_swi.PrologError as exc:
        assert "pinned_omega_content" in str(exc), exc
    else:
        raise AssertionError("launcher accepted altered sealed runtime content")
    finally:
        core.write_bytes(original_core)
    manifest = Path(omega_root) / "persistent_atp_runtime.json"
    hidden_manifest = manifest.with_suffix(".json-hidden")
    manifest.rename(hidden_manifest)
    try:
        try:
            execute(expression)
        except janus_swi.PrologError as exc:
            assert "pinned_omega_metadata" in str(exc), exc
        else:
            raise AssertionError("launcher accepted neither Git metadata nor a build manifest")
    finally:
        hidden_manifest.rename(manifest)
finally:
    hidden_metadata.rename(metadata)
for module in ("utils.py", "helper.py", "utils", "logger.py", "log", "config.py", "config", "skills", "plugin.py", "plugin"):
    values = runtime.process_metta_string(f"!(import! &self (library Omega ./src/{module}))")
    if not module.endswith(".py"):
        assert values, module
sys.path.insert(0, omega_root + "/src")
sys.path.insert(0, omega_root)
import config
config.init_config([])
history = Path(omega_root) / "memory/history.metta"
history.parent.mkdir(exist_ok=True)
history.write_text("")
plugin_dir = omega_root + "/plugins/persistent_atp"
# Default disabled: policy is installed without registering workflows.
os.environ.pop("OMEGA_persistentAtpEnabled", None)
config.init_config([])
base_skills = janus_swi.query_once("'getSkills'(Skills)")["Skills"]
base_names = {str(skill[0]) for skill in base_skills}
execute(f"!(loadMettaPlugin persistent_atp {json.dumps(plugin_dir)})")
assert execute("!(getSkills)")[0].count('(proof-create "') == 0
assert "literal target" in execute('!(prompt-extension persistent-atp-policy)')[0].lower()
os.environ["OMEGA_persistentAtpEnabled"] = "enabled"
config.init_config([])
execute(f"!(loadMettaPlugin persistent_atp {json.dumps(plugin_dir)})")
catalogue = execute("!(py-call (plugin.listPlugins))")[0]
assert "persistent_atp" in catalogue and plugin_dir in catalogue, catalogue
assert "deepseek_provider" in catalogue, catalogue
skills_once = execute("!(getSkills)")[0]
assert skills_once.count('(proof-create "') == 1, skills_once
for name in ("proof-create", "proof-cycle", "proof-status", "proof-audit", "proof-export",
             "formal-search", "formal-resume", "statement-align", "lemma-bridge", "hypothesis-formalize", "hypothesis-revise", "parent-refine"):
    assert skills_once.count(f'({name} "') == 1, (name, skills_once)
execute(f"!(loadMettaPlugin persistent_atp {json.dumps(plugin_dir)})")
skills_twice = execute("!(getSkills)")[0]
assert skills_twice == skills_once, "reloading the plugin changed skill registration"
listener_count = janus_swi.query_once("findall(1, clause('heartbeat-listener'(persistent_atp, _, _), _), Bodies), length(Bodies, Count)")["Count"]
assert listener_count == 1, listener_count
for name, arity in (("proof-create", 3), ("proof-cycle", 3), ("proof-status", 3),
                    ("proof-audit", 3), ("proof-export", 4), ("formal-search", 5),
                    ("formal-resume", 5), ("statement-align", 5), ("lemma-bridge", 4), ("hypothesis-formalize", 5),
                    ("hypothesis-revise", 7), ("parent-refine", 8)):
    args = ",".join("_" for _ in range(arity))
    count = janus_swi.query_once(f"findall(1, clause('{name}'({args}), _), L), length(L, Count)")["Count"]
    assert count == 1, (name, count)

def await_reply(receipt, *, read=False, allow_error=False):
    receipt = json.loads(receipt)
    assert receipt["phase"] == "dispatching" and receipt["durable_admission"] is False, receipt
    deadline = time.monotonic() + 15
    iteration = 0
    while time.monotonic() < deadline:
        execute(f"!(heartbeat {iteration})")
        iteration += 1
        raw = history.read_text()
        found = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("PERSISTENT_ATP_RESULT "):
                continue
            record, _ = json.JSONDecoder().raw_decode(line[len("PERSISTENT_ATP_RESULT "):])
            if record.get("request_id") != receipt["request_id"]:
                continue
            if record.get("transport_status") == "error":
                assert allow_error, record
                return record
            if read and "revision" in record:
                found = record
            elif record.get("lifecycle") in {"succeeded", "failed"}:
                assert allow_error or record["lifecycle"] == "succeeded", record
                found = record["result"] or record
        if found:
            assert "PERSISTENT_ATP_RESULT" in history.read_text()
            return found
        time.sleep(0.03)
    raise AssertionError("no delivered result before test deadline")


target = "∀ n : Nat, n = n"
spec = json.dumps({"theorem": target, "definitions": {}, "assumptions": [],
                   "quantifier_domains": {"n": "Nat"}, "allowed_background": [],
                   "targets": ["literal theorem"], "verification_policy": "independent Lean replay"})
raw = execute(f'!(proof-create {json.dumps(spec)} "reference")')[0]
# PeTTa returns the bridge's serialized JSON string in its result list.
envelope = await_reply(raw)
assert envelope["phase"] == "finished", envelope
assert envelope["revision"] == 1, envelope
assert envelope["claim_status"] == "provisional", envelope
assert envelope["formal_verified"] is False, envelope
assert envelope["artifact_pointers"], envelope
proof = envelope["proof_id"]

# Omega's provider constructs the next request from a tool response. The
# actual response bytes above, not a harness-authored success string, enter it.
sys.path.insert(0, omega_root + "/src")
import providers
import plugin
plugin.loadPythonPlugin("deepseek_provider", plugin_dir)
providers.llmProviderStart("DeepSeek")
tool_names = {
    "proof-create": ("spec", "workspace"),
    "proof-cycle": ("proof", "workspace"),
    "proof-status": ("proof", "workspace"),
    "proof-audit": ("proof", "workspace"),
    "proof-export": ("proof", "destination", "workspace"),
    "formal-search": ("proof", "claim", "budget", "workspace"),
    "formal-resume": ("proof", "run", "budget", "workspace"),
    "statement-align": ("proof", "claim", "lean_source", "workspace"),
    "lemma-bridge": ("proof", "obstruction", "workspace"),
    "hypothesis-formalize": ("proof", "hypothesis", "expected_revision", "workspace"),
    "hypothesis-revise": ("proof", "hypothesis", "spec", "previous_claim", "expected_revision", "workspace"),
    "parent-refine": ("proof", "previous_declaration", "lemma", "budget", "expected_revision", "expected_theorem_hash", "workspace"),
}
native_skills = janus_swi.query_once("'getSkills'(Skills)")["Skills"]
tools = providers.getTools(native_skills)
actual_tools = {str(tool.name): tuple(str(parameter.name) for parameter in tool.parameters) for tool in tools}
assert {name: args for name, args in actual_tools.items() if name not in base_names} == tool_names, repr(actual_tools)
model_request = providers.llmRequest(providers.llmRequestMessage("system", "Use committed status"),
    [providers.llmRequestMessage("user", json.dumps({"proof": proof}))], 128, "low", tools)
model_response = providers.llmProviderChat(model_request)
assert not model_response.is_error(), model_response
assert len(model_response.calls) == 1, model_response
assert model_response.calls[0].name == "proof-status", model_response
model_call = model_response.calls[0]
status_raw = execute(f'!(proof-status {json.dumps(model_call.arguments["proof"])} '
                     f'{json.dumps(model_call.arguments["workspace"])})')[0]
status_envelope = await_reply(status_raw, read=True)
status_raw = json.dumps(status_envelope)
assert status_envelope["event_hash"] == envelope["event_hash"], status_envelope
next_model_request = providers.llmRequest(providers.llmRequestMessage("system", "Use committed status"),
    [providers.llmRequestMessage("user", json.dumps({"proof": proof})),
     providers.llmToolCallMessage("assistant", model_response.calls, "[TOOL CALL]"),
     providers.llmToolCallResponseMessage("tool", model_call.id, status_raw)],
    128, "low", tools)
next_model_response = providers.llmProviderChat(next_model_request)
assert not next_model_response.is_error(), next_model_response
assert next_model_response.calls[0].name == "proof-audit", next_model_response
untrusted_claim = providers.llmProviderChat(next_model_request)
assert untrusted_claim.is_error() and untrusted_claim.error == "DEEPSEEK_PROTOCOL_ERROR", untrusted_claim
bad_arguments = providers.llmProviderChat(next_model_request)
assert bad_arguments.is_error() and bad_arguments.error == "DEEPSEEK_PROTOCOL_ERROR", bad_arguments
unauthorized = providers.llmProviderChat(next_model_request)
assert unauthorized.is_error() and unauthorized.error == "DEEPSEEK_HTTP_401", unauthorized
from deepseek_provider import DeepSeekProvider
saved_key = os.environ.pop("DEEPSEEK_API_KEY")
try:
    try:
        DeepSeekProvider().start()
        raise AssertionError("provider accepted a missing API key")
    except RuntimeError as exc:
        assert "DEEPSEEK_API_KEY" in str(exc)
finally:
    os.environ["DEEPSEEK_API_KEY"] = saved_key
import config
saved_url = os.environ.pop("DEEPSEEK_API_URL")
os.environ.pop("DEEPSEEK_API_KEY")
try:
    config.init_config(["GATEWAY_URL=" + os.environ["DEEPSEEK_GATEWAY_TEST_URL"],
                        "model=deepseek-v4-pro"])
    providers.llmProviderStart("DeepSeek")
    gateway_response = providers.llmProviderChat(next_model_request)
    assert not gateway_response.is_error() and gateway_response.calls[0].name == "proof-audit", gateway_response
finally:
    os.environ["DEEPSEEK_API_URL"] = saved_url
    os.environ["DEEPSEEK_API_KEY"] = saved_key
plugin.loadPythonPlugin("persistent_atp_providers", plugin_dir)
sdk_roundtrip = False
if os.environ.get("OMEGACLAW_SDK_PROCESS_TEST") == "1":
    providers.llmProviderStart("PersistentATP-DeepSeek")
    sdk_response = providers.llmProviderChat(next_model_request)
    assert not sdk_response.is_error() and sdk_response.calls[0].name == "proof-audit", sdk_response
    sdk_roundtrip = True
tool_message = providers.llmToolCallResponseMessage("tool", "tool-1", json.dumps(envelope))
next_request = providers.llmRequest(
    providers.llmRequestMessage("system", "Report only committed status"),
    [tool_message], 128, "low", [])
assert next_request.messages[1].content == json.dumps(envelope)
assert not any(name in skills_once for name in ('(coordination-page "', '(proof-cycle-selected "', '(queue-research-moves "'))
assert envelope["event_hash"] in history.read_text()
# Exercise every registered signature through the real provider registry and
# native string-only tool calls. Unavailable formal work must remain blocked.
all_calls = {
    "proof-create": {"spec": spec}, "proof-cycle": {"proof": proof},
    "proof-status": {"proof": proof}, "proof-audit": {"proof": proof},
    "proof-export": {"proof": proof, "destination": "default"},
    "formal-search": {"proof": proof, "claim": f"{proof}/c-1", "budget": '{"max_steps":5,"wall_clock_ms":1000}'},
    "formal-resume": {"proof": proof, "run": f"{proof}/missing-run", "budget": '{"max_steps":5,"wall_clock_ms":1000}'},
    "statement-align": {"proof": proof, "claim": f"{proof}/c-1", "lean_source": envelope["artifact_pointers"][0]},
    "lemma-bridge": {"proof": proof, "obstruction": f"{proof}/missing-obstruction"},
    "hypothesis-formalize": {"proof": proof, "hypothesis": f"{proof}/hyp-" + "0" * 32, "expected_revision": "0"},
    "hypothesis-revise": {"proof": proof, "hypothesis": f"{proof}/hyp-" + "0" * 32,
        "spec": spec, "previous_claim": "", "expected_revision": "0"},
    "parent-refine": {"proof": proof, "previous_declaration": f"{proof}/fd-1", "lemma": f"{proof}/c-2",
        "budget": '{"max_steps":5,"wall_clock_ms":1000}', "expected_revision": "0",
        "expected_theorem_hash": "sha256:" + "0" * 64},
}
for name, arguments in all_calls.items():
    arguments["workspace"] = "reference"
    request = providers.llmRequest(providers.llmRequestMessage("system", "Call the requested tool"),
        [providers.llmRequestMessage("user", json.dumps({"proof": proof,
            "next_call": {"name": name, "arguments": arguments}}))], 128, "low", tools)
    response = providers.llmProviderChat(request)
    assert not response.is_error() and len(response.calls) == 1, response
    call = response.calls[0]
    assert call.name == name and call.arguments == arguments, call
    expression = f'!({name} ' + ' '.join(json.dumps(call.arguments[key]) for key in tool_names[name]) + ')'
    result = await_reply(execute(expression)[0], read=name == "proof-status", allow_error=True)
    assert not result.get("formal_verified"), result
    if name in {"formal-search", "formal-resume", "lemma-bridge", "hypothesis-formalize", "hypothesis-revise", "parent-refine"}:
        assert result.get("error_code"), result

# Run Omega's real recursive loop with a bounded test communication channel.
# Only provider/channel I/O is substituted; context, heartbeat, tool dispatch,
# history and next-turn request construction use the pinned upstream code.
import logging
logging.basicConfig(filename=str(Path(omega_root) / "loop-test.log"), level=logging.INFO, force=True)
os.environ["OPENAI_API_KEY"] = "mock-no-openai-network"
execute("!(import! &self (library lib_llm))")
for module in ("channels.py", "channels", "providers", "memory", "loop"):
    execute(f"!(import! &self (library Omega ./src/{module}))")
import channels
class LoopChannel(channels.CommChannel):
    received = 0
    confirmed = False
    def receive(self):
        self.received += 1
        logging.info("TEST_LOOP_RECEIVE %s confirmed=%s", self.received, self.confirmed)
        if self.confirmed: raise RuntimeError("OMEGA_LOOP_COMPLETE")
        if self.received > 20: raise RuntimeError("OMEGA_LOOP_TIMEOUT")
        return json.dumps({"proof": proof}) if self.received == 1 else ""
    def send(self, message):
        if message == "OMEGA_LOOP_CONFIRMED": self.confirmed = True
channel = LoopChannel()
channels.registerCommChannel("atp-test", channel)
channels.commChannelStart("atp-test")
os.environ["OMEGA_provider"] = "DeepSeek"
os.environ["DEEPSEEK_API_URL"] = saved_url
config.init_config(["sleepInterval=0.05", "maxOutputToken=128", "maxNewInputLoops=100"])
providers.llmProviderStart("DeepSeek")
execute("!(configure maxFeedback 50000)")
execute("!(configure maxHistory 200000)")
execute("!(initLoop)")
try:
    execute("!(omega 2)")
except janus_swi.PrologError as exc:
    assert "OMEGA_LOOP_COMPLETE" in str(exc), str(exc)
else:
    raise AssertionError("real Omega loop did not reach its bounded test-channel stop")
assert channel.confirmed

# A structured WebSocket task must finish even when the model allowance is
# zero. Exercise the installed frame hook, actual recursive MeTTa heartbeat,
# reference HTTP receiver/gate and final ACK without provider inference.
plugin.loadPythonPlugin("wschat", omega_root + "/channels")
import wschat
import omega_persistent_atp_bridge as bridge
wschat._ws_url = "ws://managed-process-fixture/session"
wschat._connected = True
managed_messages = []
def capture_managed(payload, **kwargs):
    managed_messages.append(payload)
    wschat._handle_frame(json.dumps({"type": "ack", "client_seq": payload["client_seq"], "seq": None}))
wschat._send_json = capture_managed
managed_frame = {"type": "user_message", "seq": 1, "text": "Complete a bounded proof task",
    "persistent_atp_task": {"spec": json.loads(spec), "max_cycles": 1, "max_research": 0,
                            "reply_prefix": "MANAGED_PROCESS_FINAL"}}
wschat._handle_frame(json.dumps(managed_frame))
wschat._handle_frame(json.dumps(managed_frame))  # Replay cannot create another task.
assert wschat.getLastMessage() == ""
assert len(bridge._get_adapter().state.snapshot()["tasks"]) == 1
bridge._get_adapter().state.update(lambda data: data.setdefault("continuation", {}).update(used=24))
class ManagedChannel(channels.CommChannel):
    received = 0
    def receive(self):
        self.received += 1
        tasks = bridge._get_adapter().state.snapshot()["tasks"]
        if managed_messages and all(task["phase"] == "done" for task in tasks.values()):
            raise RuntimeError("MANAGED_TASK_COMPLETE")
        if self.received > 1000:
            raise RuntimeError("MANAGED_TASK_TIMEOUT")
        return ""
    def send(self, message):
        raise AssertionError("managed task must use its durable response outbox")
channels.registerCommChannel("atp-managed", ManagedChannel())
channels.commChannelStart("atp-managed")
saved_chat = providers.llmProviderChat
def forbidden_chat(*args, **kwargs):
    raise AssertionError("zero-turn task attempted a model call")
providers.llmProviderChat = forbidden_chat
config.init_config(["sleepInterval=0.01", "maxNewInputLoops=" + ("100" if sdk_roundtrip else "0"),
                    "maxWakeLoops=0", "wakeupInterval=3600"])
execute("!(initLoop)")
try:
    execute("!(omega 2)")
except janus_swi.PrologError as exc:
    assert "MANAGED_TASK_COMPLETE" in str(exc), str(exc)
finally:
    providers.llmProviderChat = saved_chat
    wschat._connected = False
assert len(managed_messages) == 1
managed_task = next(iter(bridge._get_adapter().state.snapshot()["tasks"].values()))
assert managed_task["delivery"]["state"] == "acknowledged"
assert managed_task["status"]["formal_verified"] is False
assert set(managed_task["steps"]) == {"create", "cycle-1", "audit", "export"}
print("PROCESS_RESULT=" + json.dumps({"create": envelope,
    "next_model_request_saw_digest": envelope["event_hash"] in next_request.messages[1].content,
    "deepseek_mock_roundtrip": envelope["event_hash"] in status_raw,
    "sdk_provider_roundtrip": sdk_roundtrip,
    "gateway_request_without_key": gateway_response.calls[0].name == "proof-audit",
    "registered_skills": len(tool_names), "total_tools": len(tools), "actual_loop_verified": channel.confirmed,
    "zero_turn_task_acknowledged": managed_task["delivery"]["state"] == "acknowledged"}, sort_keys=True), flush=True)
