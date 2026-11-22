"""Operational queue bounds and recovery without external provider dependencies."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from persistent_atp_client import AsyncClient, SyncClient, ProofCycle, SearchReconciliationRequest
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import Forbidden, QuotaExceeded, ReviewRequired


@pytest.fixture
def queued_recovery(tmp_path, monkeypatch):
    service = OperationService(tmp_path)
    calls = []
    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        raise ReviewRequired('fixture provider is unavailable')
    service.formal = SimpleNamespace(ready=lambda: False, reconcile_search=observe)
    def fail_cycle(*args, **kwargs):
        raise ValueError('fixture failed before formal execution')
    monkeypatch.setattr(service, '_cycle', fail_cycle)
    receiver = ReferenceReceiver(service)
    client = SyncClient(AsyncClient(LocalReceiverAdapter(receiver)))
    controls = []
    for index in range(2):
        proof = 'queue-proof-' + str(index)
        service.submit({'schema_version': 1, 'request_id': 'create-' + str(index), 'workspace': 'reference',
            'operation': 'proof-create', 'arguments': {'spec': '{"proof_id":"' + proof + '","literal_target":"True","title":"Queue fixture"}'}})
        handle = client.submit(client.prepare(ProofCycle(proof), workspace_id='reference', request_id='failed-' + str(index)))
        failed = client.wait(handle, timeout=2)
        assert failed.lifecycle == 'failed'
        controls.append(SearchReconciliationRequest('recover-' + str(index), handle.ref, failed.observation_version))
    monkeypatch.setattr(receiver, '_schedule_reconciliation', lambda _: None)
    yield service, receiver, controls, calls
    receiver.close()
    service.close()


def test_queue_limit_preserves_existing_job_identity(queued_recovery, monkeypatch):
    import omegaclaw.receiver as module
    service, receiver, controls, calls = queued_recovery
    monkeypatch.setattr(module, 'MAX_RECONCILIATION_JOBS', 1)
    first = asyncio.run(receiver.reconcile_search(controls[0]))
    with pytest.raises(QuotaExceeded):
        asyncio.run(receiver.reconcile_search(controls[1]))
    assert asyncio.run(receiver.reconcile_search(controls[0])) == first
    assert receiver._db.execute('SELECT COUNT(*) FROM reconciliation_jobs').fetchone()[0] == 1
    assert receiver._db.execute('SELECT 1 FROM controls WHERE request_id=?', (controls[1].request_id,)).fetchone() is None
    assert calls == []


def test_expired_observation_does_not_contact_provider_or_refresh_job(queued_recovery):
    service, receiver, controls, calls = queued_recovery
    control = controls[0]
    asyncio.run(receiver.reconcile_search(control))
    receiver._db.execute('UPDATE reconciliation_jobs SET deadline=?', (time.time() - 1,))
    head = service.store.head('queue-proof-0')
    receiver._run_reconciliation(control.request_id)
    result = asyncio.run(receiver.read_search_reconciliation(workspace_id='reference', request_id=control.request_id))
    assert result.state == 'blocked' and 'deadline' in result.summary
    assert asyncio.run(receiver.reconcile_search(control)) == result
    assert receiver._db.execute('SELECT attempts FROM reconciliation_jobs').fetchone()[0] == 1
    assert service.store.head('queue-proof-0') == head and calls == []


def test_restart_allowance_does_not_replenish_observation_budget(queued_recovery):
    service, receiver, controls, calls = queued_recovery
    control = controls[0]
    asyncio.run(receiver.reconcile_search(control))
    original = receiver._db.execute('SELECT body, deadline FROM reconciliation_jobs').fetchone()
    receiver._db.execute("UPDATE reconciliation_jobs SET state='running', attempts=3")
    receiver.close()
    reopened = ReferenceReceiver(service)
    try:
        result = asyncio.run(reopened.read_search_reconciliation(workspace_id='reference', request_id=control.request_id))
        assert result.state == 'blocked' and 'allowance exhausted' in result.summary
        assert asyncio.run(reopened.reconcile_search(control)) == result
        row = reopened._db.execute('SELECT body, deadline, attempts FROM reconciliation_jobs').fetchone()
        assert row['body'] == original['body'] and row['deadline'] == original['deadline'] and row['attempts'] == 3
        assert calls == []
    finally:
        reopened.close()


def test_job_reads_are_principal_and_workspace_scoped(queued_recovery):
    service, receiver, controls, _ = queued_recovery
    asyncio.run(receiver.reconcile_search(controls[0]))
    with pytest.raises(Forbidden):
        asyncio.run(receiver.read_search_reconciliation(workspace_id='other', request_id=controls[0].request_id))
    other = ReferenceReceiver(service, principal='other-principal')
    try:
        with pytest.raises(KeyError):
            asyncio.run(other.read_search_reconciliation(workspace_id='reference', request_id=controls[0].request_id))
        with pytest.raises(KeyError):
            asyncio.run(other.reconcile_search(controls[0]))
    finally:
        other.close()
