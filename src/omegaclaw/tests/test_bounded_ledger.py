"""Cumulative allowances and proof ownership survive separate ledger connections."""
import pytest

from omegaclaw.jobs import JobLedger
from omegaclaw.bounded_ledger import BoundedLedger,BoundedBlocked


def task(jobs,name='task'):
    job,_=jobs.submit({'request_id':name,'operation':'bounded-refine','workspace':'reference','arguments':{'proof':'p1'}})
    ledger=BoundedLedger(jobs);identity=job['operation_id']
    ledger.start(identity,{'proof_id':'p1','request':{'policy':{'total_search_steps':6,'total_replay_steps':4}}})
    return ledger,identity


def test_another_connection_cannot_reset_a_proof_budget(tmp_path):
    jobs=JobLedger(tmp_path/'jobs.sqlite3');ledger,identity=task(jobs)
    other=JobLedger(tmp_path/'jobs.sqlite3')
    try:
        with pytest.raises(BoundedBlocked,match='owns'):task(other,'second')
        ledger.reserve(identity,'initial','child',{'body':'original'},5,4)
        restored=BoundedLedger(other)
        assert restored.usage(identity)=={'search':5,'replay':4}
        with pytest.raises(BoundedBlocked,match='CUMULATIVE'):
            restored.reserve(identity,'verify','next',{'body':'next'},2,0)
        assert restored.step(identity,'verify') is None
        assert restored.reserve(identity,'initial','child',{'body':'original'},5,4)['child']=='child'
        with pytest.raises(BoundedBlocked,match='binding changed'):
            restored.reserve(identity,'initial','child',{'body':'changed'},5,4)
    finally:other.conn.close();jobs.conn.close()


def test_only_settled_evidence_releases_reserved_allowance(tmp_path):
    jobs=JobLedger(tmp_path/'jobs.sqlite3');ledger,identity=task(jobs)
    try:
        ledger.reserve(identity,'initial','child',{},5,4)
        with pytest.raises(BoundedBlocked,match='unsettled'):ledger.release(identity)
        with pytest.raises(BoundedBlocked,match='exceeds'):ledger.settle(identity,'initial',{},6,0)
        assert ledger.usage(identity)=={'search':5,'replay':4}
        ledger.settle(identity,'initial',{'settled':True},1,0)
        ledger.reserve(identity,'verify','next',{},5,4)
        assert ledger.usage(identity)=={'search':6,'replay':4}
        with pytest.raises(BoundedBlocked,match='evidence changed'):
            ledger.settle(identity,'initial',{'settled':False},0,0)
    finally:jobs.conn.close()


def test_unscoped_graph_write_is_rejected_before_commit(tmp_path):
    from omegaclaw.service import OperationService
    from omegaclaw.tests.test_task_workflow import SPEC
    from commit_gate.ops import UpsertNode
    from commit_gate.proposal import Proposal
    from commit_gate.reasons import Reason
    service=OperationService(tmp_path/'receiver')
    import json
    try:
        created=service.submit({'schema_version':1,'workspace':'reference','request_id':'create',
            'operation':'proof-create','arguments':{'spec':json.dumps(SPEC)}})
        proof=created['proof_id'];job,_=service.jobs.submit({'request_id':'owner','operation':'bounded-refine',
            'workspace':'reference','arguments':{'proof':proof}})
        ledger=BoundedLedger(service.jobs)
        ledger.start(job['operation_id'],{'proof_id':proof,'request':{'policy':{'total_search_steps':1,'total_replay_steps':1}}})
        lease=service.store.issue_lease(proof,proof+'/audit',actor='coordinator',worker_class='coordinator',selected_move_id=proof+'/c-1')
        ref=service.artifacts.put(b'bounded fixture')
        proposal=Proposal(proof,lease.actor,lease.worker_class,
            ops=(UpsertNode('Artifact',ref,{'artifact_hash':ref}),),base_revision=lease.base_revision,
            lease_id=lease.lease_id,fencing_token=lease.fencing_token,selected_move_id=lease.selected_move_id)
        denied=service.gate.commit(proposal)
        assert not denied.accepted and denied.rejections[0].reason==Reason.BOUNDED_TASK_OWNERSHIP
        assert service.store.head(proof)[0]==created['revision']
        ledger.reserve(job['operation_id'],'report','scoped-child',{},0,0)
        with service.gate.scope('scoped-child'):accepted=service.gate.commit(proposal)
        assert accepted.accepted,accepted.to_dict()
        assert ledger.prepared(job['operation_id'])==[proposal.to_dict()]
    finally:service.close()
