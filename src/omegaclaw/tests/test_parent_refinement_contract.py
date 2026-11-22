"""Default collection checks need no provider checkout or native Lean."""
import json

import pytest

from omegaclaw.frameworks.omega import OmegaAdapter
from persistent_atp_client.testing.scripted import ScriptedReceiverPort


def arguments():
    return {'proof':'p1','previous_declaration':'p1/fd-1','lemma':'p1/c-2',
        'budget':json.dumps({'max_steps':5,'wall_clock_ms':1000}),
        'expected_revision':'7','expected_theorem_hash':'sha256:'+'a'*64}


def test_refinement_reservations_count_against_the_existing_parent_cycle_limit(tmp_path):
    adapter=OmegaAdapter(ScriptedReceiverPort(),workspace='reference',cache_path=tmp_path/'state.json',max_cycles_per_proof=1)
    identity=adapter.reserve('parent-refine','reference',arguments(),intent_id='retry',new_intent=True)
    assert adapter.reserve('parent-refine','reference',arguments(),intent_id='retry',new_intent=True)==identity
    with pytest.raises(ValueError,match='CYCLE_LIMIT_REACHED'):
        adapter.reserve('proof-cycle','reference',{'proof':'p1'},intent_id='next',new_intent=True)


def test_existing_managed_task_ownership_cannot_be_bypassed_by_refinement(tmp_path):
    adapter=OmegaAdapter(ScriptedReceiverPort(),workspace='reference',cache_path=tmp_path/'state.json')
    adapter.state.update(lambda data:data['tasks'].update({'owner':{'proof_id':'p1'}}))
    with pytest.raises(ValueError,match='TASK_OWNED_BY_COORDINATOR'):
        adapter.reserve('parent-refine','reference',arguments())
