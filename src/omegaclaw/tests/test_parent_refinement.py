"""Controlled search proposals, real native replay and fenced refinement writes."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from commit_gate.ops import SetField, RemoveEdge
from commit_gate.store import LeaseCandidate
from omegaclaw.refinement_runtime import ParentRefinementWorkflow
from omegaclaw.intake_runtime import IntakeWorkflow
from omegaclaw.formal_runtime import FormalWorkflow, ReplayReconciliationRequired
from omegaclaw.tests.test_lemma_reuse_integration import reusable_case
from omegaclaw.tests.test_formal_v2_receiver import provider, checked_receiver
from maths_ai.formal.backends import AttemptOutcome
from omegaclaw.tests.test_production_intake import CANDIDATE, REVIEW
from persistent_atp_client import AsyncClient, ArtifactRef, Budget, ParentRefine, Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter


@pytest.fixture
def failed_parent(reusable_case, monkeypatch):
    case,backend,old_operation = reusable_case
    service = case.service
    dependency = service.view.edges_from(case.claim,'DEPENDS_ON')[0]
    lemma = dependency.dst_id
    case.commit([RemoveEdge('DEPENDS_ON',dependency.edge_id)])
    # Finish the direct fixture's candidate verification ledger entry.
    for row in service.jobs.conn.execute("SELECT operation_id,formal_json FROM operations WHERE formal_json IS NOT NULL"):
        outcome = service.formal._report(json.loads(row['formal_json']))
        service.jobs.update(row['operation_id'],{**outcome,'phase':'finished'})
    service.intake.config = replace(service.intake.config,lemma_reuse='disabled')
    prepared = service.intake.run(old_operation,case.proof)
    original_run = backend.run
    async def exhaust(*_args,**_kwargs):
        return AttemptOutcome('step-budget',steps_used=1)
    with monkeypatch.context() as patch:
        patch.setattr(backend,'run',exhaust)
        gate_commit=service.gate.commit
        def inspect_commit(proposal):
            result=gate_commit(proposal)
            assert result.accepted,[str(item) for item in result.rejections]
            return result
        patch.setattr(service.gate,'commit',inspect_commit)
        outcome = service.formal.run(old_operation,case.proof,case.claim,{'max_steps':1,'wall_clock_ms':1000})
    assert outcome['mathematical_disposition']=='budget-exhausted',outcome
    service.jobs.update(old_operation,{**outcome,'phase':'finished'})
    service.intake.config = replace(service.intake.config,lemma_reuse='source-prefix')
    case.llm.responses = [CANDIDATE|{'lean_name':'parent_retry'},REVIEW]
    backend.use_bridge = True
    previous = prepared['declaration_id']
    arguments = {'proof':case.proof,'previous_declaration':previous,'lemma':lemma,
        'budget':json.dumps({'max_steps':5,'wall_clock_ms':1000}),
        'expected_revision':str(service.store.head(case.proof)[0]),
        'expected_theorem_hash':service.view.node(case.proof+'/proof').fields['target_artifact']}
    request = {'schema_version':1,'request_id':'refine-parent','workspace':'reference',
        'operation':'parent-refine','arguments':arguments}
    return case,backend,request


def run(case,request):
    job,_ = case.service.jobs.submit(request)
    args = request['arguments']
    return case.service.refinement.run(job['operation_id'],case.proof,args['previous_declaration'],args['lemma'],
        json.loads(args['budget']),expected_revision=int(args['expected_revision']),
        expected_theorem_hash=args['expected_theorem_hash'])


def test_failed_parent_gets_new_selected_declaration_and_verified_lemma_use(failed_parent):
    case,backend,request = failed_parent
    previous = request['arguments']['previous_declaration']
    original = deepcopy(dict(case.service.view.node(previous).fields))
    result = run(case,request)
    assert result['mathematical_disposition']=='lean-verified',result
    assert result['declaration_id']!=previous
    assert result['selected_move_id']==result['declaration_id']
    assert dict(case.service.view.node(previous).fields)==original
    assert case.service._status(case.proof)['literal_target']=='True'
    assert case.service._status(case.proof)['formal_verified']
    assert json.loads(case.service.artifacts.get(result['lemma_use_audit_artifact']))['used']
    assert result['refinement_artifact'] in case.service._artifact_pointers(case.proof)
    assert case.service.store.verify_chain(case.proof)==case.service.store.head(case.proof)[0]


def test_sdk_negotiates_refinement_and_preserves_original_request_identity(failed_parent):
    case,backend,request = failed_parent
    receiver = checked_receiver(case.service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        args = request['arguments']
        prepared = client.prepare(ParentRefine(case.proof,
            args['previous_declaration'],args['lemma'],Budget(5,1000)),workspace_id='reference',
            request_id='sdk-refine',preconditions=Preconditions(expected_revision=int(args['expected_revision']),
                expected_theorem_hash=args['expected_theorem_hash']))
        handle = await client.submit(prepared)
        result = await handle.wait(timeout=20,poll_interval=.05)
        assert result.lifecycle=='succeeded',result.result
        assert result.result['mathematical_disposition']=='lean-verified',result.result
        for key in ('lemma_use_audit_artifact','refinement_artifact'):
            raw = await client.get_artifact(ArtifactRef(result.result[key]),workspace_id='reference')
            assert json.loads(raw)['format'].startswith('persistent-atp-')
        repeated = await receiver.submit(prepared)
        assert repeated.operation_ref==handle.ref
    try:asyncio.run(scenario())
    finally:receiver.close()


@pytest.mark.parametrize('boundary',['prepared','admission','draft','source','alignment','intake-finished','search-ready','audit'])
def test_refinement_recovers_saved_stage_without_repeating_mathematical_work(failed_parent,monkeypatch,boundary):
    case,backend,request = failed_parent
    service = case.service
    original_save = ParentRefinementWorkflow._save
    intake_save = IntakeWorkflow._save
    formal_save = FormalWorkflow._save
    commit = service.gate.commit
    fired=[]
    def stop():
        if not fired:fired.append(True);raise SystemExit('refinement interruption '+boundary)
    def saved(workflow,op,state):
        original_save(workflow,op,state)
        if boundary in {'prepared','search-ready'} and state['phase']==boundary:stop()
    def intake_saved(workflow,op,state):
        intake_save(workflow,op,state)
        if boundary=='intake-finished' and state.get('refinement_artifact') and state['phase']=='finished':stop()
    def formal_saved(workflow,op,state):
        formal_save(workflow,op,state)
        if boundary=='audit' and state.get('lemma_use_audit_artifact'):stop()
    def committed(proposal):
        result=commit(proposal)
        if result.accepted:
            if boundary=='admission' and any(getattr(op,'label',None)=='Artifact' for op in proposal.ops):stop()
            if boundary=='draft' and proposal.worker_class=='formalizer':stop()
            if boundary=='source' and any(getattr(op,'label',None)=='Environment' for op in proposal.ops):stop()
            if boundary=='alignment' and any(getattr(op,'label',None)=='Alignment' for op in proposal.ops):stop()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(ParentRefinementWorkflow,'_save',saved)
        patch.setattr(IntakeWorkflow,'_save',intake_saved)
        patch.setattr(FormalWorkflow,'_save',formal_saved)
        patch.setattr(service.gate,'commit',committed)
        with pytest.raises(SystemExit):run(case,request)
    assert fired
    history=len(case.llm.history);replays=backend.replays
    auditor=service.formal.config.reuse_lean
    case.restart()
    case.service.formal.config=replace(case.service.formal.config,mode='real',reuse_lean=auditor)
    case.service.intake.config=replace(case.service.intake.config,lemma_reuse='source-prefix')
    result=run(case,request)
    assert result['mathematical_disposition']=='lean-verified',result
    if boundary in {'alignment','intake-finished','search-ready','audit'}:assert len(case.llm.history)==history
    if boundary=='audit':assert backend.replays==replays
    assert len([node for node in case.service.view.list_nodes(case.proof,'FormalDeclaration')
        if node.fields.get('parent_refinement_artifact')])==1


@pytest.mark.parametrize('field,value,match',[
    ('expected_revision','0','proof changed'),('expected_theorem_hash','sha256:'+'0'*64,'literal parent'),
    ('lemma','parent','committed Claim'),('previous_declaration','lemma','literal parent'),
])
def test_invalid_refinement_does_not_change_graph_or_call_model(failed_parent,field,value,match):
    case,backend,request=failed_parent
    revision=case.service.store.head(case.proof)[0];calls=len(case.llm.history)
    if value=='parent':value=case.claim
    if value=='lemma':
        claim=request['arguments']['lemma']
        cert=case.service.view.edges_from(claim,'PROVED_BY')[0].dst_id
        value=case.service.view.edges_from(cert,'CERTIFIES')[0].dst_id
    request['arguments'][field]=value
    with pytest.raises((ValueError,KeyError)):run(case,request)
    assert case.service.store.head(case.proof)[0]==revision
    assert len(case.llm.history)==calls


def test_unrelated_commit_after_saved_intent_blocks_recovery_and_releases_own_lease(failed_parent,monkeypatch):
    case,backend,request=failed_parent
    save=ParentRefinementWorkflow._save
    def interrupt(workflow,op,state):
        save(workflow,op,state)
        if state['phase']=='prepared':raise SystemExit('prepared')
    with monkeypatch.context() as patch:
        patch.setattr(ParentRefinementWorkflow,'_save',interrupt)
        with pytest.raises(SystemExit):run(case,request)
    # A separate accepted graph mutation is never silently rebased.
    state=case.service.jobs.conn.execute('SELECT operation_id FROM operations WHERE request_id=?',('refine-parent',)).fetchone()
    prepared=case.service.jobs.refinement_state(state[0])
    case.service.intake._release(prepared['initial_lease'])
    case.commit([SetField('Claim',case.claim,'state_novelty',0.2)])
    with pytest.raises(ValueError,match='inputs changed|outside the parent'):run(case,request)
    assert not case.service.store.active_leases(case.proof)


def test_failed_operation_with_uncertain_replay_cannot_be_bypassed(failed_parent):
    case,backend,request=failed_parent
    job,_=case.service.jobs.submit({'schema_version':1,'request_id':'uncertain','workspace':'reference',
        'operation':'formal-search','arguments':{'proof':case.proof,'claim':case.claim,'budget':'{}'}})
    case.service.jobs.save_formal(job['operation_id'],{'proof_id':case.proof,'phase':'replay-started',
        'run_id':case.proof+'/fr-999','binding':{'snapshot':{'result':{'disposition':'proved-pending-replay'}}}})
    case.service.jobs.update(job['operation_id'],{'phase':'failed'})
    with pytest.raises(ReplayReconciliationRequired,match='reconciliation'):run(case,request)
    assert not case.service.store.active_leases(case.proof)


@pytest.mark.parametrize('recovery',['expired-lease','other-owner','expired-deadline'])
def test_prepared_refinement_respects_lease_and_original_deadline(failed_parent,monkeypatch,recovery):
    case,backend,request=failed_parent
    save=ParentRefinementWorkflow._save
    saved=[]
    def interrupt(workflow,op,state):
        save(workflow,op,state)
        if state['phase']=='prepared':saved.append(state);raise SystemExit('prepared')
    with monkeypatch.context() as patch:
        patch.setattr(ParentRefinementWorkflow,'_save',interrupt)
        with pytest.raises(SystemExit):run(case,request)
    state=saved[0];lease=state['initial_lease'];revision=case.service.store.head(case.proof)[0]
    if recovery=='expired-deadline':
        monkeypatch.setattr('omegaclaw.refinement_runtime.time.time',lambda:state['deadline']+1)
        assert run(case,request)['lifecycle']=='cancelled'
        assert case.service.store.head(case.proof)[0]==revision
    elif recovery=='other-owner':
        case.service.intake._release(lease)
        owner=case.service.store.claim_next(case.proof,[LeaseCandidate(case.claim,{})],
            actor='another-worker',worker_class='coordinator',base_revision=revision)
        try:
            with pytest.raises(ValueError,match='lease recovery'):run(case,request)
            assert case.service.store.active_leases(case.proof)[0].lease_id==owner.lease_id
        finally:case.service.store.release_lease(case.proof,owner.lease_id,fencing_token=owner.fencing_token)
    else:
        monkeypatch.setattr(case.service.store,'_clock_ns',lambda:int(lease['expires_at']*1e9)+1_000_000_000)
        assert run(case,request)['mathematical_disposition']=='lean-verified'
