"""Source/build drift must fail without silently rewriting the committed record."""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from mork.backend import MorkSpace, MorkUnavailable
from mork.provenance import RECORD, REPO, attest, source_pins, verify


def test_record_pins_match_launcher_and_patch():
    record = json.loads(RECORD.read_text())
    assert record['sources'] == source_pins()
    assert record['build_info']['patch_sha256'] == record['sources']['ffi_patch_sha256']
    assert record['build_info']['mork_commit'] == record['sources']['mork']
    assert record['toolchain'] == record['build_info']['rustc']


def test_record_describes_loaded_library():
    try:
        actual = attest(MorkSpace())
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    verify(json.loads(RECORD.read_text()), actual)
    loaded = [line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
              if 'libmork_ffi.so' in line]
    assert actual['resolved_path'] in loaded


@pytest.mark.parametrize('field,value', [('mork_commit', 'wrong'), ('rustc', 'wrong'),
                                         ('patch_sha256', 'wrong')])
def test_build_drift_is_rejected(field, value):
    record = json.loads(RECORD.read_text())
    actual = {**record['reference_artifact'], 'build_info': copy.deepcopy(record['build_info'])}
    actual['build_info'][field] = value
    with pytest.raises(ValueError):
        verify(record, actual)


def test_artifact_drift_is_rejected_at_reference_path():
    record = json.loads(RECORD.read_text())
    actual = {**record['reference_artifact'], 'sha256': 'wrong', 'build_info': record['build_info']}
    with pytest.raises(ValueError, match='SHA256'):
        verify(record, actual)


def test_launcher_verifies_without_modifying_record():
    try:
        space = MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    before = RECORD.read_bytes()
    result = subprocess.run([str(REPO / 'scripts/with-mork.sh'), '--', '/bin/true'],
                            env=dict(os.environ, MORK_LIBRARY=space.library_path),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert RECORD.read_bytes() == before


def test_build_configuration_drift_is_rejected():
    record = json.loads(RECORD.read_text())
    actual = {**record['reference_artifact'], 'build_info': record['build_info']}
    changed = copy.deepcopy(record)
    changed['build_configuration']['disabled_optional_features'] = []
    with pytest.raises(ValueError, match='feature/build'):
        verify(changed, actual)
