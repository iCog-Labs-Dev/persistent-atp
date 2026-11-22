"""Versioned refinement intent and strict wire pins, without proof authority."""
from dataclasses import replace
from importlib.resources import files
import json

import pytest
from jsonschema import Draft202012Validator

from persistent_atp_client import AsyncClient, Budget, CapabilitySet, ParentRefine, Preconditions
from persistent_atp_client.codecs import canonical_json, request_from_json, to_dict
from persistent_atp_client.models import action_types_for_contract


def request():
    return AsyncClient(None).prepare(ParentRefine('p1','p1/fd-1','p1/c-2',Budget(5,1000)),
        workspace_id='reference',preconditions=Preconditions(expected_revision=7,
            expected_theorem_hash='sha256:'+'a'*64))


def test_refinement_has_its_own_negotiated_contract_and_strict_schema():
    prepared=request()
    assert prepared.contract_version=='2.3'
    assert request_from_json(canonical_json(prepared))==prepared
    schema=json.loads(files('persistent_atp_client').joinpath('schemas/refinement-request.schema.json').read_text())
    Draft202012Validator(schema).validate(prepared.to_dict())
    public=json.loads(files('persistent_atp_client').joinpath('schemas/public-models.schema.json').read_text())
    cap_schema={'$defs':public['$defs'],'$ref':'#/$defs/CapabilitySet'}
    Draft202012Validator(cap_schema).validate(to_dict(CapabilitySet('2.3',frozenset({'parent-refine'}))))
    assert all('parent-refine' not in action_types_for_contract(version) for version in ('2.0','2.1','2.2'))


@pytest.mark.parametrize('pins',[Preconditions(),Preconditions(expected_revision=7),
    Preconditions(expected_theorem_hash='sha256:'+'a'*64)])
def test_both_revision_and_literal_target_pins_are_required(pins):
    with pytest.raises(ValueError):replace(request(),preconditions=pins)


@pytest.mark.parametrize('previous,lemma',[('p2/fd-1','p1/c-2'),('p1/fd-1','p2/c-2'),('p1/fd-1','p1/c-1')])
def test_inputs_cannot_escape_proof_or_make_parent_its_own_dependency(previous,lemma):
    with pytest.raises(ValueError):ParentRefine('p1',previous,lemma,Budget(5,1000))


def test_old_version_and_arbitrary_mutation_fields_are_rejected():
    with pytest.raises(ValueError):replace(request(),contract_version='2.2')
    raw=request().to_dict();raw['action']['ops']=[]
    with pytest.raises(ValueError):request_from_json(json.dumps(raw))
