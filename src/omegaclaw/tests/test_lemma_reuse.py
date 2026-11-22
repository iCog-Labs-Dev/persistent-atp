"""Verified source reuse boundaries; no provider package is needed to collect."""
from pathlib import Path
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from omegaclaw.intake_runtime import IntakeConfig
from omegaclaw.lemma_reuse import audit_parent_usage, prefix_source, verified_lemma, validate_binding


@pytest.fixture
def lean():
    explicit = os.environ.get('OMEGA_TEST_LEAN')
    paths = [Path(explicit)] if explicit else sorted((Path.home()/'.elan/toolchains').glob('*/bin/lean'))
    if not paths:
        pytest.skip('an installed native Lean executable is required')
    return str(paths[0])


def test_disabled_reuse_preserves_existing_checkpoint_identity():
    from commit_gate.canon import content_hash
    config = IntakeConfig('DeepSeek', 'fixture')
    fields = {key: value for key, value in config.__dict__.items() if key != 'lemma_reuse'}
    assert config.checkpoint_hash() == content_hash(fields)
    assert IntakeConfig('DeepSeek', 'fixture', lemma_reuse='source-prefix').checkpoint_hash() != config.checkpoint_hash()


def test_reuse_requires_an_explicit_supported_source_scope():
    with pytest.raises(ValueError, match='Init-only'):
        IntakeConfig('DeepSeek', 'fixture', imports=('Mathlib',), lemma_reuse='source-prefix')
    with pytest.raises(ValueError, match='unknown verified lemma'):
        IntakeConfig('DeepSeek', 'fixture', lemma_reuse='invent-a-proof')


@pytest.mark.parametrize('uses_lemma', [False, True])
def test_native_usage_audit_distinguishes_dependency_from_available_but_unused_lemma(lean, uses_lemma):
    prefix = prefix_source({'source': 'import Init\ntheorem bridge : True := by trivial\n',
                            'lean_name': 'bridge'}, mode='source-prefix')
    # A valid parent proof alone is insufficient. It must retain the selected
    # lemma in its elaborated proof value.
    source = prefix + '\ntheorem parent : True := by ' + ('exact bridge' if uses_lemma else 'trivial') + '\n'
    result = audit_parent_usage(source, 'parent', 'bridge', lean=lean, timeout=15)
    assert result['used'] is uses_lemma, result
    assert (result['exit_code'] == 0) is uses_lemma


def test_verified_simp_prefix_is_loaded_and_used_by_native_parent_proof(lean):
    prefix = prefix_source({'source': 'import Init\ntheorem bridge (n : Nat) : 0 + n = n := by simp\n',
                            'lean_name': 'bridge'}, mode='simp-prefix')
    source = prefix + '\ntheorem parent (n : Nat) : (0 + n) + 0 = n := by simp\n'
    result = audit_parent_usage(source, 'parent', 'bridge', lean=lean, timeout=15)
    assert result['used'], result


@pytest.fixture
def verified_graph(tmp_path):
    """Input-view fixture only; integration checks below use actual gate writes."""
    from commit_gate.state import MemoryView
    from omegaclaw.artifact_bytes import ReferenceArtifactStore
    view, artifacts = MemoryView(), ReferenceArtifactStore(tmp_path)
    source = 'import Init\n\ntheorem bridge : True := by\n  exact persistent_atp_pending_proof\n'
    source_hash = artifacts.put(source.encode())
    environment_hash = 'sha256:' + 'a'*64
    target = {'declaration':'bridge','header':'theorem bridge : True','prefix':''}
    statement = {'target': 'True'}
    input_hash = artifacts.put(json.dumps({'format':'maths-ai-formal-input-v1','kind':'source',
        'input':{'kind':'source','artifact_hash':source_hash,'declaration':'bridge',
                 'starting_state':'binders-introduced'},
        'source_artifact_hash':source_hash,'environment_hash':environment_hash,
        'native_target':target,'statement':statement}).encode())
    certificate_hash = artifacts.put(json.dumps({'format':'maths-ai-accepted-certificate-v1',
        'native_target':target,'statement':statement,'steps':['trivial']}).encode())
    receipt = {'status':'verified','protocol_version':2,'run_id':'p1/fr-1','certificate_hash':certificate_hash,
        'source_artifact_hash':source_hash,'input_artifact_hash':input_hash,'environment_hash':environment_hash,
        'verification_cost':{'sorry_detected':False,'session':'fresh-native-compilation',
            'axiom_audit':{'status':'passed','exit_code':0,'forbidden_axioms':[],
                'source':'import Lean\nimport Init\ntheorem bridge : True := by\n  focus\n    trivial\n\n#print axioms bridge\n'}}}
    receipt_hash = artifacts.put(json.dumps(receipt).encode())
    records = (
        ('p1/c-2','Claim',{'status':'lean-verified','claim_text':'True'}),
        ('p1/fd-2','FormalDeclaration',{'lean_name':'bridge','lean_type':'True','lean_source_artifact':source_hash}),
        ('p1/cert-1','Certificate',{'status':'replay-accepted','protocol_version':2,'actor':'producer',
            'source_declaration':'bridge','input_kind':'source','source_artifact_hash':source_hash,
            'input_artifact_hash':input_hash,'artifact_hash':certificate_hash,'producer_run_id':'p1/fr-1',
            'environment_hash':environment_hash}),
        ('p1/replay-1','LeanReplay',{'actor':'independent','status':'verified','sorry_detected':False,
            'protocol_version':2,'certificate_id':'p1/cert-1','artifact_hash':certificate_hash,'run_id':'p1/fr-1',
            'receipt_artifact':receipt_hash,'source_artifact_hash':source_hash,'input_artifact_hash':input_hash,
            'environment_hash':environment_hash}),
        ('p1/al-2','Alignment',{'lifecycle':'reviewed','verdict':'aligned','source_hash':source_hash}),
        ('p1/env-1','Environment',{'environment_hash':environment_hash}),
    )
    for identity,label,fields in records:view.add_node(identity,label,fields)
    for index,(relation,left,right) in enumerate((
        ('PROVED_BY','p1/c-2','p1/cert-1'),('CERTIFIES','p1/cert-1','p1/fd-2'),
        ('REPLAYED_BY','p1/cert-1','p1/replay-1'),('ALIGNS_DECLARATION','p1/al-2','p1/fd-2'),
        ('ALIGNS_CLAIM','p1/al-2','p1/c-2'),('PINNED_ENVIRONMENT','p1/fd-2','p1/env-1'),
    )):view.add_edge(relation,left,right,f'p1/e-{index}')
    return SimpleNamespace(view=view,artifacts=artifacts)


def test_verified_binding_reuses_only_the_replayed_declaration(verified_graph):
    binding=verified_lemma(verified_graph,'p1','p1/c-2')
    assert binding['lean_name']=='bridge'
    assert binding['source'].startswith('import Init\n')
    assert 'theorem bridge : True := by\n  focus\n    trivial' in binding['source']
    assert '#print' not in binding['source'] and 'import Lean' not in binding['source']
    validate_binding(verified_graph,binding)


@pytest.mark.parametrize('identity,field,value',[
    ('p1/c-2','status','provisional'),('p1/cert-1','status','replay-pending'),
    ('p1/replay-1','actor','producer'),('p1/replay-1','sorry_detected',True),
    ('p1/replay-1','certificate_id','p1/other-certificate'),
    ('p1/replay-1','artifact_hash','sha256:'+'b'*64),
    ('p1/replay-1','run_id','p1/other-run'),('p1/replay-1','protocol_version',1),
    ('p1/al-2','verdict','ambiguous'),('p1/env-1','environment_hash','sha256:'+'b'*64),
])
def test_changed_or_insufficient_committed_evidence_cannot_be_reused(verified_graph,identity,field,value):
    binding=verified_lemma(verified_graph,'p1','p1/c-2')
    verified_graph.view.set_field(identity,field,value)
    with pytest.raises(ValueError):validate_binding(verified_graph,binding)


def test_saved_proof_text_cannot_be_replaced_by_an_unverified_proposal(verified_graph):
    binding=deepcopy(verified_lemma(verified_graph,'p1','p1/c-2'))
    binding['source']='import Init\naxiom bridge : False\n'
    with pytest.raises(ValueError,match='saved verified lemma changed'):
        validate_binding(verified_graph,binding)


def test_source_input_manifest_must_bind_the_certified_declaration(verified_graph):
    certificate = verified_graph.view.node('p1/cert-1')
    manifest = json.loads(verified_graph.artifacts.get(certificate.fields['input_artifact_hash']))
    manifest['input']['declaration'] = 'different'
    ref = verified_graph.artifacts.put(json.dumps(manifest).encode())
    verified_graph.view.set_field('p1/cert-1','input_artifact_hash',ref)
    with pytest.raises(ValueError,match='admitted source input'):
        verified_lemma(verified_graph,'p1','p1/c-2')
