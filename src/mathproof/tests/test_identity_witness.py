"""Accepted PoC identity and explicit certificate witness contracts."""
import copy

import pytest

from mathproof.formal_atp import build_result, build_state
from mathproof.schemas import validate
from mathproof.soundness import validate_formal_search_result, SoundnessReason
from mathproof.state_identity import identity_issue
from mathproof.witness import build_witness, validate_witness
from mathproof.maths_ai_atp import MathsAIFormalATP
from mathproof.tests.test_maths_ai_adapter import StubReasoner, kernel_closed_children_graph, REQUEST

ENV = 'sha256:' + '11' * 32


def test_exact_identity_binds_context_order_environment_and_version():
    original = build_state('p', 1, 'P', ENV, local_context=['x : Nat', 'h : P'])
    assert original['serialization_version'] == 1
    assert identity_issue(original, ENV) is None
    for changed in [build_state('p', 2, 'P', ENV, local_context=['h : P', 'x : Nat']),
                    build_state('p', 3, 'P', 'sha256:' + '22' * 32, local_context=original['local_context'])]:
        assert changed['exact_state_hash'] != original['exact_state_hash']
    assert identity_issue({**original, 'goal_text': 'Q'}) == 'exact-state-hash-mismatch'
    assert identity_issue({**original, 'serialization_version': 2}) == 'unsupported-or-incomplete-state-identity'
    legacy = {'exact_state_hash': original['exact_state_hash'], 'goal_text': 'P'}
    before = copy.deepcopy(legacy)
    assert identity_issue(legacy) == 'legacy-state-identity'
    assert legacy == before
    with pytest.raises(ValueError):
        build_state('p', 1, 'P', ENV, local_context='not a context list')


def test_adapter_emits_complete_valid_multi_child_witness():
    result = MathsAIFormalATP(reasoner=StubReasoner(kernel_closed_children_graph())).formal_search_start(REQUEST)
    assert result['disposition'] == 'proved-pending-replay'
    witness = result['certificate']['witness']
    assert validate('certificate-witness', witness) == []
    assert len(witness['membership_set']) == 3
    assert len(witness['closure_leaves']) == 2
    assert [child['child_index'] for child in witness['indexed_obligations']] == [0, 1]
    validate_witness(witness, result)
    assert all(identity_issue(state) is None for state in result['states'])


@pytest.mark.parametrize('field,value', [
    ('membership_set', []), ('closure_leaves', []), ('indexed_obligations', [{'wrong': True}]),
    ('artifact_hash', 'sha256:' + '99' * 32), ('root_state_id', 'p/fs-99'),
])
def test_incomplete_or_mismatched_witness_is_rejected(field, value):
    result = build_result('proved-pending-replay', {'proof_id': 'p', 'run_id': 'p/fr-1', 'environment_hash': ENV})
    result['certificate']['witness'][field] = value
    with pytest.raises((ValueError, KeyError)):
        validate_witness(result['certificate']['witness'], result)
    assert SoundnessReason.INVALID_CERTIFICATE_WITNESS in {v.reason for v in validate_formal_search_result(result)}


def test_new_state_hash_tampering_is_a_soundness_error():
    result = build_result('budget-exhausted', {'proof_id': 'p', 'run_id': 'p/fr-1', 'environment_hash': ENV})
    result['states'][0]['environment_hash'] = 'sha256:' + '22' * 32
    assert SoundnessReason.STATE_IDENTITY_MISMATCH in {v.reason for v in validate_formal_search_result(result)}
