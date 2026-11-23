import json
from datetime import datetime,timezone,timedelta

import pytest

from omegaclaw.frameworks.omega import OmegaAdapter
from omegaclaw.tests.test_task_workflow import TaskPort,SPEC,finish
from persistent_atp_client import Budget,RefinementPolicy
from persistent_atp_client.codecs import to_dict


class BoundedTaskPort(TaskPort):
    def complete(self,ref,kind):
        if kind!='bounded-refine':return super().complete(ref,kind)
        from dataclasses import replace
        previous=self._operations[ref];proof=previous.proof_id
        self.proofs[proof]['verified']=True
        self.proofs[proof]['revision']+=3
        self._operations[ref]=replace(previous,lifecycle='succeeded',observation_version=1,
            result={'formal_verified':True,'reason':'TARGET_VERIFIED','cycles_used':2,'research_used':1,
                'execution_settled':True,'bounded_usage':{'search':3,'replay':2}})
        self._event(ref,'succeeded',1)


def test_new_bounded_task_uses_one_durable_receiver_workflow(tmp_path):
    port=BoundedTaskPort();adapter=OmegaAdapter(port,workspace='reference',cache_path=tmp_path/'state.json')
    try:
        identity=adapter.tasks.accept('channel','bounded',{'spec':SPEC,'max_cycles':2,'max_research':1,
            'refinement_policy':'bounded-v1'})
        done=finish(adapter,identity)
        assert done['cycles']==2 and done['research']==1,(done['reason'],done['errors'],done.get('cycle_result'))
        assert [r.action.kind for r in port.requests]==['proof-create','bounded-refine','proof-audit','proof-export']
        original=adapter.state.intent(done['steps']['bounded-refinement'])['request']
        assert original['contract_version']=='2.4'
        assert original['deadline']==datetime.fromtimestamp(done['deadline'],timezone.utc).isoformat().replace('+00:00','Z')
        report=json.loads(done['delivery']['text'].split('\n',1)[1])
        assert report['cycle']['bounded_usage']=={'search':3,'replay':2}
    finally:adapter.client.close()


def test_old_task_cache_does_not_silently_gain_refinement_allowances(tmp_path):
    port=BoundedTaskPort();adapter=OmegaAdapter(port,workspace='reference',cache_path=tmp_path/'state.json')
    try:
        identity=adapter.tasks.accept('channel','old',{'spec':SPEC})
        restored=OmegaAdapter(port,workspace='reference',cache_path=adapter.state.path)
        try:
            assert restored.tasks.accept('channel','old',{'spec':SPEC})==identity
            finish(restored,identity)
            assert 'bounded-refine' not in [r.action.kind for r in port.requests]
        finally:restored.client.close()
    finally:adapter.client.close()
