"""Bounded, restartable Omega/PeTTa experiment with durable tool-call intent.

Uses Omega's actual provider registry and MeTTa plugin; checkpoints are control
metadata, while the receiver journal remains mathematical authority.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
import sys
import uuid

from .contracts import ARGUMENTS
from . import providers as provider_plugin

EXTRA = {"coordination-page": ("proof", "cursor", "page_size", "workspace"),
         "proof-cycle-selected": ("proof", "category", "workspace"),
         "queue-research-moves": ("proof", "intent_key", "moves", "expected_revision", "workspace")}


def normalized_arguments(arguments):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError("duplicate planned JSON field")
            result[key] = value
        return result
    result = dict(arguments)
    for key in ("spec", "budget", "moves"):
        if key in result:
            result[key] = json.loads(result[key], object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite planned JSON")))
    return json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False)


def validate_planned_call(call, expected):
    if (call["name"] != expected["name"]
            or normalized_arguments(call["arguments"]) != normalized_arguments(expected["arguments"])):
        raise ValueError("model tool call differs from the trusted playground plan")


def save(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omega-root", type=Path, required=True, help="derived Omega checkout with plugin installed")
    parser.add_argument("--petta-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--provider", choices=tuple(provider_plugin.PROFILES), default="custom")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--plan", type=Path, help="optional trusted exact tool sequence; rejects deviations before effects")
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= 10:
        parser.error("turns must be within 1..10")
    args.omega_root = args.omega_root.resolve()
    args.petta_root = args.petta_root.resolve()
    args.state = args.state.resolve()
    plan = json.loads(args.plan.read_text()) if args.plan else None
    definitions = {**ARGUMENTS, **EXTRA}
    if plan is not None:
        if not isinstance(plan, list) or len(plan) != args.turns:
            parser.error("plan must contain exactly one call per turn")
        for call in plan:
            if (not isinstance(call, dict) or set(call) != {"name", "arguments"}
                    or call["name"] not in definitions or not isinstance(call["arguments"], dict)
                    or set(call["arguments"]) != set(definitions[call["name"]])
                    or any(not isinstance(value, str) for value in call["arguments"].values())):
                parser.error("plan has an invalid tool or string argument shape")
            normalized_arguments(call["arguments"])
    for name in ("PERSISTENT_ATP_OMEGA_INTENTS_DB", "PERSISTENT_ATP_OMEGA_RECEIVER_ID"):
        if not os.environ.get(name): parser.error(f"{name} is required")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    # One host owns a checkpoint. Receiver deduplication remains the authority.
    import fcntl
    owner = args.state.with_suffix(args.state.suffix + ".lock").open("a+")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identity = {"omega_root": str(args.omega_root.resolve()), "petta_root": str(args.petta_root.resolve()),
                "provider": args.provider, "prompt": args.prompt,
                "receiver_id": os.environ["PERSISTENT_ATP_OMEGA_RECEIVER_ID"],
                "receiver_url": os.environ.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765").rstrip("/")}
    if plan is not None: identity["plan"] = plan
    if args.state.exists():
        state = json.loads(args.state.read_text())
        if state["identity"] != identity:
            parser.error("checkpoint belongs to another experiment; use a new state file")
    else:
        state = {"identity": identity, "run_id": uuid.uuid4().hex, "turn": 0,
                 "history": [{"role": "user", "content": args.prompt}], "pending": []}
        save(args.state, state)
    runtime_root = args.state.parent / (args.state.stem + "-runtime")
    if not runtime_root.exists():
        staging = runtime_root.with_name(runtime_root.name + ".building")
        if staging.exists():
            shutil.rmtree(staging)
        for folder in ("src", "lib", "python"):
            shutil.copytree(args.petta_root / folder, staging / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(args.omega_root, staging / "repos/Omega",
                        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))
        staging.rename(runtime_root)
    omega_runtime = runtime_root / "repos/Omega"
    sys.path.insert(0, str(runtime_root / "python"))
    sys.path.insert(0, str(omega_runtime / "src"))
    os.chdir(runtime_root)  # SWI captures its working directory on initialization.
    from petta import PeTTa
    import janus_swi
    import config
    import plugin
    import providers
    config.init_config([])
    runtime = PeTTa(petta_path=str(runtime_root))
    janus_swi.query_once("asserta(silent(true))")
    janus_swi.query_once("working_directory(_, Dir)", {"Dir": str(runtime_root)})
    janus_swi.query_once("asserta(working_dir(Dir))", {"Dir": str(runtime_root)})
    os.chdir(runtime_root)
    runtime.process_metta_string("!(import! &self (library lib_import))")
    runtime.process_metta_string('!(git-import! "https://github.com/singnet/Omega.git")')
    for module in ("utils.py", "utils", "logger.py", "log", "skills", "plugin.py", "plugin"):
        loaded = runtime.process_metta_string(f"!(import! &self (library Omega ./src/{module}))")
        if not loaded:
            raise ValueError(f"could not load pinned Omega module {module}")
    location = str(args.omega_root.resolve() / "plugins/persistent_atp")
    runtime.process_metta_string(f"!(loadMettaPlugin persistent_atp {json.dumps(location)})")
    plugin.loadPythonPlugin("persistent_atp_providers", location)
    providers.llmProviderStart(provider_plugin.PROFILES[args.provider].name)
    tools = [providers.LLMTool().with_name(name).with_description("Persistent ATP: " + name)
        .with_parameters([providers.LLMToolParameter().with_name(field) for field in fields])
        for name, fields in definitions.items()]

    def message(item):
        if item["role"] == "tool":
            return providers.llmToolCallResponseMessage("tool", item["callid"], item["content"])
        if "calls" in item:
            calls = [providers.LLMToolCall().with_id(call["id"]).with_name(call["name"])
                     .with_arguments(call["arguments"]) for call in item["calls"]]
            return providers.llmToolCallMessage("assistant", calls, item["content"])
        return providers.llmRequestMessage(item["role"], item["content"])

    try:
        while state["turn"] < args.turns:
            if not state["pending"]:
                request = providers.llmRequest(providers.llmRequestMessage("system",
                    "Use only registered proof tools. Read authoritative results. "
                    "Operation success and heuristic opinions are not Lean verification."),
                    [message(item) for item in state["history"]], 1024, "low",
                    [tool for tool in tools if tool.name == plan[state["turn"]]["name"]] if plan else tools)
                response = providers.llmProviderChat(request)
                if response.is_error():
                    print(json.dumps({"status": "provider-error", "error": response.error}))
                    return 2
                calls = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in response.calls]
                if not 1 <= len(calls) <= 5:
                    raise ValueError("one to five tool calls are required")
                for call in calls:
                    if call["name"] not in definitions or set(call["arguments"]) != set(definitions[call["name"]]):
                        raise ValueError("model returned an unknown tool or argument shape")
                if plan is not None:
                    if len(calls) != 1: raise ValueError("trusted plan permits one call per turn")
                    validate_planned_call(calls[0], plan[state["turn"]])
                state["history"].append({"role": "assistant", "calls": calls, "content": "[TOOL CALL]"})
                state["pending"] = [{**call, "intent": f"{state['run_id']}:{state['turn']}:{index}"}
                                    for index, call in enumerate(calls)]
                save(args.state, state)  # Model response and intent precede all effects.
            while state["pending"]:
                call = state["pending"][0]
                if plan is not None:
                    if len(state["pending"]) != 1: raise ValueError("pending checkpoint violates the trusted plan")
                    validate_planned_call(call, plan[state["turn"]])
                expression = "!(persistent-atp-invoke " + " ".join(json.dumps(value, ensure_ascii=False)
                    for value in (call["intent"], call["name"], json.dumps(call["arguments"], ensure_ascii=False))) + ")"
                result = runtime.process_metta_string(expression)
                if len(result) != 1 or not isinstance(result[0], str):
                    raise ValueError("Omega plugin returned an invalid result")
                outcome = json.loads(result[0])
                if outcome.get("error_code") in {"API_UNAVAILABLE", "OBSERVATION_TIMEOUT", "TRANSIENT_UNAVAILABLE"}:
                    print(json.dumps({"status": "reconciliation-required", "intent": call["intent"]}))
                    return 2
                state["history"].append({"role": "tool", "callid": call["id"], "content": result[0]})
                state["pending"].pop(0)
                if not state["pending"]:
                    state["turn"] += 1
                save(args.state, state)
                print(json.dumps({"tool": call["name"], "intent": call["intent"], "result": outcome}, ensure_ascii=False))
    finally:
        providers._llmProviderRegistry[provider_plugin.PROFILES[args.provider].name].stop()
        owner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
