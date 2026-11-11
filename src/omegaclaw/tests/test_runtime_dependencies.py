"""Readiness must reject an interpreter unsupported by the client contract."""
import json
import subprocess

import pytest

from omegaclaw import cli


@pytest.mark.parametrize("version,supported", [
    ([3, 10, 12], False),
    ([3, 11, 0], True),
    ([3, 13, 5], True),
])
def test_doctor_checks_selected_petta_python_version(monkeypatch, version, supported):
    original = subprocess.run
    monkeypatch.setenv("PETTA_PYTHON", "controlled-petta-python")

    def probe(command, **kwargs):
        if command[0] != "controlled-petta-python":
            return original(command, **kwargs)
        if "sys.version_info" in command[2]:
            return subprocess.CompletedProcess(command, 0, json.dumps(version), "")
        assert command[2] == "import janus_swi"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", probe)
    result = cli.doctor("omega")
    version_blockers = [item for item in result["blockers"] if "Python must be >=3.11" in item]
    assert bool(version_blockers) is (not supported)
    if not supported:
        assert result["ready"] is False


def test_doctor_rejects_unreadable_python_version(monkeypatch):
    original = subprocess.run
    monkeypatch.setenv("PETTA_PYTHON", "controlled-petta-python")

    def probe(command, **kwargs):
        if command[0] != "controlled-petta-python":
            return original(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "not-a-version", "")

    monkeypatch.setattr(cli.subprocess, "run", probe)
    result = cli.doctor("omega")
    assert result["ready"] is False
    assert "cannot determine supported Python version for controlled-petta-python" in result["blockers"]


def test_real_atp_doctor_uses_the_configured_canonical_handshake(monkeypatch):
    from omegaclaw import execution
    observed = []
    monkeypatch.setenv("PERSISTENT_ATP_FORMAL_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(execution, "CanonicalExecutor", lambda endpoint, **kwargs: observed.append(endpoint))
    assert cli.doctor("real-atp")["ready"] is True
    assert observed == ["http://127.0.0.1:8000"]
