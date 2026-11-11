"""Native CI opts into a strict no-skip gate; ordinary local runs may skip it."""
import os
import json
from pathlib import Path
import platform
import sys
import pytest


def pytest_sessionstart(session):
    destination = os.environ.get('POC_TEST_METADATA')
    if destination:
        Path(destination).write_text(json.dumps({
            'interpreter': sys.executable, 'python': platform.python_version(),
            'platform': platform.platform(),
        }, indent=2) + '\n')


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get('MORK_REQUIRE_NATIVE') != '1':
        return
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    skipped = reporter.stats.get('skipped', []) if reporter else []
    if skipped:
        reporter.write_sep('=', f'native coverage requires zero skips; found {len(skipped)}')
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
