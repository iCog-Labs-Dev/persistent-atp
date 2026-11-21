"""Trusted intake and evidence checks for model-driven ATP acceptance."""
import json
from types import SimpleNamespace

import pytest

from commit_gate.canon import canonical_json
from omegaclaw.playground import Docker
from omegaclaw.playground_atp import ReceiverHost, digest, load_input
from omegaclaw.service import OperationService


@pytest.fixture
def reviewed(tmp_path):
    source = b'theorem target (P : Prop) (h : P) : P := by exact h\n'
    (tmp_path / 'Target.lean').write_bytes(source)
    spec = {'theorem': 'For every proposition P, P implies P.', 'definitions': {}, 'assumptions': [],
        'quantifier_domains': {'P': 'Prop'}, 'allowed_background': [], 'targets': ['P implies P'],
        'verification_policy': {'independent_lean_replay': True}}
    value = {'spec': spec, 'declaration': {'source': 'Target.lean', 'lean_name': 'target', 'lean_type': '∀ (P : Prop), P → P',
            'toolchain': 'leanprover/lean4:v4.10.0-rc1'},
        'review': {'verdict': 'aligned', 'reviewer': 'fixture-reviewer', 'claim_text': spec['theorem'],
            'lean_name': 'target', 'lean_type': '∀ (P : Prop), P → P', 'source_hash': digest(source),
            'toolchain': 'leanprover/lean4:v4.10.0-rc1',
            'spec_hash': digest(canonical_json(spec)), 'environment_hash': 'sha256:' + '1' * 64,
            'rationale': 'The formal binders and implication exactly match the literal target.'}}
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(value))
    return path, value


@pytest.mark.parametrize('field', ['claim_text', 'lean_name', 'lean_type', 'source_hash', 'spec_hash', 'toolchain', 'verdict'])
def test_intake_rejects_review_for_different_input(reviewed, field):
    path, value = reviewed
    value['review'][field] = 'different'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='does not bind'):
        load_input(path)


def test_intake_rejects_source_changed_after_review(reviewed):
    path, _ = reviewed
    path.with_name('Target.lean').write_bytes(b'theorem target : False := by sorry\n')
    with pytest.raises(ValueError, match='does not bind'):
        load_input(path)


def test_intake_requires_explicit_reviewer_and_environment(reviewed):
    path, value = reviewed
    value['review'].pop('reviewer')
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='needs reviewer'):
        load_input(path)


def test_review_is_committed_under_a_real_gate_lease_and_cannot_replace_model_input(reviewed, tmp_path):
    path, value = reviewed
    host = object.__new__(ReceiverHost)
    host.prepared = load_input(path)
    host.deployment = {'environment_hash': value['review']['environment_hash'], 'environment_id': 'fixture-env'}
    service = host.service = OperationService(tmp_path / 'service')
    try:
        service.submit({'schema_version': 1, 'request_id': 'create', 'workspace': 'reference',
            'operation': 'proof-create', 'arguments': {'spec': json.dumps(value['spec'])}})
        proof = service.store.proof_ids()[0]
        host.prepare_declaration(proof)
        assert service.store.verify_chain(proof) == 2
        assert not service.store.active_leases(proof)
        assert service.view.node(proof + '/al-1').fields['actor'] == 'fixture-reviewer'
        assert service.view.node(proof + '/fd-1').fields['lean_source_artifact'] == value['review']['source_hash']
        assert service.view.node(proof + '/env-1').fields['toolchain'] == value['review']['toolchain']
        host.prepared['spec']['theorem'] = 'False'
        with pytest.raises(ValueError, match='differs from'):
            host.prepare_declaration(proof)
        assert service.store.verify_chain(proof) == 2
    finally:
        service.close()


def test_atp_container_receives_receiver_credentials_outside_model_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'model-secret')
    atp = {'PERSISTENT_ATP_OMEGA_URL': 'http://127.0.0.1:8765',
        'PERSISTENT_ATP_OMEGA_TOKEN': 'receiver-secret', 'PERSISTENT_ATP_OMEGA_WORKSPACE': 'reference'}
    runner = Docker('test-image', 'DeepSeek', 'model', tmp_path, 'test', 9000, atp=atp)
    (tmp_path / 'nginx.conf.template').write_text('existing')
    calls = []
    runner.call = lambda *args, **kw: (calls.append(args) or SimpleNamespace(stdout='container', stderr='', returncode=0))
    runner.start()
    command = calls[-1]
    assert 'persistentAtpEnabled=enabled' in command
    assert 'maxNewInputLoops=24' in command
    assert 'PERSISTENT_ATP_OMEGA_TOKEN' in command
    assert not any('receiver-secret' in value for value in command)
    assert runner.scrub('model-secret receiver-secret') == '[REDACTED] [REDACTED]'


def test_reporting_policy_names_trusted_workspace_and_immediate_failure(monkeypatch):
    from omegaclaw.omega_plugin.omega_persistent_atp_bridge import reporting_policy
    monkeypatch.setenv('PERSISTENT_ATP_OMEGA_WORKSPACE', 'scope-for-test')
    policy = reporting_policy()
    assert 'workspace argument is exactly "scope-for-test"' in policy
    assert 'no asynchronous result will arrive' in policy


def test_export_includes_transitive_provider_artifacts_from_set_field():
    from omegaclaw.export import referenced_artifacts
    summary = digest(b'actual-search-summary')
    body = canonical_json({'artifacts': [{'role': 'search-summary', 'sha256': summary}]})
    result_ref = digest(body)
    events = [{'ops': [{'op': 'set_field', 'field': 'provider_result_artifact', 'value': result_ref}]}]
    assert referenced_artifacts(events, read=lambda key: body) == {result_ref, summary}


def test_notification_recovery_check_reads_actual_bridge_records_only():
    from omegaclaw.playground_atp import notification_ids
    history = '\n'.join(['(send "I saw PERSISTENT_ATP_RESULT {not a receipt}")',
        'PERSISTENT_ATP_RESULT {"notification_id":"original"}',
        'PERSISTENT_ATP_RESULT {"notification_id":"original"}'])
    assert notification_ids(history) == ['original', 'original']


def test_receiver_root_cannot_be_owned_by_two_playgrounds(tmp_path, monkeypatch):
    import fcntl
    from omegaclaw import playground_atp
    config = {'profile': 'production', 'formal_provider': True, 'host': '127.0.0.1', 'workspace': 'reference'}
    monkeypatch.setattr(playground_atp, 'load_config', lambda name: (config, tmp_path))
    monkeypatch.setattr(playground_atp.FormalProviderConfig, 'from_environment', lambda: object())
    monkeypatch.setenv('ARTIFACT_DB_URL', 'not-contacted')
    monkeypatch.setenv('MORK_LIBRARY', 'not-loaded')
    with (tmp_path / 'omega-playground.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='another playground owns'):
            ReceiverHost('unused', tmp_path, {})


def test_automated_intake_accepts_spec_only_and_rejects_prepared_review(reviewed):
    path, value = reviewed
    with pytest.raises(ValueError, match='only the exact theorem spec'):
        load_input(path, intake=True)
    path.write_text(json.dumps({'spec': value['spec']}))
    assert load_input(path, intake=True) == {'spec': value['spec']}
