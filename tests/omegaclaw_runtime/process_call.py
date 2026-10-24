"""Child interpreter: pinned PeTTa evaluates Omega's real MeTTa plugin."""
import json
import os
import sys

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
execute('!(git-import! "https://github.com/singnet/Omega.git")')
for module in ("utils.py", "utils", "logger.py", "log", "skills", "plugin.py", "plugin"):
    values = runtime.process_metta_string(f"!(import! &self (library Omega ./src/{module}))")
    if not module.endswith(".py"):
        assert values, module
plugin_dir = omega_root + "/plugins/persistent_atp"
execute(f"!(loadMettaPlugin persistent_atp {json.dumps(plugin_dir)})")
catalogue = execute("!(py-call (plugin.listPlugins))")[0]
assert "persistent_atp" in catalogue and plugin_dir in catalogue, catalogue
assert "deepseek_provider" in catalogue, catalogue
skills_once = execute("!(getSkills)")[0]
assert skills_once.count('(proof-create "') == 1, skills_once
for name in ("proof-create", "proof-cycle", "proof-status", "proof-audit", "proof-export",
             "formal-search", "formal-resume", "statement-align", "lemma-bridge"):
    assert skills_once.count(f'({name} "') == 1, (name, skills_once)
execute(f"!(loadMettaPlugin persistent_atp {json.dumps(plugin_dir)})")
skills_twice = execute("!(getSkills)")[0]
assert skills_twice == skills_once, "reloading the plugin changed skill registration"

target = "∀ n : Nat, n = n"
spec = json.dumps({"proof_id": "omegaproc", "title": "Omega process", "literal_target": target})
raw = execute(f'!(proof-create {json.dumps(spec)} "reference")')[0]
# PeTTa returns the bridge's serialized JSON string in its result list.
envelope = json.loads(raw)
assert envelope["phase"] == "finished", envelope
assert envelope["revision"] == 1, envelope
assert envelope["claim_status"] == "provisional", envelope
assert envelope["formal_verified"] is False, envelope
assert envelope["artifact_pointers"], envelope

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
}
tools = [providers.LLMTool().with_name(name).with_description(name).with_parameters(
    [providers.LLMToolParameter().with_name(param) for param in params])
    for name, params in tool_names.items()]
model_request = providers.llmRequest(providers.llmRequestMessage("system", "Use committed status"),
    [providers.llmRequestMessage("user", "Check omegaproc")], 128, "low", tools)
model_response = providers.llmProviderChat(model_request)
assert not model_response.is_error(), model_response
assert len(model_response.calls) == 1, model_response
assert model_response.calls[0].name == "proof-status", model_response
model_call = model_response.calls[0]
status_raw = execute(f'!(proof-status {json.dumps(model_call.arguments["proof"])} '
                     f'{json.dumps(model_call.arguments["workspace"])})')[0]
status_envelope = json.loads(status_raw)
assert status_envelope["event_hash"] == envelope["event_hash"], status_envelope
next_model_request = providers.llmRequest(providers.llmRequestMessage("system", "Use committed status"),
    [providers.llmRequestMessage("user", "Check omegaproc"),
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
tool_message = providers.llmToolCallResponseMessage("tool", "tool-1", json.dumps(envelope))
next_request = providers.llmRequest(
    providers.llmRequestMessage("system", "Report only committed status"),
    [tool_message], 128, "low", [])
assert next_request.messages[1].content == json.dumps(envelope)
print("PROCESS_RESULT=" + json.dumps({"create": envelope,
    "next_model_request_saw_digest": envelope["event_hash"] in next_request.messages[1].content,
    "deepseek_mock_roundtrip": envelope["event_hash"] in status_raw,
    "gateway_request_without_key": gateway_response.calls[0].name == "proof-audit",
    "registered_skills": 9}, sort_keys=True), flush=True)
