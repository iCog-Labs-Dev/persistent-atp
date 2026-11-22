"""Candidate/refusal/revision lifecycle; LLM and search are labelled fixtures."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from commit_gate.canon import canonical_json
from omegaclaw.api import serve
from omegaclaw.export import check_export
from omegaclaw.frameworks.omega import OmegaAdapter
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.tests.test_formal_v2_receiver import provider, checked_receiver
from omegaclaw.tests.test_hypothesis_recovery import hypothesis, HypothesisCase
from omegaclaw.tests.test_production_intake import CANDIDATE, REVIEW, SPEC
from omegaclaw.tests.test_research_runtime import candidate
from persistent_atp_client import ArtifactRef, AsyncClient, Budget, HypothesisRevise, Preconditions
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import ArtifactUnavailable, StalePrecondition

REFUSAL = {'lean_name': None, 'lean_type': '', 'notes': 'Define the intended domain before translating this statement.'}


def revised_request(case, previous=None, *, spec=None, request_id='corrected', revision=None):
    spec = deepcopy(SPEC if spec is None else spec)
    ref = case.service.artifacts.put(canonical_json(spec))
    return {'schema_version': 1, 'request_id': request_id, 'workspace': 'reference',
        'operation': 'hypothesis-revise', 'arguments': {'proof': case.proof, 'hypothesis': case.hypothesis,
            'spec_artifact': ref, 'previous_claim': previous or '', 'expected_revision': str(
                case.service.store.head(case.proof)[0] if revision is None else revision)}}


@pytest.mark.parametrize('kind', ['research-move', 'legacy', 'missing-spec'])
def test_only_specified_mathematical_claims_are_admitted(provider, tmp_path, monkeypatch, kind):
    suggestion = candidate('Investigate the missing domain')
    if kind == 'research-move':
        suggestion.update(candidate_kind='research-move', formalization_spec=None)
    else:
        suggestion.pop('formalization_spec')
        if kind == 'legacy': suggestion.pop('candidate_kind')
    case = HypothesisCase(tmp_path, monkeypatch, suggestion=suggestion)
    try:
        revision = case.service.store.head(case.proof)[0]
        result = case.run()
        assert result['phase'] == 'finished' and result['formalization_disposition'] == 'needs-clarification'
        assert not result['formalization_ready']
        assert case.service.store.head(case.proof)[0] == revision
        assert len(case.service.view.list_nodes(case.proof, 'Claim')) == 1
        assert not case.llm.history
        # Explicit specification refines the suggestion without changing it.
        corrected = case.service.submit(revised_request(case))
        assert corrected['formalization_ready'], corrected
        assert dict(case.service.view.node(case.hypothesis).fields)['statement'] == suggestion['statement']
    finally: case.service.close()


@pytest.mark.parametrize('draft', [REFUSAL, CANDIDATE | {'lean_type': 'True := by sorry'},
                                 CANDIDATE | {'lean_name': 42}, 'not JSON'])
def test_invalid_or_refused_draft_is_durable_clarification(hypothesis, draft):
    case = hypothesis
    case.llm.responses = [draft]
    result = case.run()
    assert result['phase'] == 'finished', result
    assert result['formalization_disposition'] == 'needs-clarification'
    assert result['error_code'] == 'FORMALIZATION_NEEDS_CLARIFICATION'
    assert result['clarification_reasons'] and result['report_artifact']
    report = json.loads(case.service.artifacts.get(result['report_artifact']))
    assert report['claim_id'] == result['claim_id']
    assert not case.service.view.list_nodes(case.proof, 'FormalDeclaration')
    assert not case.service.view.list_nodes(case.proof, 'Certificate')
    assert not case.service.store.active_leases(case.proof)
    calls = len(case.llm.history)
    case.restart()
    assert case.run() == result
    assert len(case.llm.history) == calls
    assert not case.service._status(case.proof)['formal_verified']
    exported = case.service._export(case.proof, 'default')
    payload = case.service.artifacts.get(exported['export_artifact'])
    check_export(payload)
    assert result['report_artifact'] in json.loads(payload)['artifacts']


@pytest.mark.parametrize('boundary', ['report', 'commit', 'finished'])
def test_clarification_recovers_without_new_llm_call(hypothesis, monkeypatch, boundary):
    case = hypothesis
    case.llm.responses = [REFUSAL]
    save, commit = case.service.intake._save, case.service.gate.commit
    stopped = []
    def crash_save(operation, state):
        save(operation, state)
        if not stopped and ((boundary == 'report' and 'clarification' in state)
                or (boundary == 'finished' and state.get('phase') == 'finished')):
            stopped.append(True); raise SystemExit('controlled clarification interruption')
    def crash_commit(proposal):
        result = commit(proposal)
        if not stopped and boundary == 'commit' and result.accepted and any(
                getattr(op, 'label', None) == 'Artifact' for op in proposal.ops):
            stopped.append(True); raise SystemExit('controlled clarification interruption')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(case.service.intake, '_save', crash_save)
        patch.setattr(case.service.gate, 'commit', crash_commit)
        with pytest.raises(SystemExit): case.run()
    assert stopped
    case.restart()
    outcome = case.run()
    assert outcome['formalization_disposition'] == 'needs-clarification', outcome
    assert len(case.llm.history) == 1
    assert len(case.service.view.edges_from(outcome['claim_id'], 'HAS_ARTIFACT')) == 1
    assert not case.service.store.active_leases(case.proof)


def test_revision_preserves_refusal_and_can_reach_independent_replay(hypothesis, provider):
    case = hypothesis
    case.llm.responses = [REFUSAL]
    refused = case.run()
    before = dict(case.service.view.node(refused['claim_id']).fields)
    saved = deepcopy(case.service.jobs.intake_state(case.operation()))
    case.llm.responses = [CANDIDATE, REVIEW]
    request = revised_request(case, refused['claim_id'])
    corrected = case.service.submit(request)
    assert corrected['formalization_ready'], corrected
    assert corrected['claim_id'] != refused['claim_id']
    claim = case.service.view.node(corrected['claim_id'])
    assert claim.fields['previous_claim_id'] == refused['claim_id']
    assert dict(case.service.view.node(refused['claim_id']).fields) == before
    assert case.service.jobs.intake_state(case.operation()) == saved
    assert case.run() == refused
    calls = len(case.llm.history)
    case.restart()
    assert case.service.submit(request) == corrected
    assert len(case.llm.history) == calls
    # Exercise real independent Lean replay of the explicitly scripted True
    # search fixture; this is not a trained-provider acceptance claim.
    case.service.formal.config = replace(case.service.formal.config, mode='real')
    receiver = checked_receiver(case.service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.formal_search(case.proof, corrected['claim_id'], Budget(5, 1000), workspace_id='reference')
        result = await handle.wait(timeout=15, poll_interval=.02)
        assert result.lifecycle == 'succeeded', result.result
        assert result.result['mathematical_disposition'] == 'lean-verified', result.result
        assert case.service.view.node(corrected['claim_id']).fields['status'] == 'lean-verified'
        assert case.service.view.node(refused['claim_id']).fields['status'] == 'provisional'
        assert not (await client.status(proof_id=case.proof, workspace_id='reference')).formal_verified
        # A verified lemma cannot be revised as an unverified attempt.
        rejected = case.service.submit(revised_request(case, corrected['claim_id'], request_id='revise-verified'))
        assert rejected['phase'] == 'failed'
    try: asyncio.run(scenario())
    finally: receiver.close()


def test_revision_cannot_replace_active_or_unrelated_attempt(hypothesis, monkeypatch):
    case = hypothesis
    from omegaclaw.tests.test_hypothesis_recovery import interrupt
    interrupt(case, monkeypatch, 'before-intake')
    previous = case.service.view.edges_from(case.hypothesis, 'PROMOTED_TO')[0].dst_id
    for index, prior in enumerate((None, case.claim, previous)):
        result = case.service.submit(revised_request(case, prior, request_id='invalid-'+str(index)))
        assert result['phase'] == 'failed', result
    assert len(case.service.view.list_nodes(case.proof, 'Claim')) == 2
    assert not case.llm.history


@pytest.mark.parametrize('boundary', ['admission', 'before-intake', 'candidate', 'draft', 'alignment', 'finished'])
def test_revision_recovers_its_own_attempt_and_preserves_previous_one(hypothesis, monkeypatch, boundary):
    from omegaclaw.tests.test_hypothesis_recovery import interrupt
    case = hypothesis
    case.llm.responses = [REFUSAL]
    refused = case.run()
    old_state = deepcopy(case.service.jobs.intake_state(case.operation()))
    case.llm.responses = [CANDIDATE, REVIEW]
    case.request = revised_request(case, refused['claim_id'])
    interrupt(case, monkeypatch, boundary)
    case.restart()
    outcome = case.run()
    assert outcome['formalization_ready'], outcome
    assert len(case.llm.history) == 3
    assert len(case.service.view.edges_from(case.hypothesis, 'PROMOTED_TO')) == 2
    assert len(case.service.view.list_nodes(case.proof, 'Claim')) == 3
    assert case.service.jobs.intake_state(case.operation()) == old_state
    assert not case.service.store.active_leases(case.proof)


def test_omega_revision_tool_uses_explicit_spec_and_stable_identity(hypothesis, tmp_path):
    import time
    from omegaclaw.tests.test_omega_hypothesis import load_bridge
    case = hypothesis
    case.llm.responses = [REFUSAL]
    refused = case.run()
    case.llm.responses = [CANDIDATE, REVIEW]
    receiver = ReferenceReceiver(case.service)
    adapter = OmegaAdapter(LocalReceiverAdapter(receiver), workspace='reference', cache_path=tmp_path/'omega-revision.json')
    bridge = load_bridge(); bridge.configure_adapter(adapter)
    revision = case.service.store.head(case.proof)[0]
    try:
        args = (case.proof, case.hypothesis, json.dumps(SPEC), refused['claim_id'], str(revision), 'reference')
        receipt = json.loads(bridge.hypothesis_revise(*args))
        assert json.loads(bridge.hypothesis_revise(*args))['request_id'] == receipt['request_id']
        deadline = time.monotonic()+10
        outcome = None
        while time.monotonic()<deadline:
            for line in bridge.take_completed().splitlines():
                report = json.loads(line.removeprefix('PERSISTENT_ATP_RESULT '))
                if report.get('request_id')==receipt['request_id'] and report.get('lifecycle')=='succeeded':
                    outcome = report['result']
            if outcome: break
            time.sleep(.02)
        assert outcome and outcome['formalization_ready'], adapter.pending_reports()
        request = adapter.state.intent(receipt['request_id'])['request']
        assert request['action']['kind']=='hypothesis-revise'
        assert request['action']['previous_claim_id']==refused['claim_id']
        assert request['preconditions']['expected_revision']==revision
        raw = adapter.client.get_artifact(ArtifactRef(request['action']['spec_artifact']['hash']), workspace_id='reference')
        assert json.loads(raw)==SPEC
        assert not adapter.status(case.proof)['formal_verified']
    finally:
        bridge.disable(); bridge._pool.shutdown(wait=True)
        receiver.close(); adapter.close()


@pytest.mark.parametrize('transport', ['local', 'http'])
def test_revision_uses_scoped_artifact_revision_and_new_request(hypothesis, transport):
    import threading
    case = hypothesis
    case.llm.responses = [REFUSAL]
    refused = case.run()
    case.llm.responses = [CANDIDATE, REVIEW]
    receiver = ReferenceReceiver(case.service)
    server = thread = None
    if transport == 'http':
        server = serve(case.service, 'revision-test-token', receiver=receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        port = HTTPReceiverAdapter(f'http://127.0.0.1:{server.server_port}', 'revision-test-token')
    else: port = LocalReceiverAdapter(receiver)
    async def scenario():
        client = AsyncClient(port)
        revision = case.service.store.head(case.proof)[0]
        raw = canonical_json(SPEC)
        ref = ArtifactRef(case.service.artifacts.put(raw))
        action = HypothesisRevise(case.proof, case.hypothesis, ref, refused['claim_id'])
        pre = Preconditions(expected_revision=revision)
        with pytest.raises(ArtifactUnavailable):
            await client.submit(client.prepare(action, workspace_id='reference', preconditions=pre))
        await client.put_artifact(raw, workspace_id='reference', media_type='application/json')
        with pytest.raises(StalePrecondition):
            await client.submit(client.prepare(action, workspace_id='reference', preconditions=Preconditions(expected_revision=revision-1)))
        request = client.prepare(action, workspace_id='reference', preconditions=pre)
        handle = await client.submit(request)
        result = await handle.wait(timeout=10, poll_interval=.02)
        assert result.lifecycle == 'succeeded' and result.result['formalization_ready'], result.result
        assert (await client.submit(request)).ref == handle.ref
        report = await client.get_artifact(ArtifactRef(result.result['report_artifact']), workspace_id='reference')
        assert json.loads(report)['review']['verdict'] == 'aligned'
        return result
    try: asyncio.run(scenario())
    finally:
        if server: server.shutdown(); thread.join(5); server.server_close()
        receiver.close()


def test_native_candidate_refusal_revision_and_fresh_recovery(provider, tmp_path, monkeypatch):
    if not os.environ.get('MORK_LIBRARY') or not os.environ.get('ARTIFACT_TEST_DB_URL'):
        if os.environ.get('OMEGA_PERSISTENCE_REQUIRED')=='1':
            pytest.fail('native candidate checks require MORK and a dedicated PostgreSQL database')
        pytest.skip('native MORK and dedicated PostgreSQL required')
    if os.environ.get('OMEGA_CANDIDATE_NATIVE_CASE')!='1':
        result=subprocess.run([sys.executable,'-m','pytest','-q',
            __file__+'::test_native_candidate_refusal_revision_and_fresh_recovery',
            '--basetemp',str(tmp_path/'native-process')],capture_output=True,text=True,timeout=90,
            env={**os.environ,'OMEGA_CANDIDATE_NATIVE_CASE':'1'})
        assert result.returncode==0, result.stdout+result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ['ARTIFACT_TEST_DB_URL'])
    case=HypothesisCase(tmp_path/'receiver',monkeypatch,profile='production')
    closed=False
    try:
        test_revision_preserves_refusal_and_can_reach_independent_replay(case,provider)
        revision=case.service.store.head(case.proof)[0]
        claims={n.node_id:dict(n.fields) for n in case.service.view.list_nodes(case.proof,'Claim')}
        # A separate interpreter has no access to the old process's MORK space.
        case.service.close()
        closed=True
        code='''import json,sys
from omegaclaw.service import OperationService
s=OperationService(sys.argv[1],profile="production")
try:
 s.catch_up(sys.argv[2])
 assert s.store.verify_chain(sys.argv[2])==int(sys.argv[3])
 assert {n.node_id:dict(n.fields) for n in s.view.list_nodes(sys.argv[2],"Claim")}==json.loads(sys.argv[4])
 assert not s._status(sys.argv[2])["formal_verified"]
 for ref in s._artifact_pointers(sys.argv[2]): assert s.artifacts.get(ref)
finally: s.close()
'''
        result=subprocess.run([sys.executable,'-c',code,str(case.root),case.proof,str(revision),json.dumps(claims)],
            capture_output=True,text=True,timeout=45)
        assert result.returncode==0,result.stdout+result.stderr
    finally:
        if not closed: case.service.close()
