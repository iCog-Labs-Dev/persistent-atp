"""Controlled proposals; real provider admission, Lean replay and gate writes."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
import subprocess

import pytest

from omegaclaw.tests.test_formal_v2_receiver import provider,_TEST_LEAN
from omegaclaw.tests.test_production_intake import setup,CANDIDATE,REVIEW
from omegaclaw.tests.test_research_runtime import candidate
from maths_ai.formal.backends import AttemptOutcome,VerifyOutcome
from persistent_atp_client import Budget,RefinementPolicy
from persistent_atp_client.codecs import to_dict
from omegaclaw.bounded_ledger import BoundedLedger,BoundedBlocked
from omegaclaw.bounded_runtime import BoundedRefinementWorkflow
from omegaclaw.formal_runtime import ReplayReconciliationRequired


@pytest.fixture
def bounded_case(provider,tmp_path,monkeypatch):
    if _TEST_LEAN is None:pytest.skip('installed native Lean required')
    backend=provider[1]
    async def elaborate(payload,**_kwargs):
        body,name=payload['body'],payload['declaration'];start=body.rfind('theorem '+name+' :')
        assert start>=0
        return {'target_sexp':'(:c True)','target_pp':'True','locals':[],'target_unit_start':start,'check':{'ok':True},
            'native_target':{'header':body[start:].split(' := by',1)[0],'prefix':body[:start],'declaration':name}}
    async def search(task,**_kwargs):
        target=task.manifest['native_target']
        if target['declaration']=='initial_parent':return AttemptOutcome('step-budget',steps_used=1)
        command='exact bridge' if target['declaration']=='parent_retry' else 'trivial'
        trace={'goal':task.statement['expression'],'tactic':command,'arguments':[],'subgoals':[]}
        cert={'format':'maths-ai-accepted-certificate-v1','statement':task.statement,'native_target':target,'steps':[command],'proof_trace':trace}
        return AttemptOutcome('certified',True,1,trace,cert)
    async def verify(certificate,**_kwargs):
        backend.replays+=1;target=certificate['native_target']
        source=('import Lean\nimport Init\n'+target['prefix']+'\n'+target['header']+' := by\n'+
            '\n'.join('  focus\n'+'\n'.join('    '+line for line in step.splitlines()) for step in certificate['steps'])+
            '\n#print axioms '+target['declaration']+'\n')
        path=tmp_path/'Replay.lean';path.write_text(source)
        result=subprocess.run([str(_TEST_LEAN),str(path)],capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stdout+result.stderr
        return VerifyOutcome('verified',None,1,backend.environment_manifest(),
            {'sorry_detected':False,'session':'fresh-native-compilation','axiom_audit':{'status':'passed',
             'source':source,'exit_code':0,'forbidden_axioms':[]}})
    monkeypatch.setattr(backend,'elaborate_source',elaborate)
    monkeypatch.setattr(backend,'run',search);monkeypatch.setattr(backend,'verify',verify)
    responses=[CANDIDATE|{'lean_name':'initial_parent'},REVIEW,{'candidates':[candidate('True')]},
        CANDIDATE|{'lean_name':'bridge'},REVIEW,CANDIDATE|{'lean_name':'parent_retry'},REVIEW]
    service,proof,llm=setup(tmp_path/'receiver',monkeypatch,responses=responses)
    service.formal.config=replace(service.formal.config,mode='real',reuse_lean=str(_TEST_LEAN))
    service.intake.config=replace(service.intake.config,lemma_reuse='source-prefix')
    policy=to_dict(RefinementPolicy(Budget(1,1000),Budget(5,1000),Budget(5,1000),Budget(5,10000),11,10))
    deadline=(datetime.now(timezone.utc)+timedelta(seconds=120)).timestamp()
    request={'schema_version':1,'workspace':'reference','request_id':'bounded-control','operation':'bounded-refine',
        'arguments':{'proof':proof,'policy':json.dumps(policy),'expected_revision':str(service.store.head(proof)[0]),
         'expected_theorem_hash':service.view.node(proof+'/proof').fields['target_artifact'],
         'deadline':datetime.fromtimestamp(deadline,timezone.utc).isoformat()}}
    try:yield service,proof,llm,backend,request
    finally:service.close()


def run(case):
    service,proof,llm,backend,request=case
    job,_=service.jobs.submit(request);args=request['arguments']
    return service.bounded.run(job['operation_id'],proof,json.loads(args['policy']),
        expected_revision=int(args['expected_revision']),theorem_hash=args['expected_theorem_hash'],
        deadline=datetime.fromisoformat(args['deadline']).timestamp())


def ignore_parent_lemma(backend,monkeypatch):
    original=backend.run
    async def search(task,**kwargs):
        result=await original(task,**kwargs)
        if task.manifest['native_target']['declaration']=='parent_retry':
            result.trace['tactic']='trivial'
            result.certificate['steps']=['trivial']
        return result
    monkeypatch.setattr(backend,'run',search)


def test_bounded_loop_selects_verifies_and_reuses_a_candidate(bounded_case):
    service,proof,llm,backend,request=bounded_case
    result=run(bounded_case)
    assert result['formal_verified'],result
    assert result['cycles_used']==2 and result['research_used']==1 and result['candidate_attempts']==1
    assert result['bounded_usage']=={'search':3,'replay':2}
    assert backend.replays==2
    audit=json.loads(service.artifacts.get(result['bounded_audit_artifact']))
    assert audit['selection']['id']==result['selected_hypothesis_id']
    assert json.loads(service.artifacts.get(result['lemma_use_audit_artifact']))['used']
    assert service.bounded.ledger.owner(proof) is None
    assert service.store.verify_chain(proof)==service.store.head(proof)[0]


def test_procedural_research_is_not_promoted_into_a_mathematical_candidate(bounded_case):
    service,proof,llm,backend,request=bounded_case
    move=candidate('Review the definitions');move.update(candidate_kind='research-move',formalization_spec=None)
    llm.responses[2]={'candidates':[move]}
    result=run(bounded_case)
    assert not result['formal_verified'] and result['reason']=='MATHEMATICAL_CANDIDATE_REQUIRED'
    assert result['candidate_attempts']==0 and backend.replays==0 and len(llm.history)==3
    assert service.bounded.ledger.owner(proof) is None


def test_cumulative_limit_stops_before_a_second_search(bounded_case):
    service,proof,llm,backend,request=bounded_case
    policy=json.loads(request['arguments']['policy']);policy['total_search_steps']=1
    request['arguments']['policy']=json.dumps(policy)
    result=run(bounded_case)
    assert result['reason']=='CUMULATIVE_BUDGET_EXHAUSTED' and not result['formal_verified']
    assert result['bounded_usage']=={'search':1,'replay':0} and backend.replays==0
    assert service.bounded.ledger.owner(proof) is None


def test_a_lost_replay_response_retains_reservation_and_never_replays_again(bounded_case,monkeypatch):
    service,proof,llm,backend,request=bounded_case
    from mathproof.maths_ai_http import MathsAIHTTPFormalATP
    original=MathsAIHTTPFormalATP._call
    lost=[]
    def lose(client,method,path,*args,**kwargs):
        result=original(client,method,path,*args,**kwargs)
        if path=='/formal-replay' and not lost:
            lost.append(True);raise OSError('lost completed native replay response')
        return result
    monkeypatch.setattr(MathsAIHTTPFormalATP,'_call',lose)
    with pytest.raises(ReplayReconciliationRequired):run(bounded_case)
    assert backend.replays==1
    task=service.bounded.ledger.owner(proof)
    assert service.bounded.ledger.usage(task)=={'search':6,'replay':5}
    with pytest.raises(ReplayReconciliationRequired):run(bounded_case)
    assert backend.replays==1 and not service._status(proof)['formal_verified']


def test_an_expired_lease_does_not_authorize_work_over_an_uncertain_earlier_replay(bounded_case):
    service,proof,llm,backend,request=bounded_case
    old,_=service.jobs.submit({'request_id':'earlier','workspace':'reference','operation':'formal-search','arguments':{}})
    service.jobs.save_formal(old['operation_id'],{'proof_id':proof,'phase':'replay-started'})
    service.jobs.update(old['operation_id'],{'phase':'failed'})
    with pytest.raises(BoundedBlocked,match='earlier replay'):run(bounded_case)
    assert not llm.history and backend.replays==0
    assert service.bounded.ledger.owner(proof) is None


def test_a_definite_formalization_refusal_finishes_without_holding_the_proof(bounded_case):
    service,proof,llm,backend,request=bounded_case
    llm.responses[0]='This is not a formalization.'
    result=run(bounded_case)
    assert result['reason']=='FORMALIZATION_NEEDS_CLARIFICATION'
    assert not result['formal_verified'] and result['bounded_usage']=={'search':0,'replay':0}
    assert len(llm.history)==1 and backend.replays==0
    assert service.bounded.ledger.owner(proof) is None


def test_an_uncertain_formalizer_response_is_not_repeated_or_refunded(bounded_case,monkeypatch):
    service,proof,llm,backend,request=bounded_case
    original=llm.generate
    def lose(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('lost model response')
    with monkeypatch.context() as patch:
        patch.setattr(llm,'generate',lose)
        with pytest.raises(RuntimeError,match='lost model response'):run(bounded_case)
    with pytest.raises(BoundedBlocked,match='response delivery is uncertain'):run(bounded_case)
    task=service.bounded.ledger.owner(proof)
    assert task and service.bounded.ledger.usage(task)=={'search':1,'replay':5}
    assert len(llm.history)==1 and backend.replays==0


@pytest.mark.parametrize('stage',['initial','research','formalize','verify','refine'])
@pytest.mark.parametrize('stop_kind',['cancel','deadline'])
def test_a_reserved_stage_does_not_start_model_work_after_task_stop(bounded_case,monkeypatch,stage,stop_kind):
    import time
    service,proof,llm,backend,request=bounded_case
    original=BoundedLedger.reserve
    fired=[]
    def reserve(ledger,task,name,*args):
        value=original(ledger,task,name,*args)
        if name==stage and not fired:
            fired.append(True);raise SystemExit('reserved before dispatch')
        return value
    with monkeypatch.context() as patch:
        patch.setattr(BoundedLedger,'reserve',reserve)
        with pytest.raises(SystemExit):run(bounded_case)
    history=len(llm.history)
    task=service.bounded.ledger.owner(proof);args=request['arguments']
    deadline=datetime.fromisoformat(args['deadline']).timestamp()
    if stop_kind=='deadline':monkeypatch.setattr(time,'time',lambda:deadline+1)
    result=service.bounded.run(task,proof,json.loads(args['policy']),
        expected_revision=int(args['expected_revision']),theorem_hash=args['expected_theorem_hash'],
        deadline=deadline,cancelled=lambda:stop_kind=='cancel')
    assert result['lifecycle']=='cancelled'
    assert result['reason']==('TASK_CANCELLED' if stop_kind=='cancel' else 'TASK_DEADLINE_REACHED')
    assert len(llm.history)==history and backend.replays==(1 if stage=='refine' else 0)
    assert service.bounded.ledger.owner(proof) is None


@pytest.mark.parametrize('boundary',['selection','formalization-checkpoint','verify-before-settle','verify-settled','parent-settled','audit-commit'])
def test_bounded_restart_recovers_without_duplicate_mathematical_dispatch(bounded_case,monkeypatch,boundary):
    service,proof,llm,backend,request=bounded_case
    update=BoundedLedger.update;settle=BoundedLedger.settle;commit=service.gate.commit
    fired=[]
    def stop():
        if not fired:fired.append(True);raise SystemExit(boundary)
    def saved(ledger,task,**patch):
        value=update(ledger,task,**patch)
        if boundary=='selection' and patch.get('phase')=='formalize':stop()
        if boundary=='formalization-checkpoint' and 'formalization_result' in patch:stop()
        return value
    def settled(ledger,task,name,*args):
        if boundary=='verify-before-settle' and name=='verify':stop()
        value=settle(ledger,task,name,*args)
        if boundary=='verify-settled' and name=='verify':stop()
        if boundary=='parent-settled' and name=='refine':stop()
        return value
    def committed(proposal):
        result=commit(proposal)
        if boundary=='audit-commit' and result.accepted and any(
                dict(getattr(op,'fields',())).get('format')=='persistent-atp-bounded-refinement-v1' for op in proposal.ops):stop()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(BoundedLedger,'update',saved);patch.setattr(BoundedLedger,'settle',settled)
        patch.setattr(service.gate,'commit',committed)
        with pytest.raises(SystemExit):run(bounded_case)
    assert fired
    replays=backend.replays;history=len(llm.history)
    from omegaclaw.service import OperationService
    formal,intake=service.formal.config,service.intake.config
    root=service.root;service.close()
    restored=OperationService(root,formal_config=formal,intake_config=intake)
    try:
        result=run((restored,proof,llm,backend,request))
        assert result['formal_verified'],result
        assert backend.replays==2
        if boundary in {'verify-before-settle','verify-settled','parent-settled','audit-commit'}:
            assert len(llm.history)<=history+2
        if boundary in {'parent-settled','audit-commit'}:assert backend.replays==replays
        assert restored.bounded.ledger.owner(proof) is None
        assert len(restored.view.list_nodes(proof,'Claim'))==2
    finally:restored.close()


@pytest.mark.parametrize('parent_uses_lemma',[True,False])
def test_managed_omega_task_uses_public_receiver_and_reports_verified_loop(bounded_case,tmp_path,monkeypatch,parent_uses_lemma):
    import time
    from omegaclaw.tests.test_formal_v2_receiver import checked_receiver
    from omegaclaw.frameworks.omega import OmegaAdapter
    from persistent_atp_client.adapters.local import LocalReceiverAdapter
    from omegaclaw.tests.test_task_workflow import SPEC
    service,proof,llm,backend,request=bounded_case
    if not parent_uses_lemma:ignore_parent_lemma(backend,monkeypatch)
    receiver=checked_receiver(service)
    adapter=OmegaAdapter(LocalReceiverAdapter(receiver),workspace='reference',cache_path=tmp_path/'omega.json')
    try:
        ref=request['arguments']['expected_theorem_hash']
        adapter.client.put_artifact(service.artifacts.get(ref),workspace_id='reference',
            media_type='application/json',expected_hash=ref)
        identity=adapter.tasks.accept('native-channel','bounded-loop',{'spec':SPEC,'proof_id':proof,
            'expected_revision':int(request['arguments']['expected_revision']),
            'expected_theorem_hash':request['arguments']['expected_theorem_hash'],
            'max_cycles':2,'max_research':1,'timeout_seconds':120,'refinement_policy':'bounded-v1',
            'refinement_budget':json.loads(request['arguments']['policy'])})
        until=time.monotonic()+30
        while time.monotonic()<until:
            adapter.tasks.tick(identity)
            task=adapter.state.snapshot()['tasks'][identity]
            if task['phase']=='delivery':break
            time.sleep(.02)
        assert task['phase']=='delivery',task
        report=json.loads(task['delivery']['text'].split('\n',1)[1])
        assert report['formal_verified'] is parent_uses_lemma,(report['reason'],report['errors'])
        assert report['reason']==('TARGET_VERIFIED' if parent_uses_lemma else 'VERIFIED_LEMMA_NOT_USED')
        assert report['cycles_used']==2 and report['research_used']==1
        assert report['cycle']['bounded_usage']=={'search':3,'replay':2}
        assert not report['execution_may_continue'] and backend.replays==2
        from persistent_atp_client import ArtifactRef
        audit=json.loads(adapter.client.get_artifact(ArtifactRef(report['cycle']['lemma_use_audit_artifact']),
            workspace_id='reference'))
        assert audit['used'] is parent_uses_lemma
        assert service.bounded.ledger.owner(proof) is None
    finally:adapter.close();receiver.close()


def test_known_lemma_usage_refusal_survives_restart_before_settlement(bounded_case,monkeypatch):
    service,proof,llm,backend,request=bounded_case
    ignore_parent_lemma(backend,monkeypatch)
    original=BoundedLedger.settle
    def interrupt(ledger,task,name,*args):
        if name=='refine':raise SystemExit('after known usage refusal')
        return original(ledger,task,name,*args)
    with monkeypatch.context() as patch:
        patch.setattr(BoundedLedger,'settle',interrupt)
        with pytest.raises(SystemExit):run(bounded_case)
    assert backend.replays==2
    from omegaclaw.service import OperationService
    formal,intake=service.formal.config,service.intake.config
    root=service.root;service.close()
    restored=OperationService(root,formal_config=formal,intake_config=intake)
    try:
        result=run((restored,proof,llm,backend,request))
        assert result['reason']=='VERIFIED_LEMMA_NOT_USED' and not result['formal_verified']
        assert result['bounded_usage']=={'search':3,'replay':2} and backend.replays==2
        assert restored.bounded.ledger.owner(proof) is None
        assert result['lemma_use_audit_artifact'] in restored._artifact_pointers(proof)
    finally:restored.close()
