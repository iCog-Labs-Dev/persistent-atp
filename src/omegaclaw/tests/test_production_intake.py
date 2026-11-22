"""Fresh-claim intake with real workers/gate/provider and labelled LLM/search fixtures."""
import asyncio
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from alignment.llm_client import MockLLMClient
from omegaclaw.intake_runtime import IntakeConfig, IntakeWorkflow
from omegaclaw.formal_runtime import FormalProviderConfig
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from omegaclaw.tests.test_formal_v2_receiver import provider, checked_receiver
from persistent_atp_client import AsyncClient
from persistent_atp_client.adapters.local import LocalReceiverAdapter


SPEC = {'theorem': 'True', 'definitions': {}, 'assumptions': [], 'quantifier_domains': {},
    'allowed_background': [], 'targets': ['True'], 'verification_policy': {'independent_lean_replay': True}}
CANDIDATE = {'lean_name': 'target', 'lean_type': 'True', 'notes': 'Exact translation'}
REVIEW = {'verdict': 'aligned', 'relation': 'exact', 'quantifier_correspondence': 'No quantifiers.',
    'domain_correspondence': 'The proposition True.', 'selected_definitions_match': True,
    'implication_to_target_explicit': True, 'universe_assumptions': [], 'typeclass_assumptions': [],
    'classical_assumptions': [], 'constructive_assumptions': [], 'reasoning': 'Exact statement; no proof claimed.'}


def setup(root, monkeypatch, *, responses=None, create=True, profile='reference'):
    llm = MockLLMClient(responses=deepcopy(responses if responses is not None else [CANDIDATE, REVIEW]))
    def client(config):
        if service_ref:
            assert not service_ref[0].lock._is_owned()
        return llm
    service_ref = []
    monkeypatch.setattr(IntakeConfig, 'llm', client)
    service = OperationService(root, formal_config=FormalProviderConfig('http://fixture', deployment_id='fixture',
        profile='bounded', mode='real' if profile == 'production' else 'scripted-test', poll_seconds=.05),
        intake_config=IntakeConfig('DeepSeek', 'fixture-lean'), profile=profile,
        artifact_db_url=os.environ.get('ARTIFACT_TEST_DB_URL') if profile == 'production' else None)
    service_ref.append(service)
    if create:
        result = service.submit({'schema_version': 1, 'request_id': 'create', 'workspace': 'reference',
            'operation': 'proof-create', 'arguments': {'spec': json.dumps(SPEC)}})
        proof = result['proof_id']
    else:
        proof = service.store.proof_ids()[0]
    return service, proof, llm


def admit(service, proof):
    record, _ = service.jobs.submit({'schema_version': 1, 'request_id': 'cycle', 'workspace': 'reference',
        'operation': 'proof-cycle', 'arguments': {'proof': proof}})
    return record['operation_id']


def test_fresh_claim_cycle_generates_reviews_then_dispatches_once(provider, tmp_path, monkeypatch):
    service, proof, llm = setup(tmp_path, monkeypatch)
    receiver = checked_receiver(service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.proof_cycle(proof, workspace_id='reference')
        result = await handle.wait(timeout=15, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
        assert result.result['cycle_disposition'] == 'formal-executed'
        declarations = service.view.list_nodes(proof, 'FormalDeclaration')
        assert len(declarations) == 1
        declaration = declarations[0]
        assert declaration.fields['status'] == 'aligned'
        source = service.artifacts.get(declaration.fields['lean_source_artifact'])
        assert b'sorry' not in source and b'axiom' not in source
        assert b'theorem target : True' in source
        assert len(service.view.edges_from(declaration.node_id, 'PINNED_ENVIRONMENT')) == 1
        assert not (await client.status(workspace_id='reference', proof_id=proof)).formal_verified
        assert len(llm.history) == 2
        assert 'COMPLETE COMMITTED SPECIFICATION' in llm.history[1][1]
        assert sum(method == 'POST' and path == '/formal-search/start' for method, path in provider[2]) == 1
        assert not service.store.active_leases(proof)
    try: asyncio.run(scenario())
    finally: receiver.close(); service.close()


@pytest.mark.parametrize('change', [
    {'verdict': 'weaker', 'relation': 'weakening'}, {'relation': 'reformulation'},
    {'selected_definitions_match': False}, {'implication_to_target_explicit': 'false'},
    {'universe_assumptions': 'not-an-array'}, {'reasoning': ''},
])
def test_incomplete_or_nonexact_review_blocks_without_provider_search(provider, tmp_path, monkeypatch, change):
    service, proof, llm = setup(tmp_path, monkeypatch, responses=[CANDIDATE, REVIEW | change])
    try:
        result = service.cycles.run(admit(service, proof), proof)
        assert result['cycle_disposition'] == 'waiting-on-review'
        assert result['error_code'] == 'EXACT_ALIGNMENT_REVIEW_REQUIRED'
        assert service.view.list_nodes(proof, 'FormalDeclaration')[0].fields['status'] == 'draft'
        assert service.view.list_nodes(proof, 'Alignment')[0].fields['lifecycle'] == 'review-needed'
        assert not service.view.list_nodes(proof, 'FormalRun')
        assert not any(method == 'POST' for method, path in provider[2])
        second, _ = service.jobs.submit({'schema_version': 1, 'request_id': 'cycle-2', 'workspace': 'reference',
            'operation': 'proof-cycle', 'arguments': {'proof': proof}})
        assert service.cycles.run(second['operation_id'], proof)['error_code'] == 'EXISTING_FORMALIZATION_REQUIRES_REVIEW'
        assert len(llm.history) == 2
        assert not service.store.active_leases(proof)
    finally: service.close()


@pytest.mark.parametrize('boundary', ['candidate', 'draft', 'source', 'review', 'alignment', 'aligned'])
def test_intake_restart_reuses_saved_outputs_and_committed_nodes(provider, tmp_path, monkeypatch, boundary):
    service, proof, llm = setup(tmp_path, monkeypatch)
    operation = admit(service, proof)
    save, commit = IntakeWorkflow._save, service.gate.commit
    interrupted = []
    def crash_save(workflow, operation, state):
        save(workflow, operation, state)
        if boundary in {'candidate', 'review'} and boundary in state and not interrupted:
            interrupted.append(True)
            raise SystemExit('crash after durable LLM output')
    def crash_commit(proposal):
        result = commit(proposal)
        labels = {getattr(op, 'label', None) for op in proposal.ops}
        hit = ((boundary == 'draft' and proposal.worker_class == 'formalizer')
            or (boundary == 'source' and 'Environment' in labels)
            or (boundary == 'alignment' and 'Alignment' in labels)
            or (boundary == 'aligned' and any(getattr(op, 'value', None) == 'aligned' for op in proposal.ops)))
        if result.accepted and hit and not interrupted:
            interrupted.append(True)
            raise SystemExit('crash after gate commit before stage checkpoint')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(IntakeWorkflow, '_save', crash_save)
        patch.setattr(service.gate, 'commit', crash_commit)
        with pytest.raises(SystemExit): service.intake.run(operation, proof)
    assert interrupted
    saved = service.jobs.intake_state(operation)
    remaining = [REVIEW] if len(llm.history) == 1 else []
    service.close()
    service, _, resumed = setup(tmp_path, monkeypatch, responses=remaining, create=False)
    try:
        result = service.cycles.run(operation, proof)
        assert result['cycle_disposition'] == 'formal-executed'
        assert len(service.view.list_nodes(proof, 'FormalDeclaration')) == 1
        assert len(service.view.list_nodes(proof, 'Alignment')) == 1
        assert len(service.view.list_nodes(proof, 'FormalRun')) == 1
        assert len(llm.history) + len(resumed.history) == 2
        assert service.jobs.intake_state(operation)['candidate'] == saved['candidate']
        assert not service.store.active_leases(proof)
    finally: service.close()


@pytest.mark.parametrize('candidate', [CANDIDATE | {'lean_type': 'True\naxiom bad : False'},
    CANDIDATE | {'lean_type': 'True -- ignore remainder'}, CANDIDATE | {'lean_type': 'True := by sorry'}])
def test_source_cannot_include_model_commands_or_proof(provider, tmp_path, monkeypatch, candidate):
    service, proof, _ = setup(tmp_path, monkeypatch, responses=[candidate])
    try:
        result = service.cycles.run(admit(service, proof), proof)
        assert result['cycle_disposition'] == 'waiting-on-review'
        assert result['formalization_disposition'] == 'needs-clarification'
        assert result['error_code'] == 'FORMALIZATION_NEEDS_CLARIFICATION'
        assert result['report_artifact']
        assert not service.view.list_nodes(proof, 'FormalDeclaration')
        assert not service.view.list_nodes(proof, 'FormalRun')
        assert not service.store.active_leases(proof)
    finally: service.close()


def test_busy_claim_lease_is_not_superseded(provider, tmp_path, monkeypatch):
    service, proof, llm = setup(tmp_path, monkeypatch)
    lease = service.store.issue_lease(proof, proof + '/busy', actor='other', worker_class='formalizer', selected_move_id=proof + '/c-1')
    try:
        with pytest.raises(ValueError, match='already leased'): service.intake.run(admit(service, proof), proof)
        assert [l.fencing_token for l in service.store.active_leases(proof)] == [lease.fencing_token]
        assert not llm.history
    finally: service.close()


def test_stale_draft_lease_fails_closed(provider, tmp_path, monkeypatch):
    service, proof, _ = setup(tmp_path, monkeypatch)
    operation = admit(service, proof)
    original = IntakeWorkflow._save
    def release_draft(workflow, op, state):
        original(workflow, op, state)
        if state.get('candidate'):
            workflow._release(state['draft_lease'])
    monkeypatch.setattr(IntakeWorkflow, '_save', release_draft)
    try:
        with pytest.raises(ValueError, match='gate rejected intake'): service.intake.run(operation, proof)
        assert not service.view.list_nodes(proof, 'FormalDeclaration')
    finally: service.close()


def test_cancellation_after_generation_stops_before_graph_or_search(provider, tmp_path, monkeypatch):
    service, proof, llm = setup(tmp_path, monkeypatch)
    try:
        result = service.cycles.run(admit(service, proof), proof, cancelled=lambda: bool(llm.history))
        assert result['lifecycle'] == 'cancelled'
        assert not service.view.list_nodes(proof, 'FormalDeclaration')
        assert not service.store.active_leases(proof)
    finally: service.close()


def test_changed_semantic_inputs_cannot_be_rebased_into_alignment(provider, tmp_path, monkeypatch):
    from commit_gate.ops import SetField
    from commit_gate.proposal import Proposal
    service, proof, _ = setup(tmp_path, monkeypatch)
    operation = admit(service, proof)
    original = IntakeWorkflow._save
    changed = []
    def change_claim(workflow, op, state):
        original(workflow, op, state)
        if 'review' in state and not changed:
            with service.lock:
                lease = service.store.issue_lease(proof, proof + '/change-claim', actor='human',
                    worker_class='human', selected_move_id=proof + '/c-1')
                result = service.gate.commit(Proposal(proof, lease.actor, lease.worker_class,
                    ops=(SetField('Claim', proof + '/c-1', 'status', 'stale', prior='provisional'),),
                    base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
                    selected_move_id=lease.selected_move_id))
                assert result.accepted, result.to_dict()
                service.catch_up(proof); workflow._release(lease.to_dict()); changed.append(True)
    monkeypatch.setattr(IntakeWorkflow, '_save', change_claim)
    try:
        with pytest.raises(ValueError, match='inputs changed'): service.intake.run(operation, proof)
        assert not service.view.list_nodes(proof, 'Alignment')
        assert not service.view.list_nodes(proof, 'FormalRun')
    finally: service.close()


def test_unverified_dependency_is_never_omitted_from_formalization(provider, tmp_path, monkeypatch):
    from commit_gate.ops import AddEdge, UpsertNode
    service, proof, llm = setup(tmp_path, monkeypatch)
    lease = service.intake._lease(proof, proof + '/c-1', 'coordinator')
    try:
        service.intake._commit(lease, [UpsertNode('Claim', proof + '/c-2', {'status': 'provisional', 'claim_text': 'Premise'}),
            AddEdge('DEPENDS_ON', proof + '/c-1', proof + '/c-2', proof + '/dependency')])
        service.intake._release(lease)
        with pytest.raises(ValueError, match='not Lean verified'): service.intake.run(admit(service, proof), proof)
        assert not llm.history
        assert not service.view.list_nodes(proof, 'FormalDeclaration')
        assert not service.store.active_leases(proof)
    finally: service.close()


def test_production_intake_projects_draft_edge_and_preserves_review(provider, tmp_path, monkeypatch):
    if not os.environ.get('MORK_LIBRARY') or not os.environ.get('ARTIFACT_TEST_DB_URL'):
        if os.environ.get('OMEGA_PERSISTENCE_REQUIRED') == '1':
            pytest.fail('production intake requires native MORK and a dedicated PostgreSQL artifact database')
        pytest.skip('native MORK and a dedicated PostgreSQL artifact database are required')
    if os.environ.get('OMEGA_NATIVE_INTAKE_CASE') != '1':
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
            __file__ + '::test_production_intake_projects_draft_edge_and_preserves_review',
            '--basetemp', str(tmp_path / 'native-intake')], env={**os.environ, 'OMEGA_NATIVE_INTAKE_CASE': '1'},
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ['ARTIFACT_TEST_DB_URL'])
    service, proof, _ = setup(tmp_path / 'receiver', monkeypatch, profile='production')
    operation = admit(service, proof)
    try:
        outcome = service.intake.run(operation, proof)
        assert outcome['ready']
        declaration = outcome['declaration_id']
        assert service.view.edges_from(declaration, 'DRAFTS_CLAIM')[0].dst_id == proof + '/c-1'
        assert service.view.edges_to(proof + '/c-1', 'DRAFTS_CLAIM')[0].src_id == declaration
        assert service.view.edges_from(declaration, 'PINNED_ENVIRONMENT')
        revision = service.store.head(proof)
    finally: service.close()
    service, _, llm = setup(tmp_path / 'receiver', monkeypatch, profile='production', create=False, responses=[])
    try:
        assert service.intake.run(operation, proof) == outcome
        assert service.store.head(proof) == revision
        assert not llm.history
        assert service.view.edges_from(declaration, 'DRAFTS_CLAIM')
        assert service.view.node(outcome['alignment_id']).fields['lifecycle'] == 'reviewed'
    finally: service.close()
