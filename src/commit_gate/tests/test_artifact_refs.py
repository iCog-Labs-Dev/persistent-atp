"""Real journal commits, artifact failures and reconstruction after restart."""
from pathlib import Path
from dataclasses import replace
import json

import pytest

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode, AddEdge
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.formal_v2 import address
from commit_gate.canon import canonical_json
from mathproof.maths_ai_http import MathsAIHTTPFormalATP
from mathproof.tests.test_maths_ai_http import proved, attestation
from unittest.mock import patch


class DiskArtifacts:
    """Test repository with a separate lifetime from adapter and journal."""
    def __init__(self, path):
        self.path = path
        path.mkdir(exist_ok=True)
    def put(self, raw):
        digest = address(raw)
        (self.path / digest).write_bytes(raw)
        return digest
    def get(self, digest):
        return (self.path / digest).read_bytes()
    def exists(self, digest):
        return (self.path / digest).exists()


def test_provider_certificate_commits_rebuilds_and_replays_after_restart(tmp_path):
    atp, request, result, run, _ = proved('source')
    artifacts = DiskArtifacts(tmp_path/'blobs')
    for raw in atp.artifacts.blobs.values():
        artifacts.put(raw)
    journal_path = str(tmp_path/'journal.sqlite')
    journal = JournalStore(journal_path)
    token = journal.acquire_lease('example', 'lease', actor='worker', worker_class='formal-atp')
    fields = {**result['certificate'], 'actor':'worker', 'environment_hash':request['environment_hash']}
    ops = [UpsertNode('FormalState', state['state_id'],
                      {k: v for k, v in state.items() if k != 'state_id'})
           for state in result['states']]
    ops.extend(UpsertNode('TacticApplication', edge['tactic_id'],
                          {k: v for k, v in edge.items()
                           if k not in {'tactic_id', 'source_state_id', 'produced_goal_ids'}})
               for edge in result['tactic_edges'])
    for index, edge in enumerate(result['tactic_edges']):
        ops.append(AddEdge('HAS_TACTIC', edge['source_state_id'], edge['tactic_id'],
                           f'example/e-tactic-{index}'))
        for child_index, child in enumerate(edge['produced_goal_ids']):
            ops.append(AddEdge('FORMAL_REQUIRES', edge['tactic_id'], child,
                               f'example/e-child-{index}-{child_index}',
                               {'child_index': child_index}))
        if not edge['produced_goal_ids']:
            ops.append(AddEdge('CLOSES_STATE', edge['tactic_id'], edge['source_state_id'],
                               f'example/e-closes-{index}'))
    ops.append(UpsertNode('Certificate', 'example/cert-1', fields))
    proposal = Proposal(proof_id='example', actor='worker', worker_class='formal-atp',
                        ops=tuple(ops),
                        base_revision=0, lease_id='lease', fencing_token=token)
    gate = CommitGate(MemoryView(), journal, artifacts=artifacts)
    outcome = gate.commit(proposal)
    assert outcome.accepted, outcome.rejections
    assert journal.head('example')[0] == 1
    journal.close()
    # Discard adapter, graph, journal connection and artifact repository handle.
    restarted_journal = JournalStore(journal_path)
    rebuilt = MemoryView()
    for event in restarted_journal.read_events_between('example',0,1):
        apply_ops(rebuilt, Proposal.from_dict(event).ops)
    assert rebuilt.node(result['root_state_id']).label == 'FormalState'
    for edge in result['tactic_edges']:
        assert rebuilt.node(edge['tactic_id']).label == 'TacticApplication'
        required = rebuilt.edges_from(edge['tactic_id'], 'FORMAL_REQUIRES')
        assert {e.dst_id for e in required} == set(edge['produced_goal_ids'])
    certificate = dict(rebuilt.node('example/cert-1').fields)
    reopened = DiskArtifacts(tmp_path/'blobs')
    binding = json.loads(reopened.get(certificate['binding_artifact_hash']))
    raw_root = binding['snapshot']['result']['root_state_id']
    assert raw_root.startswith(f"{request['run_id']}/attempt-1/fs-")
    assert raw_root != result['root_state_id']
    restarted = MathsAIHTTPFormalATP('http://localhost:8000', artifacts=reopened)
    with patch.object(restarted, '_call', return_value=attestation(request,run)):
        assert restarted.formal_replay(certificate, request['environment_hash'])['status'] == 'verified'
    restarted_journal.close()


@pytest.mark.parametrize('failure', ['missing','corrupt','unconfigured'])
def test_v2_reference_cannot_commit_without_exact_bytes(tmp_path, failure):
    artifacts = DiskArtifacts(tmp_path/'blobs')
    digest = artifacts.put(b'candidate')
    binding = artifacts.put(b'binding')
    if failure == 'missing':
        (artifacts.path/digest).unlink()
    elif failure == 'corrupt':
        (artifacts.path/digest).write_bytes(b'tampered')
    journal = JournalStore()
    token = journal.acquire_lease('p1','lease',actor='worker',worker_class='formal-atp')
    proposal = Proposal(proof_id='p1',actor='worker',worker_class='formal-atp',
        ops=(UpsertNode('Certificate','p1/cert-1',{'actor':'worker','status':'candidate',
             'artifact_hash':digest,'binding_artifact_hash':binding}),),
        base_revision=0,lease_id='lease',fencing_token=token)
    gate = CommitGate(MemoryView(),journal,artifacts=None if failure=='unconfigured' else artifacts)
    outcome = gate.commit(proposal)
    assert not outcome.accepted
    assert Reason.ARTIFACT_UNAVAILABLE in {r.reason for r in outcome.rejections}
    assert journal.head('p1')[0] == 0
    journal.close()


def test_artifact_persistence_does_not_bypass_fencing(tmp_path):
    artifacts = DiskArtifacts(tmp_path/'blobs')
    digest = artifacts.put(b'candidate')
    journal = JournalStore()
    token = journal.acquire_lease('p1','lease',actor='worker',worker_class='formal-atp')
    proposal = Proposal(proof_id='p1',actor='worker',worker_class='formal-atp',
        ops=(UpsertNode('Certificate','p1/cert-1',{'actor':'worker','status':'candidate','artifact_hash':digest}),),
        base_revision=0,lease_id='lease',fencing_token=token-1)
    outcome = CommitGate(MemoryView(),journal,artifacts=artifacts).commit(proposal)
    assert not outcome.accepted
    assert journal.head('p1')[0] == 0
    journal.close()


@pytest.mark.parametrize('outcome', ['verified','inconclusive','missing-certificate','missing-attestation','lying-attestation'])
def test_promotion_requires_verified_replay_and_available_artifacts(tmp_path, outcome):
    from commit_gate.tests.test_soundness_gates import wire, promote
    view = MemoryView(); artifacts = DiskArtifacts(tmp_path/'blobs')
    digest = artifacts.put(b'proof trace')
    from commit_gate.tests.test_soundness_gates import ENVIRONMENT_HASH
    evidence = artifacts.put(canonical_json({'status':'inconclusive' if outcome=='inconclusive' else 'rejected' if outcome=='lying-attestation' else 'verified',
        'certificate_hash':digest,'environment_hash':ENVIRONMENT_HASH}))
    wire(view,cert_fields={'artifact_hash':digest},
         replay_fields={'artifact_hash':digest,'attestation_artifact_hash':evidence,
                        'status':'inconclusive' if outcome=='inconclusive' else 'verified'})
    if outcome == 'missing-certificate': (artifacts.path/digest).unlink()
    if outcome == 'missing-attestation': (artifacts.path/evidence).unlink()
    journal = JournalStore()
    token = journal.acquire_lease('p1','lease',actor='coordinator',worker_class='coordinator')
    proposal = Proposal(proof_id='p1',actor='coordinator',worker_class='coordinator',
        ops=(promote(),),base_revision=0,lease_id='lease',fencing_token=token)
    result = CommitGate(view,journal,artifacts=artifacts).commit(proposal)
    assert result.accepted == (outcome == 'verified'), result.rejections
    if outcome.startswith('missing') or outcome=='lying-attestation':
        assert Reason.ARTIFACT_UNAVAILABLE in {r.reason for r in result.rejections}
    if outcome == 'inconclusive':
        assert Reason.PROMOTION_WITHOUT_REPLAY in {r.reason for r in result.rejections}
    journal.close()


def test_v2_certificate_cannot_be_linked_to_a_different_declaration(tmp_path):
    atp, request, result, run, _ = proved('source')
    artifacts = DiskArtifacts(tmp_path/'blobs')
    for raw in atp.artifacts.blobs.values(): artifacts.put(raw)
    view = MemoryView(); view.add_node('example/fd-2','FormalDeclaration',{})
    journal = JournalStore()
    token = journal.acquire_lease('example','lease',actor='worker',worker_class='formal-atp')
    proposal = Proposal(proof_id='example',actor='worker',worker_class='formal-atp',
        ops=(UpsertNode('Certificate','example/cert-1',{**result['certificate'],
                'actor':'worker','environment_hash':request['environment_hash']}),
             AddEdge('CERTIFIES','example/cert-1','example/fd-2','example/wrong-declaration')),
        base_revision=0,lease_id='lease',fencing_token=token)
    outcome = CommitGate(view,journal,artifacts=artifacts).commit(proposal)
    assert not outcome.accepted
    assert Reason.ARTIFACT_UNAVAILABLE in {r.reason for r in outcome.rejections}
    journal.close()
