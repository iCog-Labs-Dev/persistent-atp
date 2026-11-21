"""Exercise the pinned startup script's actual environment/agent boundary."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import uuid

import pytest

from omegaclaw.api import serve
from omegaclaw.container_entrypoint import BRIDGE_VARIABLES, patch_entrypoint
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.runtime import DEFAULT_OMEGA_ROOT
from omegaclaw.service import OperationService


@pytest.fixture
def upstream_script():
    root = Path(os.environ.get("OMEGA_PINNED_ROOT", str(DEFAULT_OMEGA_ROOT)))
    if not (root / "entrypoint.sh").is_file():
        pytest.skip("pinned Omega checkout is required for entrypoint integration")
    text = (root / "entrypoint.sh").read_text()
    patch_entrypoint(text)  # Reject a source other than the inspected revision.
    return text


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def test_entrypoint_patch_rejects_unexpected_source(upstream_script):
    patched = patch_entrypoint(upstream_script)
    assert patch_entrypoint(patched) == patched
    with pytest.raises(ValueError): patch_entrypoint(upstream_script + "\necho unexpected\n")


def test_entrypoint_preserves_bridge_configuration_through_agent_startup(upstream_script, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bin").mkdir()
    marker = tmp_path / "must-not-execute"
    token = f"mock credential 'quotes' $(touch {shlex.quote(str(marker))})"
    state = runtime / "state with spaces/workflow.json"
    service = OperationService(tmp_path / "receiver")
    receiver = ReferenceReceiver(service)
    server = serve(service, token, receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    expected = {
        "PERSISTENT_ATP_OMEGA_URL": f"http://127.0.0.1:{server.server_port}",
        "PERSISTENT_ATP_OMEGA_TOKEN": token,
        "PERSISTENT_ATP_OMEGA_WORKSPACE": "reference",
        "PERSISTENT_ATP_OMEGA_STATE": str(state),
        "PERSISTENT_ATP_OMEGA_MAX_CYCLES": "7",
        "PERSISTENT_ATP_OMEGA_RETRY_LIMIT": "3",
        "PERSISTENT_ATP_OMEGA_RETRY_BASE_SECONDS": "0.75",
        "OMEGA_persistentAtpEnabled": "enabled",
        "OMEGA_persistentAtpWorkspace": "reference",
    }
    (runtime / "expected.json").write_text(json.dumps(expected))
    probe = runtime / "probe.py"
    probe.write_text('''import json, os
from pathlib import Path
from omegaclaw.frameworks.omega import OmegaAdapter
expected = json.loads(Path("expected.json").read_text())
result = {"configuration_matches": all(os.environ.get(k) == v for k, v in expected.items()),
          "unrelated_present": "SHOULD_NOT_REACH_AGENT" in os.environ}
try:
    adapter = OmegaAdapter.from_environment()
    capabilities = adapter.startup()
    adapter.state.update(lambda data: data.update(entrypoint_probe=True))
    result.update(configured=True, receiver_mode=capabilities["receiver_mode"],
                  state_saved=adapter.state.path.is_file(), cycles=adapter.max_cycles_per_proof,
                  retry_limit=adapter.retry_limit, retry_base=adapter.retry_base_seconds)
    adapter.close()
except ValueError:
    result.update(configured=False)
print(json.dumps(result))
''')
    executable(runtime / "run.sh", f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(probe))}\n")
    # Substitute system preparation and the account switch in this host test;
    # the separate OCI test below exercises the real nobody account switch.
    executable(runtime / "bin/su", '''#!/bin/bash
if [[ "${1:-}" == "www-data" || -n "${MEMORY_PORTABILITY_OPERATION:-}" ]]; then exit 0; fi
while [[ "${1:-}" != "-c" ]]; do shift; done
shift
exec /bin/sh -c "$1"
''')
    env = {**os.environ, **expected, "PATH": str(runtime / "bin") + os.pathsep + os.environ["PATH"],
           "OMEGA_DIR": str(runtime), "HOME": str(runtime), "IMPORT_KB_ON_START": "0",
           "SHOULD_NOT_REACH_AGENT": "unrelated-test-secret"}
    env.pop("MEMORY_IMPORT_FILE", None)
    try:
        for script, should_work in ((upstream_script, False), (patch_entrypoint(upstream_script), True)):
            # Remap only the fixed container working directory. The complete
            # pinned script, including its scrub and final exec, is executed.
            path = runtime / "entrypoint.sh"
            path.write_text(script.replace("cd /PeTTa\n", "cd " + shlex.quote(str(runtime)) + "\n"))
            result = subprocess.run(["/bin/bash", str(path)], env=env, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report["configured"] is should_work
            assert report["configuration_matches"] is should_work
            assert not report["unrelated_present"] and not marker.exists()
            if should_work:
                assert report["receiver_mode"] == "reference-scripted" and report["state_saved"]
                assert (report["cycles"], report["retry_limit"], report["retry_base"]) == (7, 3, 0.75)
    finally:
        server.shutdown()
        server.server_close()
        receiver.close()


def test_official_omega_entrypoint_handoff_runs_as_nobody(upstream_script, tmp_path):
    image = os.environ.get("OMEGA_ENTRYPOINT_TEST_IMAGE")
    if not image:
        pytest.skip("set OMEGA_ENTRYPOINT_TEST_IMAGE to the digest-pinned official singularitynet/omega image")
    assert re.fullmatch(r"singularitynet/omega@sha256:[0-9a-f]{64}", image), "use the official Omega image"
    runtime = tmp_path / "container"
    runtime.mkdir(mode=0o777)
    runtime.chmod(0o777)
    (runtime / "bin").mkdir()
    (runtime / "entrypoint.sh").write_text(patch_entrypoint(upstream_script))
    executable(runtime / "bin/su", '''#!/bin/bash
if [[ "${1:-}" == "www-data" || -n "${MEMORY_PORTABILITY_OPERATION:-}" ]]; then exit 0; fi
exec /usr/bin/su "$@"
''')
    executable(runtime / "run.sh", '''#!/bin/sh
set -eu
mkdir -p "$(dirname "$PERSISTENT_ATP_OMEGA_STATE")"
chmod 0777 "$(dirname "$PERSISTENT_ATP_OMEGA_STATE")"
printf '{}' > "$PERSISTENT_ATP_OMEGA_STATE"
printf 'ATP_TEST_UID=%s\000' "$(id -u)"
exec /usr/bin/env -0
''')
    expected = {key: "test-value" for key in BRIDGE_VARIABLES}
    expected.update(PERSISTENT_ATP_OMEGA_TOKEN="mock 'token' with spaces $(touch /PeTTa/must-not-execute)",
                    PERSISTENT_ATP_OMEGA_STATE="/PeTTa/state with spaces/cache.json")
    name = "omega-entrypoint-test-" + uuid.uuid4().hex
    command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--user=0:0", "--name", name,
               "--mount", f"type=bind,src={runtime},dst=/PeTTa", "--entrypoint", "/bin/bash"]
    environment = {**expected, "HOME": "/tmp", "OMEGA_DIR": "/PeTTa", "IMPORT_KB_ON_START": "0",
                   "PATH": "/PeTTa/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "SHOULD_NOT_REACH_AGENT": "unrelated-test-secret"}
    for key, value in environment.items(): command.extend(["--env", f"{key}={value}"])
    command.extend([image, "/PeTTa/entrypoint.sh"])
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        values = dict(item.split("=", 1) for item in result.stdout.decode().split("\0") if "=" in item)
        assert values["ATP_TEST_UID"] != "0"
        assert all(values.get(key) == value for key, value in expected.items())
        assert "SHOULD_NOT_REACH_AGENT" not in values
        assert not (runtime / "must-not-execute").exists()
        assert (runtime / "state with spaces/cache.json").is_file()
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=10)
