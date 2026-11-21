"""Scheduled formal cycles against the real provider service with a labelled backend fixture."""
import asyncio
import shutil
from types import SimpleNamespace

import pytest

from omegaclaw.tests.test_formal_v2_receiver import provider, configured
from omegaclaw.cycle_runtime import CycleWorkflow
from omegaclaw.formal_runtime import FormalWorkflow
from mathproof.maths_ai_http import MathsAIHTTPFormalATP
from persistent_atp_client import AsyncClient, CancelRequest, ProofCycle, SelectedProofCycle, Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import UnsupportedCapability


def job_for(service, request):
    return service.jobs.conn.execute('SELECT operation_id FROM operations WHERE request_id=?', (request.request_id,)).fetchone()[0]


@pytest.mark.parametrize('selected', [False, True])
def test_cycle_selects_declaration_and_preserves_fenced_provider_identity(provider, tmp_path, selected):
    deployment, backend, calls, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment)
    transport = MathsAIHTTPFormalATP._transport
    # Assert every network call, including status and cancellation, is outside
    # the service lock; status/event reads must remain possible during search.
    def unlocked(*args, **kwargs):
        assert not service.lock._is_owned()
        return transport(*args, **kwargs)
    original = MathsAIHTTPFormalATP._transport
    MathsAIHTTPFormalATP._transport = unlocked
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        action = SelectedProofCycle('p1', 'formal-run') if selected else ProofCycle('p1')
        request = client.prepare(action, workspace_id='reference', request_id='cycle',
            preconditions=Preconditions(expected_revision=service.store.head('p1')[0]))
        handle = await client.submit(request)
        result = await handle.wait(timeout=15, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
        assert result.result['cycle_disposition'] == 'formal-executed'
        assert result.result['selected_move_id'] == 'p1/fd-1'
        assert result.mathematical_disposition == 'proved-pending-replay'
        assert not (await client.status(workspace_id='reference', proof_id='p1')).formal_verified
        job = job_for(service, request)
        cycle, formal = service.jobs.cycle_state(job), service.jobs.formal_state(job)
        assert cycle['selection_lease']['selected_move_id'] == 'p1/fd-1'
        assert formal['request']['run_id'] == result.result['run_id']
        assert formal['request']['lease_id'] == formal['search_lease']['lease_id']
        assert formal['request']['fencing_token'] > cycle['selection_lease']['fencing_token']
        assert formal['request']['base_revision'] == cycle['selection_lease']['base_revision'] + 1
        assert not service.store.active_leases('p1')
        assert (await client.submit(request)).ref == handle.ref
        assert sum(method == 'POST' and path == '/formal-search/start' for method,path in calls) == 1
        again = await client.proof_cycle('p1', workspace_id='reference')
        idle = await again.wait(timeout=10, poll_interval=.05)
        assert idle.result['cycle_disposition'] in {'idle','blocked','waiting-on-review'}
        assert sum(method == 'POST' and path == '/formal-search/start' for method,path in calls) == 1
        assert backend.replays == 0  # This fixture deliberately uses scripted-test mode.
    try:
        asyncio.run(scenario())
    finally:
        MathsAIHTTPFormalATP._transport = original
        receiver.close(); service.close()


def test_unserved_category_is_rejected_without_admitting_or_leasing(provider, tmp_path):
    service, receiver = configured(tmp_path / 'receiver', provider[0])
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        with pytest.raises(UnsupportedCapability):
            await client.proof_cycle('p1', workspace_id='reference', category='research-move')
        assert not service.store.active_leases('p1')
        assert receiver._db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
    try: asyncio.run(scenario())
    finally: receiver.close(); service.close()


@pytest.mark.parametrize('boundary', ['selected', 'run-prepared', 'provider-request', 'search-admitted', 'cancellation'])
def test_cycle_restart_reattaches_original_selection_and_run(provider, tmp_path, monkeypatch, boundary):
    deployment, backend, calls, *_ = provider
    if boundary == 'cancellation':
        backend.delay = 2
    root = tmp_path / 'receiver'
    service, receiver = configured(root, deployment)
    original_save = CycleWorkflow._save
    formal_save = FormalWorkflow._save
    interrupted = []
    def save_cycle(workflow, operation, state):
        original_save(workflow, operation, state)
        if boundary == 'selected' and state['phase'] == 'selected' and not interrupted:
            interrupted.append(True)
            raise SystemExit('receiver crash after durable selection')
    def save_formal(workflow, operation, state):
        formal_save(workflow, operation, state)
        if boundary == 'provider-request' and state['phase'] == 'prepared' and not interrupted:
            interrupted.append(True)
            raise SystemExit('receiver crash after durable provider request')
        if boundary == 'cancellation' and state.get('cancel_requested') and not interrupted:
            interrupted.append(True)
            raise SystemExit('receiver crash after cancellation delivery')
        if boundary == 'search-admitted' and state['phase'] == 'search-admitted' and not interrupted:
            interrupted.append(True)
            raise SystemExit('receiver crash after committed search result')
    commit = service.gate.commit
    def die_after_seed(proposal):
        result = commit(proposal)
        if boundary == 'run-prepared' and result.accepted and any(getattr(op, 'label', None) == 'FormalRun' for op in proposal.ops) and not interrupted:
            interrupted.append(True)
            raise SystemExit('receiver crash after gated run preparation')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(CycleWorkflow, '_save', save_cycle)
        patch.setattr(FormalWorkflow, '_save', save_formal)
        patch.setattr(service.gate, 'commit', die_after_seed)
        async def start():
            client = AsyncClient(LocalReceiverAdapter(receiver))
            request = client.prepare(ProofCycle('p1'), workspace_id='reference', request_id='recover-cycle')
            handle = await client.submit(request)
            if boundary == 'cancellation':
                async with asyncio.timeout(15):
                    while not any(path == '/formal-search/start' for _,path in calls): await asyncio.sleep(.02)
                await handle.cancel(CancelRequest('cancel-before-restart', handle.ref))
            async with asyncio.timeout(15):
                while receiver._pending: await asyncio.sleep(.02)
            assert (await receiver.get_operation(handle.ref)).lifecycle == ('cancelling' if boundary == 'cancellation' else 'running')
            return request, handle.ref
        try:
            request, original = asyncio.run(start())
            job = job_for(service, request)
            selection = service.jobs.cycle_state(job)
            prepared = service.jobs.formal_state(job)
        finally:
            receiver.close(); service.close()
    assert interrupted
    service, receiver = configured(root, deployment)
    async def recover():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(request)
        assert handle.ref == original
        result = await handle.wait(timeout=20,poll_interval=.05)
        assert result.lifecycle == ('cancelled' if boundary == 'cancellation' else 'succeeded'), result.result
        assert not backend.active
        assert service.jobs.cycle_state(job)['selection_lease'] == selection['selection_lease']
        if prepared:
            assert service.jobs.formal_state(job)['request'] == prepared['request']
        assert sum(method == 'POST' and path == '/formal-search/start' for method,path in calls) == 1
        assert len(service.view.list_nodes('p1','FormalRun')) == 1
    try: asyncio.run(recover())
    finally: receiver.close(); service.close()


def test_formal_cycle_cancellation_waits_for_backend_cleanup(provider, tmp_path):
    deployment, backend, calls, *_ = provider
    backend.delay = 2
    service, receiver = configured(tmp_path / 'receiver', deployment)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.proof_cycle('p1',workspace_id='reference')
        async with asyncio.timeout(15):
            while not any(path == '/formal-search/start' for _,path in calls): await asyncio.sleep(.02)
        cancelled = await handle.cancel(CancelRequest('cancel-cycle',handle.ref))
        assert cancelled.lifecycle in {'cancelling','cancelled'}
        result = await handle.wait(timeout=15,poll_interval=.05)
        assert result.lifecycle == 'cancelled', result.result
        assert not backend.active
        assert not service.store.active_leases('p1')
        assert not (await client.status(workspace_id='reference',proof_id='p1')).formal_verified
    try: asyncio.run(scenario())
    finally: receiver.close(); service.close()


@pytest.mark.parametrize('invalidate', ['release', 'fencing'])
def test_invalid_selection_lease_cannot_create_run_or_send_provider_request(provider, tmp_path, monkeypatch, invalidate):
    deployment, _, calls, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment)
    save = service.cycles._save
    original = []
    def invalidate_selection(operation, state):
        save(operation, state)
        if state['phase'] == 'selected':
            lease = SimpleNamespace(**state['selection_lease'])
            original.append(lease)
            if invalidate == 'release':
                service.formal._release(lease)
            else:
                state['selection_lease']['fencing_token'] += 1000
    monkeypatch.setattr(service.cycles, '_save', invalidate_selection)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        request = client.prepare(ProofCycle('p1'), workspace_id='reference')
        handle = await client.submit(request)
        result = await handle.wait(timeout=15, poll_interval=.05)
        assert result.lifecycle == 'failed', result.result
        assert service.jobs.get(job_for(service,request),'reference')['phase'] == 'failed'
        assert not service.view.list_nodes('p1','FormalRun')
        assert not any(method == 'POST' and path == '/formal-search/start' for method,path in calls)
        assert service.store.read_rejections('p1')
    try: asyncio.run(scenario())
    finally:
        for lease in original: service.formal._release(lease)
        receiver.close(); service.close()


def test_cycle_does_not_supersede_another_workers_busy_declaration(provider, tmp_path):
    from mathproof.scheduler import GlobalScheduler
    service, receiver = configured(tmp_path / 'receiver', provider[0])
    lease = GlobalScheduler(service.view, service.store).lease_selected('p1','p1/fd-1','formal-atp',actor='another-worker')
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        result = await (await client.proof_cycle('p1', workspace_id='reference')).wait(timeout=10,poll_interval=.05)
        assert result.result['cycle_disposition'] == 'waiting-on-lease'
        assert service.store.active_leases('p1')[0].fencing_token == lease.fencing_token
        assert not any(method == 'POST' and path == '/formal-search/start' for method,path in provider[2])
    try: asyncio.run(scenario())
    finally:
        service.formal._release(lease)
        receiver.close(); service.close()


def test_unformalized_proof_reports_blocked_without_scripted_fallback(provider, tmp_path):
    from omegaclaw.service import OperationService
    from omegaclaw.receiver import ReferenceReceiver
    from omegaclaw.formal_runtime import FormalProviderConfig
    service = OperationService(tmp_path/'receiver', formal_config=FormalProviderConfig('http://fixture',deployment_id='fixture',profile='bounded',mode='scripted-test'))
    service.submit({'schema_version':1,'request_id':'create','workspace':'reference','operation':'proof-create',
        'arguments':{'spec':'{"proof_id":"p1","title":"Unformalized","literal_target":"True"}'}})
    receiver = ReferenceReceiver(service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        outcome = await (await client.proof_cycle('p1',workspace_id='reference')).wait(timeout=10,poll_interval=.05)
        assert outcome.result['error_code'] == 'FORMALIZATION_REQUIRED'
        assert outcome.result['cycle_disposition'] == 'blocked'
        assert not service.view.list_nodes('p1','Attempt')
        assert not service.view.list_nodes('p1','FormalRun')
        assert not any(method == 'POST' and path == '/formal-search/start' for method,path in provider[2])
    try: asyncio.run(scenario())
    finally: receiver.close(); service.close()


@pytest.mark.skipif(shutil.which('lean') is None, reason='real Lean is required for native replay')
def test_scheduled_lemma_verification_does_not_claim_root_target_verified(provider, tmp_path):
    from commit_gate.ops import AddEdge, UpsertNode
    from commit_gate.proposal import Proposal
    from mathproof.scheduler import GlobalScheduler
    deployment, backend, *_ = provider
    service, receiver = configured(tmp_path/'receiver', deployment, mode='real')
    lease = service.store.issue_lease('p1','p1/add-lemma',actor='fixture-reviewer',worker_class='coordinator',selected_move_id='p1/c-1')
    source = service.view.node('p1/fd-1').fields['lean_source_artifact']
    result = service.gate.commit(Proposal('p1',lease.actor,lease.worker_class,base_revision=lease.base_revision,
        lease_id=lease.lease_id,fencing_token=lease.fencing_token,selected_move_id=lease.selected_move_id,ops=(
            UpsertNode('Claim','p1/c-2',{'status':'provisional','claim_text':'True'}),
            UpsertNode('FormalDeclaration','p1/fd-2',{'status':'aligned','claim_id':'p1/c-2','lean_type':'True','lean_name':'target','lean_source_artifact':source}),
            UpsertNode('Alignment','p1/al-2',{'status':'aligned','lifecycle':'reviewed','verdict':'aligned','actor':'fixture-reviewer'}),
            AddEdge('PINNED_ENVIRONMENT','p1/fd-2','p1/env-1','p1/lemma-pin'),
            AddEdge('ALIGNS_CLAIM','p1/al-2','p1/c-2','p1/lemma-claim'),
            AddEdge('ALIGNS_DECLARATION','p1/al-2','p1/fd-2','p1/lemma-declaration'))))
    assert result.accepted, result.to_dict()
    service.catch_up('p1'); service.formal._release(lease)
    busy = GlobalScheduler(service.view,service.store).lease_selected('p1','p1/fd-1','formal-atp',actor='other-worker')
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        outcome = await (await client.proof_cycle('p1',workspace_id='reference')).wait(timeout=20,poll_interval=.05)
        assert outcome.lifecycle == 'succeeded',outcome.result
        assert outcome.result['selected_move_id'] == 'p1/fd-2'
        assert outcome.result['claim_id'] == 'p1/c-2'
        assert outcome.result['claim_status'] == 'lean-verified'
        assert not (await client.status(workspace_id='reference',proof_id='p1')).formal_verified
        assert service.view.node('p1/c-1').fields['status'] == 'provisional'
        assert backend.replays == 1
    try: asyncio.run(scenario())
    finally:
        service.formal._release(busy)
        receiver.close(); service.close()
