"""Inspectable Omega end-to-end playground with local and live scenarios.

Local mode owns a reference receiver and a deterministic model endpoint. Live
mode connects to an existing production receiver and the selected real model.
Neither mode treats operation completion as independent Lean verification.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx

from persistent_atp_client import ActionRequest, AsyncClient, ArtifactRef, OperationRef
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import to_dict

from .export import check_export
from .installer import install

ROOT = Path(__file__).resolve().parents[2]


class PlaygroundError(RuntimeError):
    pass


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Report:
    def __init__(self, output, mode):
        self.output = output
        self.data = {"mode": mode, "status": "running", "checks": [],
                     "independent_lean_replay_tested": False,
                     "model": "deterministic fixture" if mode == "local" else "configured live provider",
                     "receiver": "scripted reference" if mode == "local" else "configured production receiver"}
        if mode == "components":
            self.data.update({"receiver": "native MORK/PostgreSQL production composition under test",
                              "model": "none; synthetic HTTP ATP fixture"})
        self.save()

    def check(self, name, detail=None):
        self.data["checks"].append({"name": name, "status": "passed", "detail": detail})
        print(f"PASS {name}", flush=True)
        self.save()

    def save(self):
        write_json(self.output / "report.json", self.data)
        lines = ["# Omega playground result", "", f"Status: **{self.data['status']}**",
                 f"Mode: {self.data['mode']}", "", f"Receiver: {self.data['receiver']}",
                 f"Model: {self.data['model']}", "",
                 "Operation success is separate from independent Lean verification.", ""]
        lines += [f"- {item['status']}: {item['name']}" for item in self.data["checks"]]
        if self.data.get("error"):
            lines += ["", f"Failure: {self.data['error']}"]
        lines += ["", "Inspect report.json, driver.log, experiment.json, observations.json,",
                  "intents.sqlite3 and the derived Omega checkout in this directory.", ""]
        (self.output / "report.md").write_text("\n".join(lines))


def source_roots(args):
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    original = Path(common.stdout.strip()).parent
    roots = []
    for option, variable, name in ((args.omega_root, "OMEGA_PINNED_ROOT", "Omega"),
                                   (args.petta_root, "PETTA_PINNED_ROOT", "PeTTa")):
        candidates = ([option] if option else [Path(os.environ[variable])] if os.environ.get(variable)
                      else [ROOT / "tmp/repos" / name, original / "tmp/repos" / name])
        path = next((item.resolve() for item in candidates if item.is_dir()), None)
        if path is None:
            raise PlaygroundError(f"set {variable} or provide --{name.lower()}-root")
        roots.append(path)
    if not (roots[0] / "src/plugin.py").is_file() or not (roots[1] / "src/main.pl").is_file():
        raise PlaygroundError("Omega/PeTTa roots are not complete source checkouts")
    for dependency in ("openai", "janus_swi"):
        if importlib.util.find_spec(dependency) is None:
            raise PlaygroundError(f"missing {dependency}; install the provider-sdk and omega extras")
    if shutil.which("swipl") is None:
        raise PlaygroundError("SWI-Prolog is unavailable")
    return roots


def stop(child):
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)


def run_process(command, *, env, log, timeout):
    with log.open("a") as stream:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            stop(child)
            raise PlaygroundError(f"process stopped; checkpoint retained; inspect {log.name}") from None
    if code:
        raise PlaygroundError(f"process exited {code}; inspect {log.name}; reuse the same live output to resume")


class LocalReceiver:
    def __init__(self, output, env):
        self.output, self.env, self.child = output, env, None
        self.config = output / "receiver.json"
        write_json(self.config, {"root": str(output / "receiver"), "workspace": "reference",
            "host": "127.0.0.1", "port": 0, "token_env": "PERSISTENT_ATP_OMEGA_TOKEN"})

    def start(self):
        self.stream = (self.output / "receiver.log").open("a")
        offset = self.stream.tell()
        self.child = subprocess.Popen([sys.executable, "-m", "omegaclaw.cli", "serve", "--config", str(self.config)],
            cwd=ROOT, env=self.env, stdout=self.stream, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if self.child.poll() is not None:
                raise PlaygroundError("receiver exited; inspect receiver.log")
            with (self.output / "receiver.log").open() as reader:
                reader.seek(offset)
                for line in reader:
                    try: ready = json.loads(line)
                    except ValueError: continue
                    if ready.get("status") == "ready":
                        return f"http://127.0.0.1:{ready['port']}"
            time.sleep(.05)
        raise PlaygroundError("receiver startup timed out; inspect receiver.log")

    def close(self):
        if self.child is not None:
            stop(self.child)
            self.stream.close()

    def restart(self, url):
        self.close()
        config = json.loads(self.config.read_text())
        config["port"] = int(url.rsplit(":", 1)[1])
        write_json(self.config, config)
        return self.start()


def local_plan(proof):
    calls = [
        ("proof-create", {"spec": json.dumps({"proof_id": proof, "title": "Omega playground fixture",
                                             "literal_target": "True"}), "workspace": "reference"}),
        ("proof-cycle-selected", {"proof": proof, "category": "critic-task", "workspace": "reference"}),
        ("proof-audit", {"proof": proof, "workspace": "reference"}),
        ("proof-status", {"proof": proof, "workspace": "reference"}),
        ("coordination-page", {"proof": proof, "cursor": "", "page_size": "100", "workspace": "reference"}),
    ]
    return [{"name": name, "arguments": arguments} for name, arguments in calls]


@contextmanager
def fixture_model(proof=None, *, plan=None):
    """Only the LLM endpoint is scripted; Omega, PeTTa and HTTP SDK are real."""
    calls = plan if plan is not None else local_plan(proof)
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 < size <= 4 * 1024 * 1024: raise ValueError("request size")
                request = json.loads(self.rfile.read(size))
                index = sum(message["role"] == "assistant" for message in request["messages"])
                name, arguments = calls[index]["name"], calls[index]["arguments"]
                observed.append({"tool": name, "history_messages": len(request["messages"])})
                body = json.dumps({"id": "playground", "object": "chat.completion", "created": 0,
                    "model": "playground-fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{index}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}).encode()
            except (ValueError, KeyError, IndexError):
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try: yield f"http://127.0.0.1:{server.server_port}/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def client_for(url, token):
    return AsyncClient(HTTPReceiverAdapter(url, token))


async def preflight(url, token, workspace, proof, *, live, atp_kind=None):
    async with client_for(url, token) as client:
        caps = await client.capabilities(contract_version="2.1")
        if live and (caps.receiver_mode != "production" or caps.backend_mode != "real"
                     or "formal-search" not in caps.actions):
            raise PlaygroundError("live mode requires a production receiver with formal-search; no reference fallback")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as transport:
            response = await transport.get(url.rstrip("/") + "/health")
            response.raise_for_status()
            health = response.json()
        if health.get("ready") is not True:
            raise PlaygroundError("receiver projection is not ready")
        if atp_kind and health.get("formal_provider") != atp_kind:
            raise PlaygroundError("receiver is configured with a different ATP provider kind")
        status = await client.proof_status(proof, workspace_id=workspace) if live else None
        return {"capabilities": to_dict(caps), "health": health, "status": to_dict(status) if status else None}


def driver_command(omega, petta, output, provider, turns, prompt):
    return [sys.executable, "-m", "omegaclaw.experiment", "--omega-root", str(omega),
            "--petta-root", str(petta), "--state", str(output / "experiment.json"),
            "--provider", provider, "--turns", str(turns), "--prompt", prompt,
            "--plan", str(output / "plan.json")]


def validate_driver(output, expected_kind):
    state = json.loads((output / "experiment.json").read_text())
    if state["pending"]:
        raise PlaygroundError("Omega still has pending tool intents; resume the same output")
    names = {}
    found = []
    for message in state["history"]:
        for call in message.get("calls", []): names[call["id"]] = call["name"]
        if message["role"] == "tool":
            result = json.loads(message["content"])
            kind = names.get(message["callid"])
            if result.get("error_code") or result.get("transport_status") == "error" or result.get("phase") == "failed":
                raise PlaygroundError(f"Omega tool {kind} did not succeed; inspect experiment.json")
            if kind == expected_kind:
                found.append(result)
    if not found:
        raise PlaygroundError(f"model did not execute {expected_kind}; read-only calls cannot pass this scenario")
    return found


async def observe(output, url, token, workspace, proof, *, live, receiver_id):
    async with client_for(url, token) as client:
        status = await client.proof_status(proof, workspace_id=workspace)
        page = first = await client.coordination_page(proof, workspace_id=workspace)
        items = list(page.items)
        while page.next_cursor:
            page = await client.coordination_page(proof, workspace_id=workspace, cursor=page.next_cursor)
            if (page.revision, page.journal_hash) != (first.revision, first.journal_hash):
                raise PlaygroundError("coordination pagination changed snapshot identity")
            items.extend(page.items)
        if (len(items) != page.total_items or status.revision != status.projection_watermark
                or (first.revision, first.journal_hash) != (status.revision, status.journal_hash)):
            raise PlaygroundError("coordination page is incomplete or projection is behind")
        target = await client.get_artifact(status.target_artifact, workspace_id=workspace)
        (output / "target.txt").write_bytes(target)
        operations = []
        with sqlite3.connect(output / "intents.sqlite3") as db:
            rows = db.execute("SELECT request,operation_ref FROM intents WHERE receiver_id=? AND workspace=?",
                (receiver_id, workspace)).fetchall()
        formal_count = 0
        for body, reference in rows:
            request = ActionRequest.from_dict(json.loads(body))
            if reference is None: raise PlaygroundError("tool intent was not admitted")
            ref = OperationRef.from_dict(json.loads(reference))
            handle = client.attach(ref)
            snapshot = await handle.snapshot()
            if snapshot.lifecycle != "succeeded" or snapshot.proof_id != proof:
                raise PlaygroundError("an Omega mutation did not finish successfully")
            receipt = await client.lookup_request(workspace_id=workspace, request_id=request.request_id)
            if receipt is None or receipt.operation_ref != ref:
                raise PlaygroundError("request lookup differs from the saved operation")
            if (await client.submit(request)).ref != ref:
                raise PlaygroundError("duplicate request produced another operation")
            events = [to_dict(event) async for event in handle.events()]
            if not events or events[-1]["event_type"] != "succeeded":
                raise PlaygroundError("operation lacks a terminal success event")
            if request.action.kind == "formal-search":
                formal_count += 1
                if not snapshot.result or not snapshot.result.get("commit_hash") or not snapshot.result.get("run_id"):
                    raise PlaygroundError("formal operation has no authoritative run/commit receipt")
                for key in ("request_ref", "result_ref"):
                    if not snapshot.result.get(key): raise PlaygroundError(f"formal receipt lacks {key}")
                    await client.get_artifact(ArtifactRef(snapshot.result[key]), workspace_id=workspace)
            operations.append({"request": request.to_dict(), "snapshot": to_dict(snapshot), "events": events})
        if live and formal_count != 1:
            raise PlaygroundError("expected exactly one formal-search operation")
        after = await client.proof_status(proof, workspace_id=workspace)
        if (after.revision, after.journal_hash) != (status.revision, status.journal_hash):
            raise PlaygroundError("proof changed during duplicate-request checks; inspect concurrent work")
        audit_handle = await client.proof_audit(proof, workspace_id=workspace)
        audit = await audit_handle.wait(timeout=10)
        if audit.lifecycle != "succeeded" or audit.result.get("audit_status") != "ok":
            raise PlaygroundError("proof audit failed")
        exported = await client.proof_export(proof, "default", workspace_id=workspace)
        export = await exported.wait(timeout=10)
        if export.lifecycle != "succeeded": raise PlaygroundError("proof export failed")
        bundle = await client.get_artifact(ArtifactRef(export.result["export_artifact"]), workspace_id=workspace)
        verified = check_export(bundle)
        (output / "proof-export.json").write_bytes(bundle)
        result = {"status": to_dict(status), "coordination": {**to_dict(first), "items": [to_dict(item) for item in items]},
                  "operations": operations, "audit": to_dict(audit), "export_check": verified}
        write_json(output / "observations.json", result)
        return result


def prepare_omega(source, output):
    target = output / "Omega"
    if not target.exists():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))
    install(target)
    return target


def scenario(args, output, report):
    omega_source, petta = source_roots(args)
    omega = prepare_omega(omega_source, output)
    report.check("Omega/PeTTa runtime and plugin available")
    env = os.environ.copy()
    local = args.mode == "local"
    receiver = None
    if local:
        env.update({"PERSISTENT_ATP_OMEGA_TOKEN": "playground-" + uuid.uuid4().hex,
                    "PERSISTENT_ATP_OMEGA_RECEIVER_ID": "playground-local"})
        receiver = LocalReceiver(output, env)
        try: url = receiver.start()
        except BaseException:
            receiver.close()
            raise
        proof, workspace = "play" + uuid.uuid4().hex[:12], "reference"
    else:
        url = args.receiver_url or env.get("PERSISTENT_ATP_OMEGA_URL", "http://127.0.0.1:8765")
        proof, workspace = args.proof_id, args.workspace
        if not proof: raise PlaygroundError("live mode requires --proof-id for an existing reviewed proof")
        for name in ("PERSISTENT_ATP_OMEGA_TOKEN", "PERSISTENT_ATP_OMEGA_RECEIVER_ID"):
            if not env.get(name): raise PlaygroundError(f"{name} is required")
    env.update({"PERSISTENT_ATP_OMEGA_URL": url,
                "PERSISTENT_ATP_OMEGA_INTENTS_DB": str(output / "intents.sqlite3")})
    # Observation uses exactly the same principal/intent namespace as the driver.
    identity = {"mode": args.mode, "proof_id": proof, "workspace": workspace, "receiver_url": url,
                "receiver_id": env["PERSISTENT_ATP_OMEGA_RECEIVER_ID"], "provider": args.provider,
                "claim_id": args.claim_id or f"{proof}/c-1", "max_steps": args.max_steps,
                "wall_clock_ms": args.wall_clock_ms, "turns": 5 if local else args.turns,
                "omega_source": str(omega_source), "petta_source": str(petta)}
    if args.atp_kind is not None: identity["atp_kind"] = args.atp_kind
    manifest = output / "session.json"
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        if receiver: receiver.close()
        raise PlaygroundError("output belongs to a different session; choose a fresh --output")
    write_json(manifest, identity)
    try:
        before = asyncio.run(preflight(url, env["PERSISTENT_ATP_OMEGA_TOKEN"], workspace, proof,
                                      live=not local, atp_kind=args.atp_kind))
        write_json(output / ("preflight-latest.json" if (output / "before.json").exists() else "before.json"), before)
        report.check("receiver capabilities and scope", before["capabilities"])
        report.data.update({"proof_id": proof, "workspace": workspace, "receiver_url": url})
        report.save()
        if local:
            prompt = f"Run the local playground fixture for proof {proof} in workspace reference."
            write_json(output / "plan.json", local_plan(proof))
            with fixture_model(proof) as (model_url, requests):
                env.update({"OMEGACLAW_CUSTOM_BASE_URL": model_url, "OMEGACLAW_CUSTOM_API_KEY": "local-fixture",
                            "OMEGACLAW_CUSTOM_MODEL": "playground-fixture"})
                command = driver_command(omega, petta, output, "custom", 5, prompt)
                run_process(command, env=env, log=output / "driver.log", timeout=args.timeout)
                validate_driver(output, "proof-cycle-selected")
                report.check("Omega → PeTTa → durable intent → SDK → HTTP → gate")
                observed = asyncio.run(observe(output, url, env["PERSISTENT_ATP_OMEGA_TOKEN"], workspace, proof,
                    live=False, receiver_id=env["PERSISTENT_ATP_OMEGA_RECEIVER_ID"]))
                report.check("request identity, events, audit, artifact hashes and export chain")
                receiver.restart(url)
                run_process(command, env=env, log=output / "driver.log", timeout=args.timeout)
                recovered = asyncio.run(observe(output, url, env["PERSISTENT_ATP_OMEGA_TOKEN"], workspace, proof,
                    live=False, receiver_id=env["PERSISTENT_ATP_OMEGA_RECEIVER_ID"]))
                if (observed["status"]["revision"], observed["status"]["journal_hash"]) != (
                    recovered["status"]["revision"], recovered["status"]["journal_hash"]):
                    raise PlaygroundError("restart/repeated checkpoint changed proof history")
                if len(requests) != 5: raise PlaygroundError("completed checkpoint repeated model calls")
                write_json(output / "fixture-model-calls.json", requests)
                report.check("receiver restart and completed Omega checkpoint reuse without duplicate writes")
        else:
            budget = json.dumps({"max_steps": args.max_steps, "wall_clock_ms": args.wall_clock_ms})
            plan = [{"name": "formal-search", "arguments": {"proof": proof, "claim": identity["claim_id"],
                     "budget": budget, "workspace": workspace}},
                    {"name": "proof-status", "arguments": {"proof": proof, "workspace": workspace}},
                    {"name": "proof-audit", "arguments": {"proof": proof, "workspace": workspace}}]
            plan.extend([plan[1]] * (args.turns - 3))
            write_json(output / "plan.json", plan)
            prompt = ("Execute exactly one tool call per turn, in this exact sequence, using the supplied string arguments: "
                + json.dumps(plan) + ". Operation completion is not independent Lean verification.")
            command = driver_command(omega, petta, output, args.provider, args.turns, prompt)
            run_process(command, env=env, log=output / "driver.log", timeout=args.timeout)
            validate_driver(output, "formal-search")
            report.check("live Omega formal-search tool executed")
            observed = asyncio.run(observe(output, url, env["PERSISTENT_ATP_OMEGA_TOKEN"], workspace, proof,
                live=True, receiver_id=env["PERSISTENT_ATP_OMEGA_RECEIVER_ID"]))
            report.check("formal run, commit receipt, MORK watermark, artifact hashes and export chain")
            report.check("same-request admission and persisted operation/event observation")
        report.data["formal_verified"] = observed["status"]["formal_verified"]
        report.data["revision"] = observed["status"]["revision"]
    finally:
        if receiver: receiver.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("local", "live", "components"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--omega-root", type=Path)
    parser.add_argument("--petta-root", type=Path)
    parser.add_argument("--receiver-url")
    parser.add_argument("--workspace", default="production")
    parser.add_argument("--proof-id")
    parser.add_argument("--claim-id")
    parser.add_argument("--provider", choices=("custom", "deepseek", "openai", "openrouter"), default="custom")
    parser.add_argument("--atp-kind", choices=("canonical-http", "mathematics-ai-http"),
                        help="require this provider on the receiver; selects component test suite")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--wall-clock-ms", type=int, default=10_000)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if not (3 <= args.turns <= 10 and 1 <= args.max_steps <= 10_000
            and 1 <= args.wall_clock_ms <= 600_000 and 1 <= args.timeout <= 3600):
        parser.error("invalid turns/budget/timeout; see --help and production budget limits")
    output = (args.output or ROOT / "tmp/omega-playground" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])).resolve()
    output.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (output / "playground.lock").open("a+") as owner:
        try: fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another playground owns this output directory")
        if args.mode != "live" and (output / "session.json").exists():
            parser.error("use a fresh output for a local/component run; live sessions can resume")
        report = Report(output, args.mode)
        print(f"Playground: {output}", flush=True)
        try:
            if args.mode == "components":
                write_json(output / "session.json", {"mode": "components"})
                env = os.environ.copy()
                env["MORK_REQUIRE_NATIVE"] = "1"
                script = "test-omega-maths-ai.sh" if args.atp_kind == "mathematics-ai-http" else "test-omega-production.sh"
                run_process([str(ROOT / "scripts" / script), "-v", "--basetemp",
                    str(output / "pytest"), "--junitxml", str(output / "components.xml")],
                    env=env, log=output / "components.log", timeout=args.timeout)
                report.data.update({"receiver": "native MORK/PostgreSQL production composition under test",
                                    "model": "none; synthetic HTTP ATP fixture"})
                if args.atp_kind == "mathematics-ai-http":
                    report.data.update({"model": "fixture model/search; actual companion API and real Lean replay",
                                        "independent_lean_replay_tested": True})
                report.check("production storage, dispatch, gate, projection and crash recovery (zero skips)")
            else:
                scenario(args, output, report)
            report.data["status"] = "passed"
            code = 0
        except (Exception, KeyboardInterrupt) as exc:
            report.data["status"] = "failed"
            report.data["error"] = str(exc) if isinstance(exc, PlaygroundError) else type(exc).__name__
            print(f"FAIL {report.data['error']}", file=sys.stderr, flush=True)
            code = 2
        report.save()
        print(f"Report: {output / 'report.md'}", flush=True)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
