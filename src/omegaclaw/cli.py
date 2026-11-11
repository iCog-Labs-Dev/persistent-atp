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
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .api import serve
from .service import OperationService
from .receiver import ReferenceReceiver
from .factory import build_service

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent


def load_config(path: str) -> tuple[dict, Path]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.is_file() and path in {
            "reference", "omegclaw/config/reference.yaml",
            "omegaclaw/config/reference.yaml"}:
        config_path = PACKAGE_ROOT / "config" / "reference.yaml"
    config = json.loads(config_path.read_text())
    root = Path(config["root"])
    if not root.is_absolute():
        root = ROOT / root
    return config, root


def doctor(profile: str, provider_kind="canonical-http") -> dict:
    blockers: list[str] = []
    if profile == "deepseek":
        load_dotenv()
    if profile in ("omega", "deepseek", "persistence", "lean", "real-atp"):
        if profile in ("omega", "deepseek"):
            path = os.environ.get("OMEGA_PINNED_ROOT", str(ROOT / "tmp" / "repos" / "Omega"))
            if not (Path(path) / "src" / "plugin.metta").is_file():
                blockers.append(f"OMEGA_PINNED_ROOT must name the pinned Omega checkout (default: {ROOT / 'tmp' / 'repos' / 'Omega'})")
            elif subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() != "63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5":
                blockers.append("Omega checkout has the wrong revision")
            petta = os.environ.get("PETTA_PINNED_ROOT", str(ROOT / "tmp" / "repos" / "PeTTa"))
            if not (Path(petta) / "src" / "main.pl").is_file():
                blockers.append(f"PETTA_PINNED_ROOT must name the pinned PeTTa v1.0.4 checkout (default: {ROOT / 'tmp' / 'repos' / 'PeTTa'})")
            elif subprocess.run(["git", "-C", petta, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() != "7037f4c2ad378c52fc328004fe216d5118b674f0":
                blockers.append("PeTTa checkout has the wrong revision")
            if shutil.which("swipl") is None:
                blockers.append("SWI-Prolog is unavailable")
            petta_python = os.environ.get("PETTA_PYTHON", sys.executable)
            try:
                probe = subprocess.run([petta_python, "-c",
                    "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
                    capture_output=True, text=True)
                version = json.loads(probe.stdout)
                if (probe.returncode or not isinstance(version, list) or len(version) != 3
                        or not all(type(part) is int and part >= 0 for part in version)):
                    raise ValueError("invalid Python version probe")
                if tuple(version[:2]) < (3, 11):
                    blockers.append("PeTTa Python must be >=3.11 for persistent-atp-client; set PETTA_PYTHON to a supported interpreter")
            except (OSError, ValueError, TypeError):
                blockers.append(f"cannot determine supported Python version for {petta_python}")
            try:
                janus_ready = subprocess.run([petta_python, "-c", "import janus_swi"], capture_output=True).returncode == 0
            except OSError:
                janus_ready = False
            if not janus_ready:
                blockers.append(f"{petta_python} cannot import janus_swi")
            if profile == "deepseek":
                if not os.environ.get("DEEPSEEK_API_KEY"):
                    blockers.append("DEEPSEEK_API_KEY is unset; live DeepSeek inference cannot be tested")
                if os.environ.get("DEEPSEEK_MODEL", "deepseek-flash") not in ("deepseek-flash", "deepseek-v4-pro"):
                    blockers.append("DEEPSEEK_MODEL must be deepseek-flash or deepseek-v4-pro")
                if not (PACKAGE_ROOT / "omega_plugin" / "deepseek_provider.py").is_file():
                    blockers.append("DeepSeek Omega provider plugin is missing")
        if profile == "persistence":
            if not (os.environ.get("ARTIFACT_DB_URL") or os.environ.get("ARTIFACT_TEST_DB_URL")):
                blockers.append("ARTIFACT_DB_URL or ARTIFACT_TEST_DB_URL is required")
            if not os.environ.get("MORK_LIBRARY"):
                blockers.append("MORK_LIBRARY is unset; run through scripts/with-mork.sh")
            if not (ROOT / "mork" / "backend" / "view.py").is_file():
                blockers.append("this checkout lacks mork/backend/view.py")
            if not (ROOT / "scripts" / "with-mork.sh").is_file():
                blockers.append("this checkout lacks scripts/with-mork.sh")
        if profile == "lean" and shutil.which("lean") is None:
            blockers.append("Lean executable is unavailable")
        if profile == "lean":
            blockers.append("independent Lean replay and durable proof promotion are not wired")
        if profile == "real-atp":
            endpoint = os.environ.get("PERSISTENT_ATP_FORMAL_URL")
            if not endpoint:
                blockers.append("PERSISTENT_ATP_FORMAL_URL is unset")
            else:
                try:
                    from .execution import CanonicalExecutor, MathsAIExecutor
                    executor = MathsAIExecutor if provider_kind == "mathematics-ai-http" else CanonicalExecutor
                    executor(endpoint, token=os.environ.get("PERSISTENT_ATP_FORMAL_TOKEN"), timeout=2)
                except Exception as exc:
                    blockers.append(f"canonical ATP capability handshake failed: {type(exc).__name__}")
    return {"profile": profile, "ready": not blockers, "blockers": blockers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="persistent-atp-omega")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--profile", default="reference", choices=("reference", "omega", "deepseek", "persistence", "lean", "real-atp"))
    d.add_argument("--require", action="store_true")
    d.add_argument("--provider-kind", choices=("canonical-http", "mathematics-ai-http"), default="canonical-http")
    s = sub.add_parser("serve")
    s.add_argument("--config", required=True)
    upload = sub.add_parser("artifact-put", help="trusted local upload of exact project input bytes")
    upload.add_argument("--config", required=True)
    upload.add_argument("--file", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--profile", default="reference")
    smoke.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = doctor(args.profile, args.provider_kind)
        print(json.dumps(result, sort_keys=True))
        return int(args.require and not result["ready"])
    if args.command == "serve":
        config, root = load_config(args.config)
        token = os.environ.get(config["token_env"], "")
        if len(token) < 16:
            raise SystemExit("receiver token must contain at least 16 characters")
        service = build_service(config, root)
        receiver = server = None
        try:
            receiver = ReferenceReceiver(service)
            server = serve(service, token, config["host"], int(config["port"]), receiver=receiver)
            print(json.dumps({"status": "ready", "host": config["host"], "port": server.server_port,
                              "receiver_mode": receiver.component_mode, "contract_version": "2.1"}), flush=True)
            server.serve_forever()
        finally:
            if server is not None: server.server_close()
            if receiver is not None: receiver.close()
            if getattr(service, "production", False):
                service.close()
        return 0
    if args.command == "artifact-put":
        config, root = load_config(args.config)
        source = Path(args.file)
        if not source.is_file() or source.stat().st_size > 16 * 1024 * 1024:
            raise SystemExit("artifact input must be a file of at most 16 MiB")
        if config.get("mode") == "production":
            from persistent_atp_client import AsyncClient
            from persistent_atp_client.adapters.http import HTTPReceiverAdapter
            async def upload():
                endpoint = f"http://{config['host']}:{config['port']}"
                async with AsyncClient(HTTPReceiverAdapter(endpoint, os.environ[config["token_env"]])) as client:
                    return await client.put_artifact(source.read_bytes(), workspace_id=config["workspace"],
                                                     media_type="application/octet-stream")
            artifact = asyncio.run(upload())
        else:
            service = build_service(config, root)
            receiver = ReferenceReceiver(service)
            try:
                artifact = asyncio.run(receiver.put_artifact(source.read_bytes(), workspace_id=config["workspace"],
                    media_type="application/octet-stream"))
            finally:
                receiver.close()
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
