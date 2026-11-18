"""Reject hostile v2 proposals in native MORK, then rebuild in fresh processes.

The provider evidence is scripted. This tests durable gate/projection behavior,
not native Lean or trained-model deployment acceptance.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from mork.backend import MorkSpace, MorkUnavailable

try:
    MorkSpace()
    AVAILABLE = True
except MorkUnavailable:
    AVAILABLE = False


PROBE = r'''
import json
from pathlib import Path
import sys

from commit_gate.gate import CommitGate
from commit_gate.ops import SetField
from commit_gate.store import JournalStore
from commit_gate.tests.test_artifact_refs import DiskArtifacts
from commit_gate.tests.test_formal_run_retry import RunCase
from commit_gate.tests.test_v2_replay_promotion import PromotionCase
from mathproof.formal_v2 import address
from mork.backend import MorkSpace, MorkView

mode, root = sys.argv[1], Path(sys.argv[2])
root.mkdir(exist_ok=True)
space = MorkSpace()
view = MorkView(space)
assert view.projected_revision('example') == 0

if mode == 'promotion':
    case = PromotionCase(root)
    case.view = view
    case.gate = CommitGate(view, case.journal, artifacts=case.artifacts)
    for missing in (True, False):
        atoms = sorted(space.atoms())
        body = {**case.body, 'run_id': 'example/fr-99'}
        case.assert_rejected(case.prepare(body, 'all-in-proposal', omit_hash=missing))
        assert sorted(space.atoms()) == atoms
    result = case.gate.commit(case.proposal(case.prepare(case.body, 'all-in-proposal')))
    assert result.accepted, result.rejections
    assert view.node(case.request['claim_id']).fields['status'] == 'lean-verified'
    case.journal.close()
elif mode == 'retry':
    case = RunCase(root)
    case.view = view
    case.gate = CommitGate(view, case.journal)
    case.start()
    case.stop()
    atoms = sorted(space.atoms())
    case.assert_rejected(case.ops(case.snapshot(attempt=2, used=0)))
    assert sorted(space.atoms()) == atoms
    case.accept(case.ops(case.snapshot(attempt=2, used=40)))
    case.accept(case.ops(case.snapshot(attempt=2, used=60, disposition='budget-exhausted')))
    assert view.projected_revision('example') == case.journal.head('example')[0] == 4
    case.journal.close()
else:
    journal = JournalStore(str(root/'journal.sqlite'))
    artifacts = DiskArtifacts(root/'blobs') if mode == 'rebuild-promotion' else None
    gate = CommitGate(view, journal, artifacts=artifacts)
    gate.catch_up('example')
    assert view.projected_revision('example') == journal.head('example')[0]
    if mode == 'rebuild-promotion':
        assert view.node('example/c-1').fields['status'] == 'lean-verified'
        cert = view.node('example/cert-1')
        replay = view.node('example/replay-1')
        body = json.loads(artifacts.get(replay.fields['attestation_artifact_hash']))
        assert body['certificate_hash'] == cert.fields['artifact_hash']
        assert address(artifacts.get(cert.fields['artifact_hash'])) == cert.fields['artifact_hash']
        # A further leased write must re-check the reconstructed evidence.
        from commit_gate.proposal import Proposal
        lease = journal.active_leases('example')[0]
        result = gate.commit(Proposal(
            proof_id='example', actor='coordinator', worker_class='coordinator',
            ops=(SetField('Claim', 'example/c-1', 'review_note', 'rebuilt', prior=None),),
            base_revision=journal.head('example')[0], lease_id='lease', fencing_token=lease.fencing_token,
        ))
        assert result.accepted, result.rejections
    else:
        fields = view.node('example/fr-3').fields
        assert fields['status'] == 'budget-exhausted'
        assert [h['attempt'] for h in fields['attempt_history']] == [1, 2]
        assert [h['consumed_steps'] for h in fields['attempt_history']] == [40, 60]
        assert fields['provider']['budget']['remaining_steps'] == 0
        assert fields['provider']['frontier_resume'] is False
    journal.close()
'''


@pytest.mark.skipif(not AVAILABLE, reason="MORK library unavailable")
def test_native_v2_rejection_commit_and_process_restart(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root/"src"), str(root)))}
    for mode in ("promotion", "rebuild-promotion", "retry", "rebuild-retry"):
        directory = tmp_path / ("promotion" if "promotion" in mode else "retry")
        result = subprocess.run(
            [sys.executable, "-c", PROBE, mode, str(directory)], cwd=root, env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"{mode}:\n{result.stdout}\n{result.stderr}"
