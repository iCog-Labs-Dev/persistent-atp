"""Controlled tactic proposals, real Lean replay/usage audits and real gate writes."""
from dataclasses import replace
import json
import subprocess

import pytest

from commit_gate.ops import AddEdge, SetField
from omegaclaw.formal_runtime import FormalV2Error, FormalWorkflow
from omegaclaw.tests.test_formal_v2_receiver import provider, _TEST_LEAN
from omegaclaw.tests.test_hypothesis_recovery import HypothesisCase
from omegaclaw.tests.test_production_intake import CANDIDATE, REVIEW
from maths_ai.formal.backends import AttemptOutcome, VerifyOutcome


@pytest.fixture
def reusable_case(provider, tmp_path, monkeypatch):
    if _TEST_LEAN is None:
        pytest.skip('an installed native Lean executable is required')
    backend = provider[1]
    backend.use_bridge = False
    async def elaborate(payload, **_kwargs):
        body, name = payload['body'], payload['declaration']
        start = body.rfind('theorem ' + name + ' :')
        assert start >= 0
        header = body[start:].split(' := by', 1)[0]
        return {'target_sexp':'(:c True)','target_pp':'True','locals':[], 'target_unit_start':start,
            'check':{'ok':True}, 'native_target':{'header':header,'prefix':body[:start],'declaration':name}}
    async def search(task, **_kwargs):
        command = 'exact bridge' if backend.use_bridge else 'trivial'
        trace = {'goal':task.statement['expression'],'tactic':command,'arguments':[],'subgoals':[]}
        certificate = {'format':'maths-ai-accepted-certificate-v1','statement':task.statement,
            'native_target':task.manifest['native_target'],'steps':[command],'proof_trace':trace}
        return AttemptOutcome('certified',True,1,trace,certificate)
    async def verify(certificate, **_kwargs):
        backend.replays += 1
        target = certificate['native_target']
        commands = '\n'.join('  focus\n'+'\n'.join('    '+line for line in step.splitlines())
                             for step in certificate['steps'])
        source = ('import Lean\nimport Init\n' + target['prefix'] + '\n' + target['header'] + ' := by\n'
            + commands + '\n\n#print axioms ' + target['declaration'] + '\n')
        path = tmp_path/'Replay.lean'; path.write_text(source)
        result = subprocess.run([str(_TEST_LEAN),str(path)],capture_output=True,text=True,timeout=15)
        output = result.stdout + result.stderr
        passed = result.returncode == 0 and 'sorryAx' not in output and 'does not depend on any axioms' in output
        return VerifyOutcome('verified' if passed else 'rejected',None,1,backend.environment_manifest(),
            {'sorry_detected':not passed,'session':'fresh-native-compilation',
             'axiom_audit':{'status':'passed' if passed else 'lean_rejected','source':source,
                           'exit_code':result.returncode,'forbidden_axioms':[],'diagnostics':output}})
    monkeypatch.setattr(backend,'elaborate_source',elaborate)
    monkeypatch.setattr(backend,'run',search)
    monkeypatch.setattr(backend,'verify',verify)
    case = HypothesisCase(tmp_path/'receiver',monkeypatch)
    case.service.formal.config = replace(case.service.formal.config,mode='real',reuse_lean=str(_TEST_LEAN))
    case.llm.responses = [CANDIDATE|{'lean_name':'bridge'},REVIEW]
    try:
        candidate = case.run()
        lemma = candidate['claim_id']
        job,_ = case.service.jobs.submit({'schema_version':1,'request_id':'verify-lemma','workspace':'reference',
            'operation':'formal-search','arguments':{'proof':case.proof,'claim':lemma,
                                                  'budget':json.dumps({'max_steps':5,'wall_clock_ms':1000})}})
        outcome = case.service.formal.run(job['operation_id'],case.proof,lemma,{'max_steps':5,'wall_clock_ms':1000})
        assert outcome['mathematical_disposition']=='lean-verified',outcome
        assert case.service.view.node(case.claim).fields['status']=='provisional'
        case.commit([AddEdge('DEPENDS_ON',case.claim,lemma,case.proof+'/selected-verified-lemma')])
        case.service.intake.config = replace(case.service.intake.config,lemma_reuse='source-prefix')
        case.llm.responses = [CANDIDATE|{'lean_name':'parent'},REVIEW]
        parent_job,_ = case.service.jobs.submit({'schema_version':1,'request_id':'parent','workspace':'reference',
            'operation':'proof-cycle','arguments':{'proof':case.proof}})
        yield case,backend,parent_job['operation_id']
    finally:
        case.service.close()


@pytest.mark.parametrize('uses_lemma',[False,True])
def test_verified_candidate_must_be_used_before_parent_promotion(reusable_case, uses_lemma):
    case,backend,operation = reusable_case
    backend.use_bridge = uses_lemma
    if uses_lemma:
        result = case.service.cycles.run(operation,case.proof)
        assert result['mathematical_disposition']=='lean-verified',result
        saved = case.service.jobs.intake_state(operation)
        context = json.loads(case.service.artifacts.get(saved['packet_artifact']))
        assert context['verified_premises']==[{key:saved['verified_lemma'][key]
            for key in ('claim_id','lean_name','lean_type','certificate_id')}]
        assert case.service._status(case.proof)['formal_verified']
        audit = case.service.jobs.formal_state(operation)['lemma_use_audit_artifact']
        assert json.loads(case.service.artifacts.get(audit))['used']
        assert audit in case.service._artifact_pointers(case.proof)
        assert case.service._export(case.proof,'default')['audit_status']=='ok'
    else:
        with pytest.raises(FormalV2Error,match='did not use'):
            case.service.cycles.run(operation,case.proof)
        assert case.service.view.node(case.claim).fields['status']=='provisional'
        assert not case.service._status(case.proof)['formal_verified']
    assert case.service.store.verify_chain(case.proof)==case.service.store.head(case.proof)[0]


def test_parent_recovers_after_durable_usage_audit_without_repeating_search_or_replay(reusable_case,provider,monkeypatch):
    case,backend,operation = reusable_case
    backend.use_bridge = True
    save = FormalWorkflow._save
    def interrupt(workflow,op,state):
        save(workflow,op,state)
        if op==operation and state.get('lemma_use_audit_artifact'):
            raise SystemExit('saved usage audit before parent promotion')
    with monkeypatch.context() as patch:
        patch.setattr(FormalWorkflow,'_save',interrupt)
        with pytest.raises(SystemExit):case.service.cycles.run(operation,case.proof)
    assert case.service.view.node(case.claim).fields['status']=='provisional'
    starts = sum(path=='/formal-search/start' and method=='POST' for method,path in provider[2])
    replays = backend.replays
    auditor = case.service.formal.config.reuse_lean
    case.restart()
    case.service.formal.config = replace(case.service.formal.config,mode='real',reuse_lean=auditor)
    case.service.intake.config = replace(case.service.intake.config,lemma_reuse='source-prefix')
    def repeated_audit(*_args,**_kwargs):raise AssertionError('saved audit must be reused')
    monkeypatch.setattr('omegaclaw.lemma_reuse.audit_parent_usage',repeated_audit)
    result = case.service.cycles.run(operation,case.proof)
    assert result['mathematical_disposition']=='lean-verified'
    assert backend.replays==replays
    assert sum(path=='/formal-search/start' and method=='POST' for method,path in provider[2])==starts


def test_cancellation_during_usage_audit_does_not_promote_parent(reusable_case, monkeypatch):
    case,backend,operation = reusable_case
    backend.use_bridge = True
    cancelled = []
    audit = case.service.formal._audit_lemma_reuse
    def cancel_after_audit(*args):
        audit(*args)
        cancelled.append(True)
    monkeypatch.setattr(case.service.formal,'_audit_lemma_reuse',cancel_after_audit)
    result = case.service.cycles.run(operation,case.proof,cancelled=lambda:bool(cancelled))
    assert result['lifecycle']=='cancelled'
    assert case.service.view.node(case.claim).fields['status']=='provisional'
    assert not case.service._status(case.proof)['formal_verified']


def test_dependency_change_during_usage_audit_blocks_parent_promotion(reusable_case, monkeypatch):
    case,backend,operation = reusable_case
    backend.use_bridge = True
    audit = case.service.formal._audit_lemma_reuse
    def change_after_audit(*args):
        audit(*args)
        binding = case.service.jobs.intake_state(operation)['verified_lemma']
        case.commit([SetField('Claim',binding['claim_id'],'state_novelty',0.1)])
    monkeypatch.setattr(case.service.formal,'_audit_lemma_reuse',change_after_audit)
    with pytest.raises(ValueError,match='saved verified lemma changed'):
        case.service.cycles.run(operation,case.proof)
    assert case.service.view.node(case.claim).fields['status']=='provisional'


def test_missing_native_auditor_blocks_parent_search(reusable_case, provider):
    case,backend,operation = reusable_case
    case.service.formal.config = replace(case.service.formal.config,reuse_lean=None)
    starts = sum(path=='/formal-search/start' and method=='POST' for method,path in provider[2])
    with pytest.raises(FormalV2Error,match='installed native Lean auditor'):
        case.service.cycles.run(operation,case.proof)
    assert sum(path=='/formal-search/start' and method=='POST' for method,path in provider[2])==starts
    assert case.service.view.node(case.claim).fields['status']=='provisional'
