"""One receiver-owned, bounded refinement loop. All truth comes from the gate."""
from copy import deepcopy
import json
import time

from commit_gate.canon import canonical_json,content_hash
from commit_gate.ops import AddEdge,UpsertNode
from commit_gate.proposal import Proposal
from persistent_atp_client.codecs import to_dict
from persistent_atp_client.models import refinement_policy_from_dict

from .bounded_ledger import BoundedLedger,BoundedBlocked
from .candidates import formalization_spec
from .contracts import ContractError
from .formal_runtime import LemmaUsageRejected


class BoundedRefinementWorkflow:
    def __init__(self,service):
        self.service=service
        self.ledger=BoundedLedger(service.jobs)

    def _set(self,task,**patch):
        with self.service.lock:return self.ledger.update(task,**patch)

    def validate_child_history(self,child):
        with self.service.lock:
            task=self.ledger.child_owner(child)
            if not task:raise BoundedBlocked('child has no bounded task reservation')
            return self.validate_history(task)

    def validate_history(self,task):
        service=self.service;state=self.ledger.state(task);proof=state['proof_id']
        service.catch_up(proof)
        if self.ledger.owner(proof)!=task:raise BoundedBlocked('bounded proof ownership changed')
        target=service.view.node(proof+'/proof')
        if target is None or target.fields.get('target_artifact')!=state['request']['theorem_hash']:
            raise BoundedBlocked('bounded literal theorem identity changed')
        prepared=self.ledger.prepared(task)
        if any(event not in prepared for _,event in service.store.read_events_after(proof,state['last_revision'])):
            raise BoundedBlocked('proof changed outside the bounded workflow')
        return state

    def _cost(self,child,row,outcome):
        jobs=self.service.jobs
        intake=jobs.intake_state(child)
        research=jobs.research_state(child)
        if (intake.get('bounded_draft_started') and 'candidate' not in intake and 'clarification' not in intake
                or intake.get('bounded_review_started') and 'review' not in intake
                or research.get('bounded_generation_started') and 'response' not in research):
            raise BoundedBlocked('original model response delivery is uncertain')
        state=self.service.jobs.formal_state(child)
        if not state:return 0,0  # A prepared formal request is always saved before provider dispatch.
        result=(state.get('binding',{}).get('snapshot') or {}).get('result')
        if result is None:raise BoundedBlocked('original child search is unsettled')
        accounting=outcome.get('budget') or {}
        used=accounting.get('consumed_steps')
        search=used if accounting.get('consumed_steps_exact') is True and type(used) is int else row['search_cap']
        receipt=state.get('receipt')
        if receipt:
            cost=receipt.get('verification_cost') or {};used=cost.get('tactic_steps')
            replay=used if type(used) is int else row['replay_cap']
        elif state.get('phase')=='replay-started':
            raise BoundedBlocked('original replay receipt delivery is uncertain')
        else:replay=0
        return search,replay

    def _require_quiescent(self,task,proof):
        """An expired lease never establishes that earlier native work stopped."""
        service=self.service
        for row in service.jobs.conn.execute('SELECT * FROM operations'):
            if row['operation_id']==task:continue
            formal=json.loads(row['formal_json']) if row['formal_json'] else {}
            if formal.get('proof_id')==proof and formal.get('phase')!='completed':
                if formal.get('phase')=='replay-started' and 'receipt' not in formal:
                    raise BoundedBlocked('earlier replay requires receipt reconciliation')
                result=(formal.get('binding',{}).get('snapshot') or {}).get('result') or {}
                committed=service.view.node(formal.get('run_id'))
                if (result.get('disposition') not in {'stagnated','budget-exhausted','invalid-request','cancelled','counterexample'}
                        or committed is None or not committed.fields.get('provider_result_artifact')):
                    raise BoundedBlocked('earlier formal execution requires reconciliation')
            states=[json.loads(row[key]) for key in
                ('response','formal_json','cycle_json','intake_json','research_json','refinement_json','bounded_json') if row[key]]
            if row['phase'] not in {'finished','failed'} and any(
                    item.get('proof_id',item.get('identity',{}).get('proof_id'))==proof for item in states):
                raise BoundedBlocked('earlier proof operation is unsettled')

    def _execute(self,task,name,kind,args,search,replay,fn):
        service=self.service
        with service.lock:
            state=self.validate_history(task)
            identity=content_hash(['bounded-step',task,name])[7:]
            request={'schema_version':1,'request_id':identity,'workspace':service.workspace,
                'operation':kind,'arguments':args}
            previous=self.ledger.step(task,name)
            if previous:request=json.loads(previous['request_body'])
            job,_=service.jobs.submit(request)
            if previous:job.setdefault('operation_id',previous['child'])
            try:row=self.ledger.reserve(task,name,job['operation_id'],request,search,replay)
            except BoundedBlocked as error:
                if str(error)=='CUMULATIVE_BUDGET_EXHAUSTED':
                    service.jobs.update(job['operation_id'],{'phase':'finished','lifecycle':'cancelled',
                        'reason':'CUMULATIVE_BUDGET_EXHAUSTED','execution_settled':True,'operation_id':job['operation_id']})
                raise
            if row['status']=='settled':return json.loads(row['outcome'])
            child=job['operation_id']
        if job['phase'] in {'finished','failed'}:
            if job['phase']=='failed':raise BoundedBlocked('original child operation requires reconciliation')
            outcome=job
            # Operational wrappers add fields after the mathematical workflow result.
            outcome={key:value for key,value in outcome.items() if key not in {'transport_status','operation_id'}}
        else:
            try:
                with service.gate.scope(child):outcome=fn(child,state)
            except LemmaUsageRejected as error:
                # A complete, bound native audit is a known policy refusal.
                # It cannot promote the parent, and needs no second replay.
                with service.lock:
                    formal=service.jobs.formal_state(child)
                    formal['phase']='completed'
                    service.jobs.save_formal(child,formal)
                    outcome={**service.formal._report(formal),'reason':error.code}
                    refinement=service.jobs.refinement_state(child)
                    if refinement:
                        outcome.update(refinement_artifact=refinement['lineage_artifact'],
                            previous_declaration_id=refinement['request']['previous_declaration_id'])
                        refinement.update(phase='completed',outcome=outcome)
                        service.jobs.save_refinement(child,refinement)
            with service.lock:service.jobs.update(child,{**outcome,'phase':'finished','operation_id':child})
        with service.lock:
            self.validate_history(task)
            used_search,used_replay=self._cost(child,row,outcome)
            self.ledger.settle(task,name,outcome,used_search,used_replay)
        return outcome

    def _checkpoint(self,task,**patch):
        with self.service.lock:
            state=self.validate_history(task)
            patch['last_revision']=self.service.store.head(state['proof_id'])[0]
            return self.ledger.update(task,**patch)

    def _finish(self,task,reason):
        service=self.service
        with service.lock:
            state=self.validate_history(task);proof=state['proof_id']
            steps=[dict(row) for row in service.jobs.conn.execute('SELECT * FROM bounded_steps WHERE task=? ORDER BY rowid',(task,))
                   if row['step']!='report']
            report={'format':'persistent-atp-bounded-refinement-v1','task_operation_id':task,
                'proof_id':proof,'policy':state['request']['policy'],'reason':reason,
                'selection':state.get('selection'),'selection_exclusions':state.get('selection_exclusions',[]),
                'usage':self.ledger.usage(task),'parent_result':state.get('parent_result'),
                'steps':[{key:row[key] for key in ('step','child','body_hash','search_cap','replay_cap','search_used','replay_used','status')} for row in steps]}
            ref=service.artifacts.put(canonical_json(report))
        def publish(child,_state):
            with service.lock:
                if service.view.node(ref) is None:
                    lease=service.store.issue_lease(proof,proof+'/bounded-report-'+content_hash(task)[7:],
                        actor='omega-coordinator',worker_class='coordinator',selected_move_id=proof+'/c-1')
                    try:
                        ops=[UpsertNode('Artifact',ref,{'artifact_hash':ref,'format':report['format'],'media_type':'application/json'}),
                            AddEdge('HAS_ARTIFACT',proof+'/c-1',ref,proof+'/bounded-audit-'+content_hash(task)[7:])]
                        usage_audit=(state.get('parent_result') or state.get('initial_result') or {}).get('lemma_use_audit_artifact')
                        if usage_audit and service.view.node(usage_audit) is None:
                            ops.extend((UpsertNode('Artifact',usage_audit,{'artifact_hash':usage_audit,
                                'format':'persistent-atp-lemma-use-audit-v1','media_type':'application/json'}),
                                AddEdge('HAS_ARTIFACT',proof+'/c-1',usage_audit,proof+'/bounded-lemma-audit-'+content_hash(task)[7:])))
                        proposal=Proposal(proof,lease.actor,lease.worker_class,
                            ops=tuple(ops),
                            base_revision=lease.base_revision,lease_id=lease.lease_id,fencing_token=lease.fencing_token,
                            selected_move_id=lease.selected_move_id)
                        accepted=service.gate.commit(proposal)
                        if not accepted.accepted:raise BoundedBlocked('gate rejected bounded task audit')
                        service.catch_up(proof)
                    finally:service.store.release_lease(proof,lease.lease_id,fencing_token=lease.fencing_token)
            return {'bounded_audit_artifact':ref}
        self._execute(task,'report','proof-audit',{'proof':proof},0,0,publish)
        with service.lock:
            status=service._status(proof)
            usage=self.ledger.usage(task)
            result={**status,'phase':'completed','lifecycle':'cancelled' if reason in {'TASK_CANCELLED','TASK_DEADLINE_REACHED'} else 'succeeded','reason':reason,
                'claim_id':proof+'/c-1',
                'component_mode':'bounded-refinement-v1','bounded_audit_artifact':ref,
                'bounded_usage':usage,'cycles_used':sum(row['step'] in {'initial','refine'} for row in steps),
                'research_used':sum(row['step']=='research' for row in steps),
                'candidate_attempts':sum(row['step']=='verify' for row in steps),
                'selected_hypothesis_id':(state.get('selection') or {}).get('id'),
                'parent_result':state.get('parent_result'),'initial_result':state.get('initial_result')}
            for key in ('lemma_use_audit_artifact','refinement_artifact'):
                attempt=state.get('parent_result') or state.get('initial_result') or {}
                if attempt.get(key):result[key]=attempt[key]
            self.ledger.finish(task,result)
        return result

    def run(self,task,proof,policy,*,expected_revision,theorem_hash,deadline,cancelled=lambda:False,on_progress=None):
        service=self.service
        policy_obj=refinement_policy_from_dict(policy)
        request={'proof_id':proof,'policy':policy,'expected_revision':expected_revision,
            'theorem_hash':theorem_hash,'deadline':deadline}
        with service.lock:
            state=self.ledger.state(task)
            if state:
                if (state['request']!=request or state['config_hash']!=content_hash([
                        service.intake.config.__dict__,service.formal.config.__dict__])):
                    raise BoundedBlocked('bounded workflow request or configuration changed')
                if state.get('outcome'):return state['outcome']
            else:
                if service.formal.config.mode!='real' or service.intake.config.lemma_reuse=='disabled':
                    raise BoundedBlocked('bounded refinement requires real verification and enabled lemma reuse')
                service.catch_up(proof)
                if service.store.head(proof)[0]!=expected_revision:
                    raise BoundedBlocked('initial bounded revision changed')
                if service.store.active_leases(proof):raise BoundedBlocked('proof already has active work')
                self._require_quiescent(task,proof)
                state=self.ledger.start(task,{'proof_id':proof,'request':request,'phase':'initial',
                    'last_revision':expected_revision,'config_hash':content_hash([
                        service.intake.config.__dict__,service.formal.config.__dict__])})
        stopped=lambda:cancelled() or time.time()>=deadline
        try:
            while True:
                with service.lock:
                    state=self.validate_history(task)
                phase=state['phase']
                if on_progress:on_progress({'proof_id':proof,'phase':'bounded-'+phase,
                    'lifecycle':'running','bounded_usage':self.ledger.usage(task),
                    'cycles_used':sum(self.ledger.step(task,name) is not None for name in ('initial','refine')),
                    'research_used':int(self.ledger.step(task,'research') is not None)})
                if stopped() and not self.ledger.step(task,phase):return self._finish(task,'TASK_DEADLINE_REACHED' if not cancelled() else 'TASK_CANCELLED')
                replay=to_dict(policy_obj.replay)
                if phase=='initial':
                    budget=to_dict(policy_obj.initial)
                    outcome=self._execute(task,phase,'proof-cycle',{'proof':proof},budget['max_steps'],replay['max_steps'],
                        lambda child,s:service.cycles.run(child,proof,budget=budget,replay_budget=replay,
                            expected_revision=s['last_revision'],cancelled=stopped))
                    self._checkpoint(task,initial_result=outcome)
                    if service._status(proof)['formal_verified']:return self._finish(task,'TARGET_VERIFIED')
                    if stopped():return self._finish(task,'TASK_CANCELLED' if cancelled() else 'TASK_DEADLINE_REACHED')
                    if outcome.get('mathematical_disposition') not in {'stagnated','budget-exhausted'}:
                        return self._finish(task,outcome.get('reason') or outcome.get('error_code') or 'INITIAL_ATTEMPT_UNVERIFIED')
                    if policy_obj.max_parent_attempts<2 or not policy_obj.max_research:
                        return self._finish(task,'REFINEMENT_ALLOWANCE_EXHAUSTED')
                    contexts=outcome.get('obstruction_contexts') or []
                    if not contexts:return self._finish(task,'RESEARCH_CONTEXT_UNAVAILABLE')
                    self._checkpoint(task,phase='research',obstruction_id=contexts[0]['obstruction_id'],
                        previous_declaration_id=outcome['selected_move_id'])
                elif phase=='research':
                    obstruction=state['obstruction_id']
                    outcome=self._execute(task,phase,'lemma-bridge',{'proof':proof,'obstruction':obstruction},0,0,
                        lambda child,s:service.research.run(proof,obstruction,child,cancelled=stopped))
                    self._checkpoint(task,phase='select',research_result=outcome)
                    if stopped():return self._finish(task,'TASK_CANCELLED' if cancelled() else 'TASK_DEADLINE_REACHED')
                elif phase=='select':
                    exclusions=[];selection=None
                    for identity in state['research_result']['hypothesis_ids']:
                        node=service.view.node(identity)
                        try:
                            if node is None or node.label!='SpeculativeHypothesis':raise ContractError('candidate record unavailable')
                            if state['obstruction_id'] not in node.fields['source_ids'] or proof+'/c-1' not in node.fields['source_ids']:
                                raise ContractError('candidate source binding differs')
                            if formalization_spec(dict(node.fields)) is None:raise ContractError('procedural research move')
                        except (ValueError,KeyError) as error:
                            exclusions.append({'id':identity,'reason':str(error)});continue
                        selection={'id':identity,'fields':deepcopy(dict(node.fields)),
                            'reason':'first eligible complete mathematical candidate in committed research result order'}
                        break
                    self._checkpoint(task,selection=selection,selection_exclusions=exclusions)
                    if selection is None:return self._finish(task,'MATHEMATICAL_CANDIDATE_REQUIRED')
                    self._checkpoint(task,phase='formalize')
                elif phase=='formalize':
                    selected=state['selection'];node=service.view.node(selected['id'])
                    if node is None or dict(node.fields)!=selected['fields']:raise BoundedBlocked('selected candidate changed')
                    outcome=self._execute(task,phase,'hypothesis-formalize',{'proof':proof,'hypothesis':selected['id'],
                        'expected_revision':str(state['last_revision'])},0,0,
                        lambda child,s:service._formalize_hypothesis(proof,selected['id'],child,
                            expected_revision=str(s['last_revision']),cancelled=stopped))
                    self._checkpoint(task,formalization_result=outcome)
                    if stopped():return self._finish(task,'TASK_CANCELLED' if cancelled() else 'TASK_DEADLINE_REACHED')
                    if not outcome.get('formalization_ready'):return self._finish(task,outcome.get('error_code') or 'CANDIDATE_REVIEW_REQUIRED')
                    self._checkpoint(task,phase='verify',candidate_claim_id=outcome['claim_id'])
                elif phase=='verify':
                    budget=to_dict(policy_obj.candidate);claim=state['candidate_claim_id']
                    outcome=self._execute(task,phase,'formal-search',{'proof':proof,'claim':claim,'budget':json.dumps(budget)},
                        budget['max_steps'],replay['max_steps'],lambda child,s:service.formal.run(child,proof,claim,budget,
                            expected_revision=s['last_revision'],replay_budget=replay,cancelled=stopped))
                    self._checkpoint(task,candidate_result=outcome)
                    if stopped():return self._finish(task,'TASK_CANCELLED' if cancelled() else 'TASK_DEADLINE_REACHED')
                    if outcome.get('mathematical_disposition')!='lean-verified':return self._finish(task,'CANDIDATE_NOT_VERIFIED')
                    self._checkpoint(task,phase='refine')
                elif phase=='refine':
                    budget=to_dict(policy_obj.parent)
                    args={'proof':proof,'previous_declaration':state['previous_declaration_id'],
                        'lemma':state['candidate_claim_id'],'budget':json.dumps(budget),
                        'expected_revision':str(state['last_revision']),'expected_theorem_hash':theorem_hash}
                    outcome=self._execute(task,phase,'parent-refine',args,budget['max_steps'],replay['max_steps'],
                        lambda child,s:service.refinement.run(child,proof,s['previous_declaration_id'],s['candidate_claim_id'],budget,
                            expected_revision=s['last_revision'],expected_theorem_hash=theorem_hash,deadline=deadline,
                            replay_budget=replay,cancelled=stopped))
                    self._checkpoint(task,parent_result=outcome)
                    return self._finish(task,'TARGET_VERIFIED' if service._status(proof)['formal_verified']
                        else 'TASK_CANCELLED' if cancelled() else 'TASK_DEADLINE_REACHED' if stopped()
                        else outcome.get('reason') or outcome.get('error_code') or 'PARENT_REMAINS_UNVERIFIED')
                else:raise BoundedBlocked('unknown saved bounded workflow phase')
        except BoundedBlocked as error:
            if str(error)=='CUMULATIVE_BUDGET_EXHAUSTED':return self._finish(task,str(error))
            self._set(task,blocked_reason=str(error))
            raise
