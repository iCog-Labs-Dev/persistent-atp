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
tool_message = providers.llmToolCallResponseMessage("tool", "tool-1", json.dumps(envelope))
next_request = providers.llmRequest(
    providers.llmRequestMessage("system", "Report only committed status"),
    [tool_message], 128, "low", [])
assert next_request.messages[1].content == json.dumps(envelope)
print("PROCESS_RESULT=" + json.dumps({"create": envelope,
    "next_model_request_saw_digest": envelope["event_hash"] in next_request.messages[1].content,
    "registered_skills": 9}, sort_keys=True), flush=True)
