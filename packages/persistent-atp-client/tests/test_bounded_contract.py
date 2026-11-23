from dataclasses import replace
from datetime import datetime,timezone,timedelta
from importlib.resources import files
import json

import pytest
from jsonschema import Draft202012Validator,ValidationError

from persistent_atp_client import AsyncClient,BoundedRefine,RefinementPolicy,Budget,Preconditions,ProofCycle
from persistent_atp_client.codecs import canonical_json,request_from_json
from persistent_atp_client.models import action_types_for_contract


def policy():return RefinementPolicy(Budget(1,1000),Budget(5,1000),Budget(5,1000),Budget(5,1000),11,10)


def request():
    return AsyncClient(None).prepare(BoundedRefine('p1',policy()),workspace_id='reference',
        preconditions=Preconditions(expected_revision=1,expected_theorem_hash='sha256:'+'a'*64),
        deadline=datetime.now(timezone.utc)+timedelta(seconds=120))


def test_bounded_contract_roundtrip_preserves_explicit_policy_and_deadline():
    prepared=request()
    assert prepared.contract_version=='2.4'
    assert request_from_json(canonical_json(prepared))==prepared
    schema=json.loads(files('persistent_atp_client').joinpath('schemas/bounded-refinement-request.schema.json').read_text())
    Draft202012Validator(schema).validate(prepared.to_dict())
    assert all('bounded-refine' not in action_types_for_contract(version) for version in ('2.0','2.1','2.2','2.3'))


def test_contract_24_schema_requires_a_deadline_only_for_bounded_refinement():
    schema=json.loads(files('persistent_atp_client').joinpath('schemas/bounded-refinement-request.schema.json').read_text())
    validator=Draft202012Validator(schema)
    ordinary=AsyncClient(None).prepare(ProofCycle('p1'),workspace_id='reference',contract_version='2.4')
    validator.validate(ordinary.to_dict())
    bounded=request().to_dict();bounded['deadline']=None
    with pytest.raises(ValidationError):validator.validate(bounded)


@pytest.mark.parametrize('patch',[{'deadline':None},{'preconditions':Preconditions(expected_revision=1)},
    {'preconditions':Preconditions(expected_theorem_hash='sha256:'+'a'*64)},{'contract_version':'2.3'}])
def test_bounded_contract_cannot_drop_task_pins(patch):
    with pytest.raises(ValueError):replace(request(),**patch)


@pytest.mark.parametrize('patch',[{'total_search_steps':0},{'total_replay_steps':True},
    {'max_research':2},{'max_parent_attempts':3},{'initial':{'max_steps':1,'wall_clock_ms':1000}}])
def test_invalid_cumulative_policy_is_rejected(patch):
    with pytest.raises((TypeError,ValueError)):replace(policy(),**patch)
