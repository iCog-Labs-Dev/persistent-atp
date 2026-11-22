"""Attest original, prior and current pinned runtime bytes during installation."""
import hashlib
import os
from pathlib import Path
import shutil

import pytest

from omegaclaw.installer import install
from omegaclaw.runtime import DEFAULT_OMEGA_ROOT, seal_runtime, verify_manifest
from omegaclaw.runtime_overlay import original_bytes, patched_bytes


@pytest.fixture
def omega_source():
    root = Path(os.environ.get('OMEGA_PINNED_ROOT', str(DEFAULT_OMEGA_ROOT)))
    if not (root / 'src/loop.metta').is_file():
        reason = 'pinned Omega checkout required for overlay byte verification'
        if os.environ.get('OMEGA_RUNTIME_REQUIRED') == '1':
            pytest.fail(reason)
        pytest.skip(reason)
    return root


def legacy_loop(original):
    current = patched_bytes('src/loop.metta', original)
    legacy = current.replace(b'          (change-state! &nextWakeAt (+ (get_time) (wakeupInterval)))\n', b'', 1)
    assert hashlib.sha256(legacy).hexdigest() == 'ded823ae7b26b938e3b6d5e5fabd248766791849bb6450bc396358a69bf24eae'
    return legacy


def test_loop_overlay_initializes_wake_state_and_upgrades_exact_previous_version(omega_source):
    original = (omega_source / 'src/loop.metta').read_bytes()
    current = patched_bytes('src/loop.metta', original)
    initialization = current.split(b'(= (initLoop)', 1)[1].split(b'(= (initKnowledge)', 1)[0]
    assert b'(change-state! &nextWakeAt (+ (get_time) (wakeupInterval)))' in initialization
    assert patched_bytes('src/loop.metta', current) == current
    assert patched_bytes('src/loop.metta', legacy_loop(original)) == current
    assert original_bytes('src/loop.metta', current) == original


@pytest.mark.parametrize('version', ['original', 'previous', 'current'])
def test_loop_overlay_rejects_unrelated_changes(omega_source, version):
    raw = (omega_source / 'src/loop.metta').read_bytes()
    if version == 'previous':
        raw = legacy_loop(raw)
    elif version == 'current':
        raw = patched_bytes('src/loop.metta', raw)
    with pytest.raises(ValueError, match='pinned runtime differs'):
        patched_bytes('src/loop.metta', raw + b'\n; unapproved change\n')


def test_installer_upgrades_previous_loop_overlay_and_remains_idempotent(omega_source, tmp_path):
    derived = tmp_path / 'Omega'
    shutil.copytree(omega_source, derived, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    install(derived)
    current = (derived / 'src/loop.metta').read_bytes()
    (derived / 'src/loop.metta').write_bytes(legacy_loop((omega_source / 'src/loop.metta').read_bytes()))
    seal_runtime(derived)
    install(derived)
    install(derived)
    assert (derived / 'src/loop.metta').read_bytes() == current
    verify_manifest(derived)
