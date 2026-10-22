"""O01/O02: real PeTTa process, pinned Omega plugin and HTTP gate path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from omegaclaw.api import serve
from omegaclaw.service import OperationService

ROOT = Path(__file__).resolve().parents[2]


def test_o01_o02_real_omega_petta_process(tmp_path):
    omega_source = Path(os.environ.get("OMEGA_PINNED_ROOT", "/tmp/persistent-atp-omega-pinned"))
    petta_source = Path(os.environ.get("PETTA_PINNED_ROOT", "/tmp/persistent-atp-petta-v1.0.4"))
    if not (omega_source / "src/plugin.py").is_file() or not (petta_source / "src/main.pl").is_file():
        pytest.skip("pinned Omega/PeTTa checkouts unavailable; omega tier requires them")
    process_python = os.environ.get("PETTA_PYTHON", "/usr/bin/python3")
    if subprocess.run([process_python, "-c", "import janus_swi"], capture_output=True).returncode != 0:
        pytest.skip("janus_swi unavailable; omega tier requires it")
    petta_root = tmp_path / "petta"
    petta_root.mkdir()
    for folder in ("src", "lib", "python"):
        shutil.copytree(petta_source / folder, petta_root / folder)
    omega_root = petta_root / "repos" / "Omega"
    omega_root.parent.mkdir()
    shutil.copytree(omega_source, omega_root, ignore=shutil.ignore_patterns(".git"))
    plugin_dir = omega_root / "plugins" / "persistent_atp"
    shutil.copytree(ROOT / "omegclaw" / "omega-plugin", plugin_dir)
    catalogue = omega_root / "config" / "plugins.yaml"
    original_catalogue = catalogue.read_text()
    with catalogue.open("a") as stream:
        stream.write(f'\n- name: persistent_atp\n  loader: metta\n  location: "{plugin_dir}"\n')
    assert original_catalogue in catalogue.read_text()
    assert subprocess.run(["git", "-C", str(omega_source), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip() == "63a78265be9b8efc6e47cc39f3b4bc7a13e06ed5"
    service = OperationService(tmp_path / "service")
    server = serve(service, "omega-process-test-token", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(petta_root / "python"), "PETTA_TEST_ROOT": str(petta_root),
        "OMEGA_TEST_ROOT": str(omega_root), "PERSISTENT_ATP_OMEGA_TOKEN": "omega-process-test-token",
        "PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}"})
    try:
        completed = subprocess.run([process_python, str(ROOT / "tests/omegaclaw_runtime/process_call.py")],
            cwd=petta_root, env=env, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr[-6000:] + completed.stdout[-6000:]
        result_line = next(line for line in completed.stdout.splitlines() if line.startswith("PROCESS_RESULT="))
        result = json.loads(result_line.split("=", 1)[1])
        assert result["registered_skills"] == 9
        assert result["next_model_request_saw_digest"] is True
        assert service.store.head("omegaproc")[0] == 1
        assert service.store.verify_chain("omegaproc") == 1
        assert service.artifacts.exists(result["create"]["artifact_pointers"][0])
    finally:
        server.shutdown()
        server.server_close()
