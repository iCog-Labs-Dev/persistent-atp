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


def test_doctor_accepts_sealed_runtime_and_swipl_bridge(monkeypatch, tmp_path):
    from omegaclaw.runtime import seal_runtime, PETTA_REVISION
    for name in ('src/loop.metta', 'src/plugin.py', 'run.metta', 'entrypoint.sh', 'persistent_atp_pinned_import.pl'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('verified fixture')
    seal_runtime(tmp_path)
    petta = tmp_path / 'petta'
    (petta / 'src').mkdir(parents=True)
    (petta / 'src/main.pl').write_text('% fixture')
    monkeypatch.setenv('OMEGA_PINNED_ROOT', str(tmp_path))
    monkeypatch.setenv('PETTA_PINNED_ROOT', str(petta))
    monkeypatch.setenv('OMEGA_JANUS_MODE', 'swipl')
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/usr/bin/swipl')
    def probe(command, **kwargs):
        if command[0] == 'git':
            return subprocess.CompletedProcess(command, 0, PETTA_REVISION, '')
        if command[0] == 'swipl':
            assert 'arg(1,V,Major)' in command[-1]
            return subprocess.CompletedProcess(command, 0, '', '')
        return subprocess.CompletedProcess(command, 0, '[3,11,2]', '')
    monkeypatch.setattr(cli.subprocess, 'run', probe)
    assert cli.doctor('omega')['ready']
    (tmp_path / 'src/plugin.py').write_text('tampered')
    assert not cli.doctor('omega')['ready']


def test_persistence_doctor_uses_preloaded_mork_library(monkeypatch):
    monkeypatch.setenv('ARTIFACT_TEST_DB_URL', 'postgresql://test-only')
    monkeypatch.setenv('MORK_LIBRARY', '/test/libmork_ffi.so')
    monkeypatch.delenv('MORK_LIBRARY_PATH', raising=False)
    def probe(command, **kwargs):
        assert kwargs['env']['LD_PRELOAD'].startswith('/test/libmork_ffi.so')
        assert 'MorkSpace()' in command[-1]
        return subprocess.CompletedProcess(command, 1, '', '')
    monkeypatch.setattr(cli.subprocess, 'run', probe)
    result = cli.doctor('persistence')
    assert not result['ready']
    assert any('cannot be loaded' in message for message in result['blockers'])
