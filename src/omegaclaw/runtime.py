"""Reproducible Omega runtime selected by the bridge design."""
from pathlib import Path
import hashlib
import json
import subprocess

OMEGA_REVISION = "ba185cd359d9fe231b904110b988af39fafa5014"
PETTA_REVISION = "7037f4c2ad378c52fc328004fe216d5118b674f0"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OMEGA_ROOT = ROOT / "tmp" / "repos" / "Omega-ba185cd"
MANIFEST = "persistent_atp_runtime.json"
CORE_PATHS = ("src", "providers", "profile", "Autotests/mock", "proxy/nginx.sh",
              "lib_omega.metta", "lib_nal.metta", "lib_pln.metta")


def verify_checkout(root):
    """Attest the build source before generating a metadata-free runtime."""
    root = Path(root)
    if not (root / ".git").exists():
        verify_manifest(root)
        return
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    if result.stdout.strip() != OMEGA_REVISION:
        raise ValueError("Omega checkout has the wrong revision")
    result = subprocess.run(["git", "-C", str(root), "diff", "--no-ext-diff", "--quiet", "HEAD", "--", *CORE_PATHS])
    if result.returncode:
        raise ValueError("pinned Omega core files have local changes")


def seal_runtime(root):
    """Record the verified source plus installed overlay for an immutable image.

    The image/build is the trust anchor. This manifest is not a signature or
    permission to accept arbitrary model-supplied runtime content.
    """
    root = Path(root)
    paths = [root / "run.metta", root / "entrypoint.sh", root / "persistent_atp_pinned_import.pl", *root.glob("lib*.metta")]
    if (root / "version").is_file():
        paths.append(root / "version")
    # The Test provider and test channel import these at runtime. Leaving the
    # base image's older mocks would mix incompatible tool-call protocols.
    for name in ("src", "providers", "plugins", "profile", "config", "channels", "Autotests/mock", "proxy"):
        paths.extend(path for path in (root / name).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    files = {}
    for path in sorted(set(paths)):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("runtime source escapes its root")
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / MANIFEST).write_text(json.dumps({"revision": OMEGA_REVISION, "files": files}, sort_keys=True))


def verify_manifest(root):
    root = Path(root).resolve()
    manifest = json.loads((root / MANIFEST).read_text())
    files = manifest.get("files", {})
    required = {"src/loop.metta", "src/plugin.py", "run.metta", "entrypoint.sh", "persistent_atp_pinned_import.pl"}
    if manifest.get("revision") != OMEGA_REVISION or not isinstance(files, dict) or not required <= files.keys():
        raise ValueError("invalid Omega runtime manifest")
    for name, expected in files.items():
        path = root / name
        if not path.resolve().is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Omega runtime content differs from its build manifest")
