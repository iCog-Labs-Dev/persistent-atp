"""New adapter records pass through the gate; certificates select independently."""
from contextlib import closing
import copy
import pytest

from commit_gate import CommitGate, JournalStore
from commit_gate.ops import UpsertNode, AddEdge, SetField
from commit_gate.reasons import Reason
from commit_gate.proposal import Proposal
from mathproof.formal_atp import build_result
from mathproof.projection import result_operations
from mathproof.witness import build_witness
from mork.backend import MorkSpace, MorkView, MorkUnavailable


def test_certificate_isolation_and_versioned_state_reuse():
    try:
        view = MorkView(MorkSpace())
    except MorkUnavailable:
        pytest.skip('MORK library unavailable')
    proof = 'contract-ingestion'
    env = 'sha256:' + '11' * 32
    with closing(JournalStore()) as store:
        gate = CommitGate(view, store)
        token = store.acquire_lease(proof, 'setup', actor='setup', worker_class='coordinator')
        setup = Proposal(proof_id=proof, actor='setup', worker_class='coordinator', base_revision=0,
                         lease_id='setup', fencing_token=token, ops=[
            UpsertNode('FormalDeclaration', f'{proof}/fd-1', {}),
            UpsertNode('Environment', f'{proof}/env-1', {'environment_hash': env}),
            AddEdge('PINNED_ENVIRONMENT', f'{proof}/fd-1', f'{proof}/env-1', f'{proof}/pin')])
        result = gate.commit(setup)
        assert result.accepted, result.rejections
        store.release_lease(proof, 'setup', fencing_token=token)
        token = store.acquire_lease(proof, 'atp', actor='atp', worker_class='formal-atp')
        request = {'proof_id': proof, 'run_id': f'{proof}/fr-1', 'environment_hash': env,
                   'formal_declaration_id': f'{proof}/fd-1', 'goal_text': 'P'}
        report = build_result('proved-pending-replay', request)
        proposal = Proposal(proof_id=proof, actor='atp', worker_class='formal-atp', base_revision=1,
                            lease_id='atp', fencing_token=token,
                            ops=result_operations(report, environment_id=f'{proof}/env-1', actor='atp'))
        result = gate.commit(proposal)
        assert result.accepted, result.rejections
        certificate = report['certificate']['witness']['certificate_id']
        assert view.get_certificate_witness(certificate).closure
        for label, identity, field, value in [
            ('FormalState', f'{proof}/fs-1', 'serialization_version', 2),
            ('Certificate', certificate, 'witness', {}),
        ]:
            previous = view.node(identity).fields[field]
            rejected = gate.commit(Proposal(proof_id=proof, actor='atp', worker_class='formal-atp',
                base_revision=store.head(proof)[0], lease_id='atp', fencing_token=token,
                ops=[SetField(label, identity, field, value, previous)]))
            assert not rejected.accepted
            assert Reason.IMMUTABLE_FIELD_OVERWRITE in {r.reason for r in rejected.rejections}
        assert view.get_exact_state(f'{proof}/fs-1', env) is not None
        assert view.get_exact_state(f'{proof}/fs-1', 'sha256:' + '99' * 32) is None
        # A second certificate selects another accepted tactic at the same state.
        other = copy.deepcopy(report)
        alternative = {**other['tactic_edges'][0], 'tactic_id': f'{proof}/ta-2'}
        other['tactic_edges'].append(alternative)
        other['certificate']['artifact_hash'] = 'sha256:' + '22' * 32
        other_id = f'{proof}/cert-2'
        record = build_witness(other, other_id, {f'{proof}/fs-1': f'{proof}/ta-2'})
        # Independent certificate projection does not mutate shared tactic flags.
        view.add_node(alternative['tactic_id'], 'TacticApplication', alternative)
        view.add_edge('HAS_TACTIC', f'{proof}/fs-1', alternative['tactic_id'], f'{proof}/alt')
        view.add_node(other_id, 'Certificate', {**other['certificate'], 'witness': record})
        assert view.get_certificate_witness(other_id).closure
        assert view.get_certificate_witness(other_id).selected_tactic.dst_id == f'{proof}/ta-2'
        assert view.get_certificate_witness(certificate).closure
        assert view.get_certificate_witness(certificate).selected_tactic.dst_id == f'{proof}/ta-1'
        assert view.node(certificate).fields['witness']['selected_tactics'] != record['selected_tactics']
        view.add_node(f'{proof}/legacy', 'FormalState', {'exact_state_hash': report['states'][0]['exact_state_hash']})
        assert view.node(f'{proof}/legacy') is not None
        assert view.get_exact_state(f'{proof}/legacy') is None
