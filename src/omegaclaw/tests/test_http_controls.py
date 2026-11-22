"""Shared control DTOs cross HTTP; execution support stays capability-gated."""
import asyncio
import threading

import httpx
import pytest

from persistent_atp_client import AsyncClient, Budget, BudgetAmendment, CancelRequest, DecisionResponse, ProofCreate
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import to_dict
from persistent_atp_client.errors import StalePrecondition, UnsupportedCapability
from persistent_atp_client.testing import ScriptedReceiverPort
from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService


def test_controls_roundtrip_and_preserve_idempotency_and_version_checks(tmp_path):
    token = 'local-control-contract-test-token'
    service = OperationService(tmp_path)
    class ControlReceiver(ScriptedReceiverPort):
        async def capabilities(self, contract_version='2.0'):
            if contract_version != '2.0':
                raise UnsupportedCapability('test contract unavailable')
            return await super().capabilities()
    port = ControlReceiver()
    server = serve(service, token, receiver=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    async def scenario():
        async with AsyncClient(HTTPReceiverAdapter(url, token)) as client:
            artifact = await client.put_artifact(b'{}', workspace_id='reference', media_type='application/json')
            handle = await client.submit(client.prepare(ProofCreate(artifact), workspace_id='reference'))
            amendment = BudgetAmendment('amend-stable', handle.ref, 0, Budget(10, 1000))
            assert await handle.amend_budget(amendment) == await handle.amend_budget(amendment)
            with pytest.raises(StalePrecondition):
                await handle.amend_budget(BudgetAmendment('stale-amend', handle.ref, 0, Budget(1, 1)))
            decision = DecisionResponse('decision-stable', handle.ref, 'review-1', 0, 'reject', 'sha256:' + 'a' * 64)
            assert (await handle.answer_decision(decision))['recorded'] is True
            cancel = CancelRequest('cancel-stable', handle.ref)
            cancelled = await handle.cancel(cancel)
            assert cancelled.lifecycle == 'cancelled'
            assert await handle.cancel(cancel) == cancelled
            malformed = {**to_dict(cancel), 'actor': 'administrator'}
            async with httpx.AsyncClient() as http:
                response = await http.post(url + '/v2/controls/cancel', json=malformed,
                    headers={'Authorization': 'Bearer ' + token})
                assert response.status_code == 400
            # The same route must continue to reject unadvertised controls.
            original = port.capabilities
            async def no_controls(*args, **kwargs):
                from dataclasses import replace
                return replace(await original(*args, **kwargs), controls=frozenset())
            port.capabilities = no_controls
            with pytest.raises(UnsupportedCapability):
                await handle.cancel(CancelRequest('not-advertised', handle.ref))
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        service.close()
