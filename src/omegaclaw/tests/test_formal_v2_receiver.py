"""PR85/95 receiver wiring. The backend fixture is scripted, not a trained model."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import os

import pytest

REQUIRED = os.environ.get('OMEGA_FORMAL_CONTRACT_REQUIRED') == '1'
try:
    import fastapi
except ImportError:
    if REQUIRED:
        raise RuntimeError('install the formal-tests extra for required formal contract checks')
    pytest.skip('formal provider integration dependencies are required', allow_module_level=True)
PROVIDER = Path(os.environ.get('PERSISTENT_ATP_FORMAL_SOURCE',
    str(Path(__file__).resolve().parents[3] / 'tmp/repos/Mathematics-ai-atp-pr95-consolidated')))
if PROVIDER.is_dir():
    sys.path.insert(0, str(PROVIDER))
try:
    import maths_ai.formal
except ImportError:
    if REQUIRED:
        raise RuntimeError('install the pinned provider or set PERSISTENT_ATP_FORMAL_SOURCE to its checkout')
    pytest.skip('the pinned provider is required for this integration suite', allow_module_level=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from maths_ai.formal.backends import AttemptOutcome, VerifyOutcome
from maths_ai.formal.deployments import DeploymentRegistry
from maths_ai.formal.routes import formal_router
from maths_ai.formal.service import FormalService

from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from mathproof.maths_ai_http import MathsAIHTTPFormalATP, MathsAIAPIError as FormalV2Error
from mathproof.scheduler import GlobalScheduler
from omegaclaw.formal_runtime import FormalProviderConfig
from omegaclaw.formal_runtime import FormalWorkflow
from omegaclaw.service import OperationService
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, Budget, CancelRequest, FormalSearch, Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter


@pytest.fixture
def provider(tmp_path, monkeypatch):
    class Backend:
        supports_universes = True
        delay = 0
        replays = 0
        active = False
        stagnate = False
        def ready(self): return True
        def imports(self): return []
        def environment_manifest(self): return {'fixture': 'scripted-search-native-replay'}
        async def quiesce(self):
            assert not self.active
        async def retire(self):
            await self.quiesce()
        async def elaborate_source(self, payload, **kwargs):
            return {'target_sexp': '(:c True)', 'target_pp': 'True', 'locals': [],
                    'target_unit_start': 0, 'check': {'ok': True}}
        async def run(self, task, **kwargs):
            self.active = True
            try:
                await asyncio.sleep(self.delay)
                if self.stagnate:
                    # Exercise the real provider's typed unsuccessful outcome
                    # without claiming a counterexample or a Lean rejection.
                    return AttemptOutcome('frontier-exhausted', steps_used=1)
                trace = {'goal': task.statement['expression'], 'tactic': 'trivial', 'arguments': [], 'subgoals': []}
                certificate = {'format': 'maths-ai-accepted-certificate-v1', 'statement': task.statement, 'proof_trace': trace}
                return AttemptOutcome('certified', True, 1, trace, certificate)
            finally:
                self.active = False
        async def verify(self, certificate, *, statement, **kwargs):
            self.replays += 1
            # A fresh real Lean process verifies the controlled True fixture.
            # This does not claim trained-search or pinned Mathlib acceptance.
            assert statement['expression'] == '@True'
            path = tmp_path / 'Replay.lean'
            path.write_text('theorem replay_target : True := by trivial\n#print axioms replay_target\n')
            result = subprocess.run(['lean', '-DwarningAsError=true', str(path)], capture_output=True, text=True, timeout=15)
            verified = result.returncode == 0 and 'sorryAx' not in result.stdout + result.stderr
            return VerifyOutcome('verified' if verified else 'rejected', None, 1, self.environment_manifest(),
                {'sorry_detected': not verified})
    backend = Backend()
    registry = DeploymentRegistry.from_dict({'format': 'maths-ai-formal-deployments-v1', 'deployments': {
        'fixture': {'backend': 'accepted-gnn', 'environment_id': 'fixture-lean', 'inputs': ['source'],
            'profiles': {'bounded': {'max_steps': 10, 'wall_clock_ms': 5000}},
            'verification': {'max_steps': 20, 'wall_clock_ms': 10000}}}})
    @asynccontextmanager
    async def lifespan(app):
        app.state.formal = FormalService(registry=registry, state_dir=tmp_path / 'provider', backends={'accepted-gnn': backend})
        yield
        await app.state.formal.close()
    app = FastAPI(lifespan=lifespan)
    app.include_router(formal_router(lambda request: request.app.state.formal))
    with TestClient(app) as http:
        calls = []
        lost_start = [False]
        crash_start = [False]
        def transport(self, method, path, data=None, *, binary=False, expected_statuses=frozenset({200})):
            calls.append((method, path))
            if crash_start[0] and path == '/formal-search/start':
                crash_start[0] = False
                raise SystemExit('injected worker crash before delivery')
            result = http.request(method, path, content=data) if isinstance(data, bytes) else http.request(method, path, json=data)
            if result.status_code not in expected_statuses:
                raise FormalV2Error('fixture HTTP failure', status_code=result.status_code)
            if lost_start[0] and path == '/formal-search/start':
                lost_start[0] = False
                raise FormalV2Error('injected lost admission response')
            return (result.content, result.headers.get('X-Content-Hash')) if binary else result.json()
        monkeypatch.setattr(MathsAIHTTPFormalATP, '_transport', transport)
        yield http.get('/formal-capabilities').json()['deployments']['fixture'], backend, calls, lost_start, crash_start


def configured(root, deployment, *, mode='scripted-test', profile='reference'):
    service = OperationService(root, formal_config=FormalProviderConfig('http://fixture', deployment_id='fixture',
        profile='bounded', mode=mode, poll_seconds=.05), profile=profile,
        artifact_db_url=os.environ.get('ARTIFACT_TEST_DB_URL') if profile != 'reference' else None)
    if service.store.head('p1')[0] == 0:
        service.submit({'schema_version': 1, 'request_id': 'create', 'workspace': 'reference',
            'operation': 'proof-create', 'arguments': {'spec': '{"proof_id":"p1","title":"Fixture","literal_target":"True"}'}})
        source = service.artifacts.put(b'theorem target : True := by trivial\n')
        row = service.store.issue_lease('p1', 'p1/seed', actor='fixture-reviewer', worker_class='coordinator', selected_move_id='p1/c-1')
        ops = (UpsertNode('FormalDeclaration', 'p1/fd-1', {'status': 'aligned', 'claim_id': 'p1/c-1',
                    'lean_type': 'True', 'lean_name': 'target', 'lean_source_artifact': source}),
            UpsertNode('Environment', 'p1/env-1', {'toolchain': 'fixture-lean',
                'environment_hash': deployment['environment_hash'], 'provider_environment_id': 'fixture-lean'}),
            UpsertNode('Alignment', 'p1/al-1', {'status': 'aligned', 'lifecycle': 'reviewed', 'verdict': 'aligned', 'actor': 'fixture-reviewer'}),
            AddEdge('PINNED_ENVIRONMENT', 'p1/fd-1', 'p1/env-1', 'p1/e-pin'),
            AddEdge('ALIGNS_CLAIM', 'p1/al-1', 'p1/c-1', 'p1/e-ac'),
            AddEdge('ALIGNS_DECLARATION', 'p1/al-1', 'p1/fd-1', 'p1/e-ad'))
        result = service.gate.commit(Proposal('p1', row.actor, row.worker_class, ops=ops, base_revision=row.base_revision,
            lease_id=row.lease_id, fencing_token=row.fencing_token, selected_move_id=row.selected_move_id))
        assert result.accepted, result.to_dict()
        service.catch_up('p1')
        service.store.release_lease('p1', row.lease_id, fencing_token=row.fencing_token)
    return service, ReferenceReceiver(service)


def test_search_admission_lost_reply_is_reconciled_without_duplicate_run(provider, tmp_path):
    deployment, backend, calls, lost, _ = provider
    lost[0] = True
    service, receiver = configured(tmp_path / 'receiver', deployment)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        request = client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)), workspace_id='reference', request_id='search')
        handle = await client.submit(request)
        result = await handle.wait(timeout=15, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
        assert result.result['mathematical_disposition'] == 'proved-pending-replay'
        assert result.mathematical_disposition == 'proved-pending-replay'
        assert not (await client.status(workspace_id='reference', proof_id='p1')).formal_verified
        events, _ = await receiver.read_events(handle.ref)
        assert 'prepared' in [event.event_type for event in events]
        assert 'search-admitted' in [event.event_type for event in events]
        assert len([path for method, path in calls if method == 'POST' and path == '/formal-search/start']) == 1
        state = service.jobs.formal_state(service.jobs.conn.execute("SELECT operation_id FROM operations WHERE request_id='search'").fetchone()[0])
        assert state['request']['run_id'] == result.result['run_id']
        assert (await client.submit(request)).ref == handle.ref
        assert 'search_lease' not in str(result.result) and 'fencing_token' not in str(result.result)
    try: asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


def test_stagnated_search_is_bound_and_compiled_as_obstruction_research_context(provider, tmp_path, monkeypatch):
    """A stagnated result becomes a compiled packet and only a speculative LLM proposal."""
    deployment, backend, *_ = provider
    backend.stagnate = True
    service, receiver = configured(tmp_path / 'receiver', deployment)
    save = FormalWorkflow._save
    injected_duplicate = False

    def save_with_legacy_environment_edge(workflow, operation, state):
        nonlocal injected_duplicate
        save(workflow, operation, state)
        if state.get('phase') != 'search-admitted' or injected_duplicate:
            return
        injected_duplicate = True
        run_id = state['run_id']
        environment_id = service.view.edges_from(run_id, 'RAN_UNDER')[0].dst_id
        lease = service.store.issue_lease(
            'p1', 'p1/legacy-ran-under', actor='legacy-fixture',
            worker_class='coordinator', selected_move_id=run_id,
        )
        try:
            committed = service.gate.commit(Proposal('p1', lease.actor, lease.worker_class,
                ops=(AddEdge('RAN_UNDER', run_id, environment_id, 'p1/legacy-ran-under-edge'),),
                base_revision=lease.base_revision, lease_id=lease.lease_id,
                fencing_token=lease.fencing_token, selected_move_id=run_id))
            assert committed.accepted, committed.to_dict()
            service.catch_up('p1')
        finally:
            service.store.release_lease('p1', lease.lease_id, fencing_token=lease.fencing_token)

    monkeypatch.setattr(FormalWorkflow, '_save', save_with_legacy_environment_edge)

    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        request = client.prepare(
            FormalSearch('p1', 'p1/c-1', Budget(5, 3000)),
            workspace_id='reference',
            request_id='stagnated-search',
        )
        handle = await client.submit(request)
        outcome = await handle.wait(timeout=15, poll_interval=.05)
        assert outcome.lifecycle == 'succeeded', outcome.result
        assert outcome.result['mathematical_disposition'] == 'stagnated'
        assert outcome.result['phase'] == 'obstruction-ready'
        contexts = outcome.result['obstruction_contexts']
        assert len(contexts) == 1
        obstruction_id = contexts[0]['obstruction_id']
        obstruction = service.view.node(obstruction_id)
        assert obstruction is not None and obstruction.label == 'Obstruction'
        assert obstruction.fields['formal_run_id'] == outcome.result['run_id']
        assert obstruction.fields['formal_state_ids']
        assert obstruction.fields['diagnostic_artifacts']
        assert service.view.edges_to(obstruction_id, 'RESOLVES')[0].src_id == 'p1/c-1'
        assert len(service.view.edges_from(outcome.result['run_id'], 'RAN_UNDER')) == 1
        assert any(candidate.move_id == obstruction_id and candidate.category == 'obstruction-task'
            for candidate in GlobalScheduler(service.view, service.store).build_frontier('p1'))
        state_edges = service.view.edges_from(obstruction_id, 'AT_STATE')
        assert len(state_edges) == 1
        failed_state = service.view.node(state_edges[0].dst_id)
        assert failed_state.fields['context_scope'] == 'admitted-root'
        assert failed_state.fields['target'] == '(:c True)'
        assert failed_state.fields['hypotheses'] == []
        assert service.view.edges_from(obstruction_id, 'HAS_ARTIFACT')

        context_ref = contexts[0]['payload_artifact']
        payload = json.loads(service.artifacts.get(context_ref))
        assert payload['obstruction_id'] == obstruction_id
        assert payload['formal_state_ids'] == [failed_state.node_id]
        packet = service.artifacts.get(contexts[0]['packet_artifact']).decode('utf-8')
        assert 'id=p1/c-1' in packet
        assert 'statement:' in packet

        from types import SimpleNamespace
        from alignment.llm_client import MockLLMClient
        from omegaclaw.research_runtime import ResearchWorkflow
        llm = MockLLMClient(responses=[{"candidates": [{
            "statement": "Prove the successor case through an intermediate equality.",
            "novelty": "Separates the successor step from the base case.",
            "cost": "One auxiliary lemma and one bounded formal search.",
            "risk": "The equality may not follow from the current assumptions.",
            "falsification_test": "Attempt to prove the auxiliary equality under the pinned environment.",
            "expiry_review": "Review after one failed formal attempt.",
            "consequences": ["May reduce the original target to a simpler equality."],
        }]}])
        service.research = ResearchWorkflow(
            service, SimpleNamespace(provider="fixture", timeout=5), llm_factory=lambda: llm,
        )
        bridge = service.submit({
            "schema_version": 1,
            "request_id": "stagnated-bridge",
            "workspace": "reference",
            "operation": "lemma-bridge",
            "arguments": {"proof": "p1", "obstruction": obstruction_id},
        })
        assert bridge["phase"] == "finished", bridge
        assert bridge["hypothesis_status"] == "speculative"
        assert len(bridge["hypothesis_ids"]) == 1
        hypothesis = service.view.node(bridge["hypothesis_ids"][0])
        assert hypothesis.label == "SpeculativeHypothesis"
        assert hypothesis.fields["support"] == "LLM-generated research suggestion; no proof or verification is asserted."
        assert obstruction_id in hypothesis.fields["source_ids"]
        assert packet in llm.history[0][1]
        assert service.view.node("p1/c-1").fields["status"] == "provisional"
        assert len(service.view.list_nodes("p1", "FormalDeclaration")) == 1
        operation_id = service.jobs.conn.execute(
            "SELECT operation_id FROM operations WHERE request_id='stagnated-bridge'"
        ).fetchone()[0]
        revision = service.store.head("p1")[0]
        recovered = service.research.run("p1", obstruction_id, operation_id)
        assert recovered["hypothesis_ids"] == bridge["hypothesis_ids"]
        assert service.store.head("p1")[0] == revision
        assert len(llm.history) == 1

    try:
        asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


@pytest.mark.parametrize('boundary', ['preparation', 'before-delivery', 'cancellation', 'search-commit', 'replay-receipt', 'cached-replay-receipt', 'replay-commit', 'promotion'])
def test_receiver_restart_preserves_run_and_committed_evidence(provider, tmp_path, monkeypatch, boundary):
    if 'replay' in boundary or boundary == 'promotion':
        if shutil.which('lean') is None:
            pytest.skip('real Lean is required for replay recovery')
        mode = 'real'
    else:
        mode = 'scripted-test'
    deployment, backend, calls, _, crash = provider
    if boundary == 'cancellation':
        backend.delay = 2
    root = tmp_path / 'receiver'
    service, receiver = configured(root, deployment, mode=mode)
    saved = FormalWorkflow._save
    interrupted = []
    attempted = []
    def stop(workflow, operation, state):
        phase = state['phase']
        should_stop = ((boundary == 'preparation' and phase == 'prepared')
            or (boundary == 'search-commit' and phase == 'search-admitted')
            or (boundary == 'cancellation' and state.get('cancel_requested'))
            or (boundary in {'replay-receipt', 'cached-replay-receipt'} and phase == 'replay-started' and 'receipt' in state)
            or (boundary == 'replay-commit' and phase == 'replay-admitted')
            or (boundary == 'promotion' and phase == 'completed'))
        # For receipt recovery, simulate death after the receipt's durable write.
        if boundary in {'replay-receipt', 'cached-replay-receipt'}:
            saved(workflow, operation, state)
        if should_stop:
            interrupted.append(True)
            attempted.append(dict(state))
            raise SystemExit('injected worker crash at ' + boundary)
        if boundary not in {'replay-receipt', 'cached-replay-receipt'}:
            saved(workflow, operation, state)
    with monkeypatch.context() as patch:
        patch.setattr(FormalWorkflow, '_save', stop)
        crash[0] = boundary == 'before-delivery'
        async def start():
            client = AsyncClient(LocalReceiverAdapter(receiver))
            request = client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)),
                workspace_id='reference', request_id='recover')
            handle = await client.submit(request)
            if boundary == 'cancellation':
                async with asyncio.timeout(10):
                    while not any(path == '/formal-search/start' for _, path in calls):
                        await asyncio.sleep(.02)
                await handle.cancel(CancelRequest('cancel-before-restart', handle.ref))
            async with asyncio.timeout(20):
                while receiver._pending:
                    await asyncio.sleep(.02)
            expected = 'cancelling' if boundary == 'cancellation' else 'running'
            assert (await receiver.get_operation(handle.ref)).lifecycle == expected
            return request, handle.ref
        try:
            request, original = asyncio.run(start())
            job_id = service.jobs.conn.execute("SELECT operation_id FROM operations WHERE request_id='recover'").fetchone()[0]
            prepared = service.jobs.formal_state(job_id)
            if boundary == 'preparation':
                assert prepared == {}
                prepared = attempted[0]
            run = prepared['run_id']
        finally:
            receiver.close()
            service.close()
    assert boundary == 'before-delivery' or interrupted
    if boundary == 'cached-replay-receipt':
        def offline(*args, **kwargs):
            raise FormalV2Error('provider is offline', status_code=503)
        monkeypatch.setattr(MathsAIHTTPFormalATP, '_transport', offline)
    service, receiver = configured(root, deployment, mode=mode)
    async def recover():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        if boundary == 'cached-replay-receipt':
            assert 'formal-search' not in (await client.capabilities()).actions
            assert (await client.lookup_request(workspace_id='reference', request_id=request.request_id)).operation_ref == original
            assert (await receiver.submit(request)).operation_ref == original
            handle = client.attach(original)
        else:
            handle = await client.submit(request)
        assert handle.ref == original
        outcome = await handle.wait(timeout=20, poll_interval=.05)
        assert outcome.lifecycle == ('cancelled' if boundary == 'cancellation' else 'succeeded'), outcome.result
        assert outcome.result['run_id'] == run
        recovered = service.jobs.formal_state(job_id)['request']
        if boundary == 'preparation':
            # No request had been persisted or sent. Only fencing authority
            # changes when the orphan is released and compilation resumes.
            assert {k: v for k, v in recovered.items() if k not in {'lease_id', 'fencing_token'}} == {
                k: v for k, v in prepared['request'].items() if k not in {'lease_id', 'fencing_token'}}
            assert recovered['fencing_token'] > prepared['request']['fencing_token']
        else:
            assert recovered == prepared['request']
        expected_posts = 2 if boundary == 'before-delivery' else 1
        assert sum(method == 'POST' and path == '/formal-search/start' for method, path in calls) == expected_posts
        assert backend.replays == (1 if mode == 'real' else 0)
        assert not backend.active
        assert (await client.status(workspace_id='reference', proof_id='p1')).formal_verified == (mode == 'real')
        events, _ = await receiver.read_events(original)
        versions = [event.observation_version for event in events]
        assert versions == sorted(set(versions))
    try:
        asyncio.run(recover())
    finally:
        receiver.close()
        service.close()


@pytest.mark.skipif(shutil.which('lean') is None, reason='real Lean is required for replay delivery test')
def test_lost_replay_receipt_requires_reconciliation_without_second_replay(provider, tmp_path, monkeypatch):
    deployment, backend, calls, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment, mode='real')
    transport = MathsAIHTTPFormalATP._transport
    def lose_receipt(*args, **kwargs):
        value = transport(*args, **kwargs)
        if args[2] == '/formal-replay':
            raise FormalV2Error('injected lost replay receipt')
        return value
    monkeypatch.setattr(MathsAIHTTPFormalATP, '_transport', lose_receipt)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)),
            workspace_id='reference', request_id='uncertain-replay'))
        outcome = await handle.wait(timeout=20, poll_interval=.05)
        assert outcome.lifecycle == 'failed'
        assert outcome.result['error_code'] == 'FORMAL_REPLAY_RECONCILIATION_REQUIRED'
        assert not (await client.status(workspace_id='reference', proof_id='p1')).formal_verified
        job = service.jobs.conn.execute("SELECT operation_id FROM operations WHERE request_id='uncertain-replay'").fetchone()[0]
        with pytest.raises(FormalV2Error, match='replay delivery is uncertain'):
            service.formal.run(job, 'p1', 'p1/c-1', {'max_steps': 5, 'wall_clock_ms': 3000})
        assert backend.replays == 1
        assert sum(path == '/formal-replay' for _, path in calls) == 1
    try:
        asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


@pytest.mark.skipif(shutil.which('lean') is None, reason='real Lean is required for native replay')
def test_separate_native_replay_and_gate_promotion(provider, tmp_path):
    deployment, backend, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment, mode='real')
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)), workspace_id='reference'))
        result = await handle.wait(timeout=30, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
        assert result.result['replay_status'] == 'verified'
        assert result.mathematical_disposition == 'lean-verified'
        assert (await client.status(workspace_id='reference', proof_id='p1')).formal_verified
        audit = await (await client.proof_audit('p1', workspace_id='reference')).wait(timeout=10)
        assert audit.result['audit_status'] == 'ok'
        exported = await (await client.proof_export('p1', 'default', workspace_id='reference')).wait(timeout=10)
        from persistent_atp_client import ArtifactRef
        from omegaclaw.export import check_export
        import json
        bundle = await client.get_artifact(ArtifactRef(exported.result['export_artifact']), workspace_id='reference')
        checked = check_export(bundle)
        assert checked['revision'] == service.store.head('p1')[0]
        included = set(json.loads(bundle)['artifacts'])
        for node in service.view.list_nodes('p1', 'Certificate'):
            assert {node.fields[k] for k in ('artifact_hash', 'binding_artifact_hash',
                'input_artifact_hash', 'source_artifact_hash')} <= included
        for node in service.view.list_nodes('p1', 'LeanReplay'):
            assert node.fields['receipt_artifact'] in included
        for node in service.view.list_nodes('p1', 'FormalRun'):
            provider_result = json.loads(service.artifacts.get(node.fields['provider_result_artifact']))
            assert {item['sha256'] for item in provider_result['artifacts']} <= included
        assert backend.replays == 1
    try: asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


@pytest.mark.parametrize('lose_reply', [False, True])
def test_cancel_remains_pending_until_provider_stops(provider, tmp_path, monkeypatch, lose_reply):
    deployment, backend, *_ = provider
    backend.delay = 2
    service, receiver = configured(tmp_path / 'receiver', deployment)
    if lose_reply:
        transport = MathsAIHTTPFormalATP._transport
        def lose_cancel(*args, **kwargs):
            value = transport(*args, **kwargs)
            if args[2].endswith('/cancel'):
                raise FormalV2Error('injected lost cancellation response')
            return value
        monkeypatch.setattr(MathsAIHTTPFormalATP, '_transport', lose_cancel)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)), workspace_id='reference'))
        async with asyncio.timeout(10):
            while not any(path == '/formal-search/start' for method, path in provider[2]):
                await asyncio.sleep(.02)
        response = await handle.cancel(CancelRequest('cancel', handle.ref))
        assert response.lifecycle in {'cancelling', 'cancelled'}
        result = await handle.wait(timeout=10, poll_interval=.05)
        assert result.lifecycle == 'cancelled', result.result
        assert not (await client.status(workspace_id='reference', proof_id='p1')).formal_verified
        assert backend.replays == 0
        assert not backend.active
        assert sum(path.endswith('/cancel') for _, path in provider[2]) == 1
    try: asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


@pytest.mark.skipif(shutil.which('lean') is None, reason='real Lean is required for persisted replay')
@pytest.mark.parametrize('operation', ['formal-search', 'proof-cycle', 'selected-cycle'])
def test_production_formal_result_survives_postgres_mork_receiver_restart(provider, tmp_path, operation):
    if not os.environ.get('MORK_LIBRARY') or not os.environ.get('ARTIFACT_TEST_DB_URL'):
        if os.environ.get('OMEGA_PERSISTENCE_REQUIRED') == '1':
            pytest.fail('formal persistence check requires native MORK and a dedicated artifact database')
        pytest.skip('native MORK and a dedicated PostgreSQL artifact database are required')
    # The native library owns one process-wide space. Each parameter needs a
    # fresh process so another journal's p1 projection cannot masquerade as ours.
    if os.environ.get('OMEGA_FORMAL_STORAGE_CASE') != operation:
        env = {**os.environ, 'OMEGA_FORMAL_STORAGE_CASE': operation}
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
            f'{__file__}::test_production_formal_result_survives_postgres_mork_receiver_restart[{operation}]',
            '--basetemp', str(tmp_path / 'native-process')], env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ['ARTIFACT_TEST_DB_URL'])
    deployment, backend, *_ = provider
    root = tmp_path / 'receiver'
    service, receiver = configured(root, deployment, mode='real', profile='production')
    async def execute():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        caps = await client.capabilities()
        assert caps.independent_replay and caps.backend_mode == 'real'
        assert 'formal-search' in caps.actions and 'formal-resume' not in caps.actions
        assert 'proof-cycle' in caps.actions and 'cancel' in caps.controls
        from persistent_atp_client import ProofCycle, SelectedProofCycle
        action = (FormalSearch('p1', 'p1/c-1', Budget(5, 3000)) if operation == 'formal-search'
            else ProofCycle('p1') if operation == 'proof-cycle' else SelectedProofCycle('p1', 'formal-run'))
        request = client.prepare(action, workspace_id='reference', request_id='persisted-formal')
        handle = await client.submit(request)
        result = await handle.wait(timeout=20, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
        status = await client.status(workspace_id='reference', proof_id='p1')
        assert status.formal_verified and status.revision == status.projection_watermark
        return request, handle.ref, status.revision
    try:
        request, original, revision = asyncio.run(execute())
    finally:
        receiver.close()
        service.close()
    service, receiver = configured(root, deployment, mode='real', profile='production')
    async def recover():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(request)
        assert handle.ref == original
        assert (await handle.wait(timeout=5)).lifecycle == 'succeeded'
        status = await client.status(workspace_id='reference', proof_id='p1')
        assert status.formal_verified and status.revision == status.projection_watermark == revision
        assert service.store.verify_chain('p1') == revision
        assert backend.replays == 1
    try:
        asyncio.run(recover())
    finally:
        receiver.close()
        service.close()


def test_old_contract_operation_requires_reconciliation_before_any_delivery(provider, tmp_path):
    from omegaclaw.formal_runtime import ContractMigrationRequired
    deployment, _, calls, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment)
    saved = {'phase': 'prepared', 'provider_url': 'http://fixture', 'request': {'run_id': 'p1/fr-old'}}
    service.formal._load = lambda operation: saved
    count = len(calls)
    try:
        with pytest.raises(ContractMigrationRequired) as failure:
            service.formal.run('old-operation', 'p1', 'p1/c-1', {'max_steps': 5, 'wall_clock_ms': 3000})
        assert failure.value.code == 'FORMAL_CONTRACT_MIGRATION_REQUIRED'
        assert len(calls) == count
        assert saved['request'] == {'run_id': 'p1/fr-old'}
        assert 'contract_manifest' not in saved
    finally:
        receiver.close()
        service.close()


def test_provider_io_does_not_hold_service_lock(provider, tmp_path, monkeypatch):
    deployment, _, *_ = provider
    service, receiver = configured(tmp_path / 'receiver', deployment)
    transport = MathsAIHTTPFormalATP._transport
    def checked_transport(*args, **kwargs):
        assert not service.lock._is_owned(), 'provider I/O cannot hold the journal service lock'
        return transport(*args, **kwargs)
    monkeypatch.setattr(MathsAIHTTPFormalATP, '_transport', checked_transport)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(client.prepare(FormalSearch('p1', 'p1/c-1', Budget(5, 3000)), workspace_id='reference'))
        result = await handle.wait(timeout=15, poll_interval=.05)
        assert result.lifecycle == 'succeeded', result.result
    try:
        asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()
