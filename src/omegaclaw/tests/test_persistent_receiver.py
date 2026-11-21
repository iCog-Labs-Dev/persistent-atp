"""Actual PostgreSQL/MORK receiver recovery, including death after journal commit."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest


@pytest.mark.parametrize('kill_after_commit', [False, True])
def test_persistent_receiver_recovers_same_operation_in_fresh_process(tmp_path, kill_after_commit):
    if not os.environ.get('MORK_LIBRARY') or not os.environ.get('ARTIFACT_TEST_DB_URL'):
        if os.environ.get('OMEGA_PERSISTENCE_REQUIRED') == '1':
            pytest.fail('persistence acceptance requires MORK_LIBRARY and ARTIFACT_TEST_DB_URL')
        pytest.skip('real MORK and a dedicated artifact test database are required')
    from artifacts.schema import apply_schema
    apply_schema(os.environ['ARTIFACT_TEST_DB_URL'])
    proof = 'persist' + uuid.uuid4().hex[:20]
    source = '''
import asyncio, json, os, pathlib, signal, sys
from omegaclaw.service import OperationService
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, ProofCreate, ProofCycle
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import UnsupportedCapability
root, proof, kill = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'kill'
service = OperationService(root, profile='production', artifact_db_url=os.environ['ARTIFACT_TEST_DB_URL'])
if kill:
    def interrupt(*args):
        os.kill(os.getpid(), signal.SIGKILL)
    service.view.record_projected = interrupt
receiver = ReferenceReceiver(service)
async def scenario():
    client = AsyncClient(LocalReceiverAdapter(receiver))
    caps = await client.capabilities()
    assert caps.receiver_mode == 'persistent-storage'
    assert 'proof-cycle' not in caps.actions and caps.backend_mode == 'unavailable'
    spec = json.dumps({'proof_id': proof, 'title': 'Persistence test', 'literal_target': 'True'}).encode()
    artifact = await client.put_artifact(spec, workspace_id='reference', media_type='application/json')
    request = client.prepare(ProofCreate(artifact), workspace_id='reference', request_id='stable-create')
    handle = await client.submit(request)
    assert (await client.submit(request)).ref == handle.ref
    result = await handle.wait(timeout=10)
    assert result.lifecycle == 'succeeded', result
    status = await client.status(workspace_id='reference', proof_id=proof)
    assert status.revision == status.projection_watermark == 1
    assert status.component_mode == 'persistent-storage' and not status.formal_verified
    assert service.store.verify_chain(proof) == 1
    try:
        await client.submit(client.prepare(ProofCycle(proof), workspace_id='reference'))
    except UnsupportedCapability:
        pass
    else:
        raise AssertionError('production selected a scripted executor')
    assert service.store.head(proof)[0] == 1
    (root / 'observed.json').write_text(json.dumps({'operation': handle.ref.operation_id, 'revision': status.revision}))
    await client.aclose()
asyncio.run(scenario())
receiver.close()
service.close()
'''
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(Path.cwd() / 'src'), str(Path.cwd()),
        str(Path.cwd() / 'packages/persistent-atp-client/src')]),
        'ARTIFACT_DB_URL': os.environ['ARTIFACT_TEST_DB_URL']}
    command = [sys.executable, '-c', source, str(tmp_path), proof]
    first = subprocess.run([*command, 'kill' if kill_after_commit else 'normal'], env=env,
                           capture_output=True, text=True, timeout=30)
    assert first.returncode == (-9 if kill_after_commit else 0), first.stderr[-2000:]
    before = json.loads((tmp_path / 'observed.json').read_text()) if not kill_after_commit else None
    second = subprocess.run([*command, 'normal'], env=env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr[-2000:]
    after = json.loads((tmp_path / 'observed.json').read_text())
    assert after['revision'] == 1
    if before:
        assert after == before
