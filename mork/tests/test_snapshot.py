"""Native persistence and rejection tests in isolated host processes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mork.backend import MorkSpace, MorkUnavailable


@pytest.mark.parametrize('scenario', [
    'roundtrip', 'replay', 'corrupt', 'foreign', 'rewound', 'metadata', 'parity', 'invalid', 'concurrent', 'panic', 'dependencies',
])
def test_snapshot_scenario(scenario, tmp_path):
    try:
        MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    child = subprocess.run([sys.executable, str(Path(__file__).with_name('snapshot_worker.py')),
                            scenario, str(tmp_path)], env=env, capture_output=True, text=True,
                           timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
