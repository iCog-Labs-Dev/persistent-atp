"""Intake metadata regressions with no optional provider, Lean, or HTTP server.

Generation, alignment responses and the search handoff are labelled fixtures;
the workflow, source construction, commit gate and SQLite recovery are real.
"""
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest

from alignment.llm_client import MockLLMClient
from omegaclaw.formal_runtime import FormalProviderConfig
from omegaclaw.intake_runtime import IntakeConfig, IntakeWorkflow
from omegaclaw.service import OperationService


SPEC = {'theorem': 'True', 'definitions': {}, 'assumptions': [], 'quantifier_domains': {},
    'allowed_background': [], 'targets': ['True'], 'verification_policy': {'independent_lean_replay': True}}
REVIEW = {'verdict': 'aligned', 'relation': 'exact', 'quantifier_correspondence': 'No quantifiers.',
    'domain_correspondence': 'The proposition True.', 'selected_definitions_match': True,
    'implication_to_target_explicit': True, 'universe_assumptions': [], 'typeclass_assumptions': [],
    'classical_assumptions': [], 'constructive_assumptions': [], 'reasoning': 'Exact statement fixture.'}


def receiver(root, monkeypatch, responses, *, create=True):
    llm = MockLLMClient(responses=deepcopy(responses))
    monkeypatch.setattr(IntakeConfig, 'llm', lambda _config: llm)
    service = OperationService(root,
        formal_config=FormalProviderConfig('http://fixture', deployment_id='fixture', mode='scripted-test'),
        intake_config=IntakeConfig('DeepSeek', 'fixture-lean'))
    monkeypatch.setattr(service.formal, 'client', lambda: None)
    monkeypatch.setattr(service.formal, 'deployment', lambda _client: ('fixture', {}, {
        'environment_hash': 'sha256:' + 'a' * 64, 'environment_id': 'fixture-lean',
    }))
    if create:
        proof = service.submit({'schema_version': 1, 'request_id': 'create', 'workspace': 'reference',
            'operation': 'proof-create', 'arguments': {'spec': json.dumps(SPEC)}})['proof_id']
    else:
        proof = service.store.proof_ids()[0]
    record, _ = service.jobs.submit({'schema_version': 1, 'request_id': 'cycle', 'workspace': 'reference',
        'operation': 'proof-cycle', 'arguments': {'proof': proof}})
    operation = record['operation_id']
    handoffs = []
    def search_handoff(_operation, state, **_kwargs):
        handoffs.append(deepcopy(state))
        service.formal._release(SimpleNamespace(**state['selection_lease']))
        return {'proof_id': proof, 'lifecycle': 'succeeded', 'mathematical_disposition': 'budget-exhausted'}
    monkeypatch.setattr(service.formal, 'run_selected', search_handoff)
    return service, proof, operation, llm, handoffs


@pytest.mark.parametrize('metadata', [{}, {'lean_name': None}, {'lean_name': ''},
                                       {'lean_name': '  '}, {'lean_name': 'supplied_target'}])
def test_name_is_persisted_before_source_and_reaches_search_handoff(tmp_path, monkeypatch, metadata):
    raw = {'lean_type': 'True'} | metadata
    service, proof, operation, llm, handoffs = receiver(tmp_path, monkeypatch, [raw, REVIEW])
    commit = service.gate.commit
    def checked_commit(proposal):
        if proposal.worker_class == 'formalizer':
            saved = service.jobs.intake_state(operation)
            assert saved['candidate'] == raw
            assert saved['effective_candidate']['lean_name'] == proposal.ops[0].fields['lean_name']
        return commit(proposal)
    monkeypatch.setattr(service.gate, 'commit', checked_commit)
    try:
        result = service.cycles.run(operation, proof)
        assert result['cycle_disposition'] == 'formal-executed'
        assert len(handoffs) == 1
        declaration = service.view.node(handoffs[0]['declaration_id'])
        name = declaration.fields['lean_name']
        generated = metadata.get('lean_name') != 'supplied_target'
        if generated:
            assert re.fullmatch(r'persistent_atp_[0-9a-f]{64}', name)
        else:
            assert name == 'supplied_target'
        assert declaration.fields['status'] == 'aligned'
        assert declaration.fields['lean_type'] == 'True'
        state = service.jobs.intake_state(operation)
        assert state['candidate'] == raw
        assert state['lean_name_origin'] == ('host-generated' if generated else 'model')
        source = service.artifacts.get(declaration.fields['lean_source_artifact'])
        assert f'theorem {name} : True'.encode() in source
        assert name in llm.history[1][1]
        assert len(llm.history) == 2
        assert service.cycles.run(operation, proof) == result
        assert len(handoffs) == 1
        assert not service._status(proof)['formal_verified']
        assert not service.store.active_leases(proof)
    finally:
        service.close()


def test_saved_name_survives_receiver_recovery_without_regeneration(tmp_path, monkeypatch):
    raw = {'lean_name': None, 'lean_type': 'True'}
    service, proof, operation, llm, _ = receiver(tmp_path, monkeypatch, [raw])
    save = IntakeWorkflow._save
    def interrupt(workflow, op, state):
        save(workflow, op, state)
        if 'effective_candidate' in state:
            raise SystemExit('saved naming decision')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(IntakeWorkflow, '_save', interrupt)
            with pytest.raises(SystemExit, match='saved naming decision'):
                service.intake.run(operation, proof)
        saved = service.jobs.intake_state(operation)
        assert len(llm.history) == 1
    finally:
        service.close()
    service, _, recovered_operation, llm, handoffs = receiver(tmp_path, monkeypatch, [REVIEW], create=False)
    try:
        assert recovered_operation == operation
        assert service.cycles.run(operation, proof)['cycle_disposition'] == 'formal-executed'
        recovered = service.jobs.intake_state(operation)
        assert recovered['effective_candidate'] == saved['effective_candidate']
        assert recovered['candidate'] == raw
        assert len(llm.history) == 1
        assert service.view.node(handoffs[0]['declaration_id']).fields['lean_name'] == saved['effective_candidate']['lean_name']
    finally:
        service.close()


@pytest.mark.parametrize('candidate', [
    {'lean_name': None, 'lean_type': ''}, {'lean_name': None, 'lean_type': 'True := by sorry'},
    {'lean_name': 42, 'lean_type': 'True'}, {'lean_name': 'bad; axiom bad : False', 'lean_type': 'True'},
])
def test_naming_cannot_repair_refusals_or_bypass_validation(tmp_path, monkeypatch, candidate):
    service, proof, operation, llm, handoffs = receiver(tmp_path, monkeypatch, [candidate])
    try:
        result = service.cycles.run(operation, proof)
        assert result['formalization_disposition'] == 'needs-clarification'
        assert service.jobs.intake_state(operation)['candidate'] == candidate
        assert not service.view.list_nodes(proof, 'FormalDeclaration')
        assert not handoffs
        assert len(llm.history) == 1
        assert not service._status(proof)['formal_verified']
        assert not service.store.active_leases(proof)
    finally:
        service.close()
