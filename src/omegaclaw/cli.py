"""Reference service commands and fail-closed tier prerequisite checks."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .api import serve
from .service import OperationService
from .receiver import ReferenceReceiver
from .runtime import DEFAULT_OMEGA_ROOT, OMEGA_REVISION, PETTA_REVISION, verify_checkout

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent


def load_config(path: str) -> tuple[dict, Path]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.is_file() and path in {"reference", "persistence", "production", "formal"}:
        config_path = PACKAGE_ROOT / "config" / (path + ".yaml")
    if not config_path.is_file() and path in {
            "reference", "omegclaw/config/reference.yaml",
            "omegaclaw/config/reference.yaml"}:
        config_path = PACKAGE_ROOT / "config" / "reference.yaml"
    config = json.loads(config_path.read_text())
    root = Path(config["root"])
    if not root.is_absolute():
        root = ROOT / root
    return config, root


def _omega_runtime_blockers():
    blockers = []
    path = Path(os.environ.get("OMEGA_PINNED_ROOT", str(DEFAULT_OMEGA_ROOT)))
    try:
        verify_checkout(path)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        blockers.append("Omega runtime content or revision is invalid")
    petta = os.environ.get("PETTA_PINNED_ROOT", str(ROOT / "tmp/repos/PeTTa"))
    try:
        revision = subprocess.run(["git", "-C", petta, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if not (Path(petta) / "src/main.pl").is_file() or revision.returncode or revision.stdout.strip() != PETTA_REVISION:
            blockers.append("PeTTa runtime has the wrong revision or is unavailable")
    except (OSError, subprocess.TimeoutExpired):
        blockers.append("PeTTa runtime revision probe failed")
    mode = os.environ.get("OMEGA_JANUS_MODE", "auto")
    if mode not in {"auto", "python", "swipl"}:
        return blockers + ["OMEGA_JANUS_MODE must be auto, python, or swipl"]
    python = os.environ.get("PETTA_PYTHON", sys.executable)
    python_ok = False
    if mode != "swipl":
        try:
            probe = subprocess.run([python, "-c",
                "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
                capture_output=True, text=True, timeout=10)
            version = json.loads(probe.stdout)
            if (probe.returncode or not isinstance(version, list) or len(version) != 3
                    or not all(type(part) is int and part >= 0 for part in version)):
                raise ValueError("invalid version")
            if tuple(version[:2]) < (3, 11):
                blockers.append("PeTTa Python must be >=3.11 for persistent-atp-client; set PETTA_PYTHON to a supported interpreter")
            elif mode != "swipl":
                python_ok = subprocess.run([python, "-c", "import janus_swi"],
                    capture_output=True, timeout=10).returncode == 0
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            if mode == "python" or "PETTA_PYTHON" in os.environ:
                blockers.append(f"cannot determine supported Python version for {python}")
    # Probe the actual SWI -> Python bridge, including its selected interpreter.
    # Explicit PETTA_PYTHON means the host Python mode was selected by the caller.
    use_swipl = mode == "swipl" or (mode == "auto" and not python_ok and "PETTA_PYTHON" not in os.environ)
    if use_swipl:
        try:
            probe = subprocess.run(["swipl", "-q", "-g",
                "use_module(library(janus)), py_call(sys:version_info, V), arg(1,V,Major), arg(2,V,Minor), "
                "(Major>3;Major=:=3,Minor>=11), halt"], capture_output=True, timeout=15)
            if probe.returncode:
                blockers.append("SWI-Prolog Janus cannot load Python >=3.11")
        except (OSError, subprocess.TimeoutExpired):
            blockers.append("SWI-Prolog Janus runtime probe failed")
    elif not python_ok:
        blockers.append(f"{python} cannot import janus_swi")
    if shutil.which("swipl") is None:
        blockers.append("SWI-Prolog is unavailable")
    return blockers


def _formal_config(config):
    if config.get('formal_provider'):
        from .formal_runtime import FormalProviderConfig
        return FormalProviderConfig.from_environment()
    return None


def _intake_config(config):
    if config.get('formal_provider'):
        from .intake_runtime import IntakeConfig
        return IntakeConfig.from_environment()
    return None


def doctor(profile: str) -> dict:
    blockers: list[str] = []
    if profile == "deepseek":
        load_dotenv()
    if profile in ("omega", "deepseek", "persistence", "lean", "real-atp"):
        if profile in ("omega", "deepseek"):
            blockers.extend(_omega_runtime_blockers())
            if profile == "deepseek":
                if not os.environ.get("DEEPSEEK_API_KEY"):
                    blockers.append("DEEPSEEK_API_KEY is unset; live DeepSeek inference cannot be tested")
                if os.environ.get("DEEPSEEK_MODEL", "deepseek-flash") not in ("deepseek-flash", "deepseek-v4-pro"):
                    blockers.append("DEEPSEEK_MODEL must be deepseek-flash or deepseek-v4-pro")
                if not (PACKAGE_ROOT / "omega_plugin" / "deepseek_provider.py").is_file():
                    blockers.append("DeepSeek Omega provider plugin is missing")
        if profile == "persistence":
            if not os.environ.get("ARTIFACT_TEST_DB_URL"):
                blockers.append("ARTIFACT_TEST_DB_URL is unset")
            library = os.environ.get("MORK_LIBRARY")
            if not library:
                blockers.append("MORK_LIBRARY is unset; use scripts/with-mork.sh")
            else:
                env = os.environ.copy()
                env["LD_PRELOAD"] = library + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
                try:
                    check = subprocess.run([sys.executable, "-c",
                        "from mork.backend.ffi import MorkSpace; MorkSpace()"],
                        env=env, capture_output=True, timeout=15)
                    if check.returncode:
                        blockers.append("MORK_LIBRARY cannot be loaded in a preloaded subprocess")
                except (OSError, subprocess.TimeoutExpired):
                    blockers.append("MORK native runtime probe failed")
            if not (ROOT / "mork" / "backend" / "view.py").is_file():
                blockers.append("this checkout lacks mork/backend/view.py")
            if not (ROOT / "scripts" / "with-mork.sh").is_file():
                blockers.append("this checkout lacks scripts/with-mork.sh")
        if profile == "lean" and shutil.which("lean") is None:
            blockers.append("Lean executable is unavailable")
        if profile == "lean":
            blockers.append("independent Lean replay and durable proof promotion are not wired")
        if profile == "real-atp":
            endpoint = os.environ.get("MATHS_AI_ATP_URL", "http://127.0.0.1:8000").rstrip("/")
            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                blockers.append("MATHS_AI_ATP_URL is invalid")
            else:
                try:
                    with urllib.request.urlopen(endpoint + "/health", timeout=2) as response:
                        if response.status != 200:
                            blockers.append(f"ATP API health returned HTTP {response.status}")
                except (urllib.error.URLError, TimeoutError, OSError):
                    blockers.append(f"ATP API health is unavailable at {endpoint}/health")
            blockers.append("persistent-atp formal-search is not wired to the ATP /prove API")
            blockers.append("durable backend checkpoint continuation is not configured")
    if profile == 'formal-v2':
        from .formal_runtime import FormalProviderConfig, FormalWorkflow
        try:
            config = FormalProviderConfig.from_environment()
            FormalWorkflow(None, config).deployment(FormalWorkflow(None, config).client())
        except (ValueError, OSError, RuntimeError):
            blockers.append('formal-v2 provider configuration, schema compatibility, or deployment readiness check failed')
    return {"profile": profile, "ready": not blockers, "blockers": blockers}


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="persistent-atp-omega")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--profile", default="reference", choices=("reference", "omega", "deepseek", "persistence", "lean", "real-atp", "formal-v2"))
    d.add_argument("--require", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--config", required=True)
    upload = sub.add_parser("artifact-put", help="trusted local upload of exact project input bytes")
    upload.add_argument("--config", required=True)
    upload.add_argument("--file", required=True)
    reconcile = sub.add_parser("reconcile-search", help="inspect an unsettled original search without retry or replay")
    reconcile.add_argument("--config", required=True)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--expected-observation-version", required=True, type=int)
    reconcile.add_argument("--request-id", required=True, help="stable control identity retained across transport retries")
    reconcile.add_argument("--no-wait", action="store_true", help="enqueue or reattach and return the durable job immediately")
    reconcile.add_argument("--timeout", type=float, default=60, help="maximum client observation time in seconds")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--profile", default="reference")
    smoke.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = doctor(args.profile)
        print(json.dumps(result, sort_keys=True))
        return int(args.require and not result["ready"])
    if args.command == "serve":
        config, root = load_config(args.config)
        token = os.environ.get(config["token_env"], "")
        service = OperationService(root, workspace=config["workspace"], profile=config.get("profile", "reference"), formal_config=_formal_config(config), intake_config=_intake_config(config))
        receiver = ReferenceReceiver(service)
        server = serve(service, token, config["host"], int(config["port"]), receiver=receiver)
        print(json.dumps({"status": "ready", "host": config["host"], "port": server.server_port,
                          "receiver_mode": service.receiver_mode, "contract_version": "2.0"}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            receiver.close()
            service.close()
        return 0
    if args.command == "reconcile-search":
        from persistent_atp_client import AsyncClient, SearchReconciliationRequest, OperationRef
        from persistent_atp_client.adapters.http import HTTPReceiverAdapter
        from persistent_atp_client.codecs import to_dict
        from persistent_atp_client.errors import ClientError
        config, _ = load_config(args.config)
        token = os.environ.get(config["token_env"], "")
        async def reconcile_original():
            # Use the live receiver; opening another service could dispatch
            # queued work and would bypass the host's connection locks.
            port = HTTPReceiverAdapter(f"http://{config['host']}:{int(config['port'])}", token, timeout=60)
            async with AsyncClient(port) as client:
                ref = OperationRef(config["workspace"], args.operation_id)
                return await client.attach(ref).reconcile_search(SearchReconciliationRequest(
                    args.request_id, ref, args.expected_observation_version), wait=not args.no_wait, timeout=args.timeout)
        try:
            result = asyncio.run(reconcile_original())
        except ClientError as exc:
            print(json.dumps({"error_code": exc.code, "reconciled": False, "request_id": args.request_id,
                "operation_id": args.operation_id, "job_may_continue": exc.code in {'OBSERVATION_TIMEOUT', 'TRANSIENT_UNAVAILABLE'}}))
            return 1
        print(json.dumps(to_dict(result), sort_keys=True))
        return 0
    if args.command == "artifact-put":
        config, root = load_config(args.config)
        source = Path(args.file)
        if not source.is_file() or source.stat().st_size > 16 * 1024 * 1024:
            raise SystemExit("artifact input must be a file of at most 16 MiB")
        service = OperationService(root, workspace=config["workspace"], profile=config.get("profile", "reference"), formal_config=_formal_config(config), intake_config=_intake_config(config))
        receiver = ReferenceReceiver(service)
        try:
            artifact = asyncio.run(receiver.put_artifact(source.read_bytes(), workspace_id=config["workspace"],
                media_type="application/octet-stream"))
        finally:
            receiver.close()
            service.close()
        pointer = artifact.hash
        print(json.dumps({"artifact_ref": pointer, "bytes": source.stat().st_size}, sort_keys=True))
        return 0
    if args.command == "smoke":
        if args.profile != "reference":
            print(json.dumps({"error_code": "PROFILE_UNAVAILABLE", "profile": args.profile}))
            return 1
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        workspace_root = Path(tempfile.mkdtemp(prefix="reference-", dir=output))
        service = OperationService(workspace_root)
        proof_id = "smoke" + uuid.uuid4().hex[:8]
        create = service.submit({"schema_version": 1, "request_id": "create-" + uuid.uuid4().hex,
            "workspace": "reference", "operation": "proof-create",
            "arguments": {"spec": json.dumps({"proof_id": proof_id, "literal_target": "For every n, n = n", "title": "Reference smoke"})}})
        cycle = service.submit({"schema_version": 1, "request_id": "cycle-" + uuid.uuid4().hex,
            "workspace": "reference", "operation": "proof-cycle", "arguments": {"proof": proof_id}})
        result = {"mode": "reference; scripted critic; fake ATP/replay unavailable", "workspace_root": str(workspace_root),
            "create": create, "cycle": cycle, "audit": service._audit(proof_id)}
        (workspace_root / "smoke-result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result, sort_keys=True))
        return 0 if create["phase"] == cycle["phase"] == "finished" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
