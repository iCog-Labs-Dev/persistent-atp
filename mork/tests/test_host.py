"""Operational PoC entry point: restore/replay, all-proof readiness and restart."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from contextlib import closing

import pytest

from commit_gate import JournalStore
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from mork.backend import MorkSpace, MorkUnavailable


def native_env():
    try:
        MorkSpace()
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    return dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))


def seed(path, proof, node):
    with closing(JournalStore(str(path))) as store:
        store.append(Proposal(proof_id=proof, actor='fixture', worker_class='fixture',
                               ops=[UpsertNode('FormalState', proof + '/' + node, {})]).to_dict())


def wait_ready(path, *, pid=None, proofs=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            state = json.loads(path.read_text())
            if (state['ready'] and (pid is None or state['pid'] != pid)
                    and (proofs is None or state['proofs'] == proofs)):
                return state
        except (OSError, ValueError):
            pass
        time.sleep(.02)
    raise AssertionError('PoC host failed to become ready')


def test_real_cli_supervises_and_replays_new_proofs(tmp_path):
    env = native_env()
    journal, ready = tmp_path / 'journal.sqlite', tmp_path / 'ready.json'
    seed(journal, 'a', 's')
    log = (tmp_path / 'host.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'mork.host', '--journal', str(journal),
                                '--ready', str(ready), '--supervise'], env=env, stdout=log, stderr=log)
    try:
        first = wait_ready(ready, proofs={'a': {'journal_revision': 1, 'projected_revision': 1}})
        seed(journal, 'b', 's')
        expected = {p: {'journal_revision': 1, 'projected_revision': 1} for p in ('a', 'b')}
        wait_ready(ready, proofs=expected)
        os.kill(first['pid'], signal.SIGKILL)
        second = wait_ready(ready, pid=first['pid'], proofs=expected)
        assert second['pid'] != first['pid']
        with closing(JournalStore(str(journal))) as store:
            assert store.head('a')[0] == store.head('b')[0] == 1
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
    assert not ready.exists()


def test_real_cli_restores_bundle_and_replays_suffix(tmp_path):
    env = native_env()
    journal, ready, bundle = (tmp_path / name for name in ('journal.sqlite', 'ready.json', 'bundle'))
    seed(journal, 'a', 's')
    with closing(JournalStore(str(journal))) as store:
        store.append(Proposal(proof_id='a', actor='fixture', worker_class='fixture',
                     ops=[UpsertNode('Alignment', 'a/al-1',
                                     {'lifecycle': 'reviewed', 'verdict': 'aligned'})]).to_dict())

    command = [sys.executable, '-m', 'mork.host', '--journal', str(journal), '--ready', str(ready), '--once']
    first = subprocess.run([*command, '--snapshot-out', str(bundle)], env=env,
                            text=True, capture_output=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert not ready.exists()  # once-mode never leaves a dead host advertised
    seed(journal, 'a', 's2')
    seed(journal, 'b', 's')
    second = subprocess.run([*command, '--snapshot', str(bundle)], env=env,
                             text=True, capture_output=True, timeout=20)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)['proofs'] == {
        'a': {'journal_revision': 3, 'projected_revision': 3},
        'b': {'journal_revision': 1, 'projected_revision': 1}}
    (bundle / 'bundle.json').write_text('bad')
    bad = subprocess.run([*command, '--snapshot', str(bundle)], env=env,
                          text=True, capture_output=True, timeout=20)
    assert bad.returncode == 70 and not ready.exists()


def test_poisoned_host_exits_fatally_and_withdraws_readiness(tmp_path):
    env = native_env()
    journal, ready = tmp_path / 'journal.sqlite', tmp_path / 'ready.json'
    seed(journal, 'a', 's')
    # Inject a native poison after successful startup inside an isolated child.
    script = '''
import sys
from unittest.mock import patch
from mork.host import ProofHost, main
original = ProofHost.tick
calls = 0
def poisoned(self):
    global calls
    calls += 1
    if calls > 1:
        type(self.space).unavailable = True
    return original(self)
with patch.object(ProofHost, 'tick', poisoned):
    raise SystemExit(main(sys.argv[1:]))
'''
    result = subprocess.run([sys.executable, '-c', script, '--journal', str(journal), '--ready', str(ready)],
                             env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 70, result.stderr
    assert not ready.exists()
    assert 'restart and replay' in result.stderr
