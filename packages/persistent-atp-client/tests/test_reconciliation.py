"""Recovery must distinguish an absent request from an unavailable lookup."""
import asyncio
from dataclasses import replace

import pytest

from persistent_atp_client import AsyncClient, ProofCycle, OperationRef, SearchReconciliationRequest, SearchReconciliationJob
from persistent_atp_client.errors import ReceiverProtocolError, SubmissionUncertain, TransientUnavailable, UnsupportedCapability, ObservationTimeout, ReviewRequired
from persistent_atp_client.testing import ScriptedReceiverPort


def test_reconcile_reattaches_without_submitting_again():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab")
        original = await client.submit(request)
        async def forbidden(*args): raise AssertionError("admitted request was resubmitted")
        port.submit = forbidden
        assert (await client.reconcile(request)).ref == original.ref
    asyncio.run(scenario())


def test_reconcile_absence_submits_the_identical_prepared_body():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab")
        submitted = []
        original = port.submit
        async def capture(value):
            submitted.append(value)
            return await original(value)
        port.submit = capture
        handle = await client.reconcile(request)
        assert submitted == [request] and (await handle.snapshot()).request_id == request.request_id
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unavailable", "wrong-fingerprint"])
def test_reconcile_does_not_submit_when_lookup_is_untrustworthy(failure):
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab")
        receipt = await port.submit(request)
        async def lookup(*args):
            if failure == "unavailable": raise TransientUnavailable()
            return replace(receipt, fingerprint="sha256:" + "0" * 64)
        async def forbidden(*args): raise AssertionError("unconfirmed absence caused resubmission")
        port.lookup_request, port.submit = lookup, forbidden
        with pytest.raises(TransientUnavailable if failure == "unavailable" else ReceiverProtocolError):
            await client.reconcile(request)
    asyncio.run(scenario())


def test_reconcile_bounds_lookup_and_preserves_original_identity_on_timeout():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab")
        async def lookup(*args): await asyncio.Event().wait()
        async def forbidden(*args): raise AssertionError("timeout authorized resubmission")
        port.lookup_request, port.submit = lookup, forbidden
        with pytest.raises(SubmissionUncertain) as error:
            await client.reconcile(request, timeout=0.01)
        assert error.value.request == request
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['unadvertised', 'operation', 'request', 'version', 'untyped'])
def test_search_reconciliation_requires_capability_and_bound_monotonic_response(failure):
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        handle = await client.submit(client.prepare(ProofCycle('p1'), workspace_id='lab', request_id='original-search'))
        response = replace(await handle.snapshot(), observation_version=3, lifecycle='failed')
        port._operations[handle.ref] = response
        await handle.snapshot()
        capabilities = port.capabilities
        async def caps(**kwargs):
            result = await capabilities(**kwargs)
            return result if failure == 'unadvertised' else replace(result, controls=result.controls | {'reconcile-search'})
        calls = []
        async def reconcile(control):
            calls.append(control)
            if failure == 'operation': return replace(response, ref=OperationRef('other', 'op1'))
            if failure == 'request': return replace(response, request_id='other-original-search')
            if failure == 'version': return replace(response, observation_version=2)
            return {'approved': True}
        port.capabilities, port.reconcile_search = caps, reconcile
        with pytest.raises(UnsupportedCapability if failure == 'unadvertised' else ReceiverProtocolError):
            await handle.reconcile_search(SearchReconciliationRequest('inspect-original', handle.ref, 3))
        assert len(calls) == (0 if failure == 'unadvertised' else 1)
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['operation', 'request', 'version', 'across-calls', 'untyped', 'blocked', 'missing-receipt'])
def test_background_reconciliation_validates_each_polled_job(fault):
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        handle = await client.submit(client.prepare(ProofCycle('p1'), workspace_id='lab'))
        control = SearchReconciliationRequest('observe-original', handle.ref, 0)
        initial = SearchReconciliationJob(control.request_id, handle.ref, 'running', 1)
        caps = port.capabilities
        async def capabilities(**kwargs):
            return replace(await caps(**kwargs), controls=frozenset({'reconcile-search', 'reconcile-search-jobs'}))
        async def start(request): return initial
        async def read(**kwargs):
            assert kwargs == {'workspace_id': 'lab', 'request_id': control.request_id}
            if fault == 'operation': return replace(initial, operation_ref=OperationRef('lab', 'other'))
            if fault == 'request': return replace(initial, request_id='other-control')
            if fault in {'version', 'across-calls'}: return replace(initial, version=0)
            if fault == 'untyped': return {'state': 'succeeded'}
            if fault == 'blocked': return replace(initial, state='blocked', version=2, error_code='REVIEW_REQUIRED', summary='Original run still active')
            return replace(initial, state='succeeded', version=2)
        port.capabilities, port.reconcile_search, port.read_search_reconciliation = capabilities, start, read
        with pytest.raises(ReviewRequired if fault == 'blocked' else ReceiverProtocolError):
            if fault == 'across-calls':
                assert await handle.reconcile_search(control, wait=False) == initial
                await handle.wait_search_reconciliation(control.request_id, poll_interval=.001)
            else:
                await handle.reconcile_search(control, poll_interval=.001)
    asyncio.run(scenario())


def test_background_observation_timeout_can_resume_without_another_post():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        handle = await client.submit(client.prepare(ProofCycle('p1'), workspace_id='lab'))
        control = SearchReconciliationRequest('durable-control', handle.ref, 0)
        job = [SearchReconciliationJob(control.request_id, handle.ref, 'running', 1)]
        caps = port.capabilities
        posts = []
        async def capabilities(**kwargs):
            return replace(await caps(**kwargs), controls=frozenset({'reconcile-search', 'reconcile-search-jobs'}))
        async def start(request):
            posts.append(request)
            return job[0]
        async def read(**kwargs): return job[0]
        port.capabilities, port.reconcile_search, port.read_search_reconciliation = capabilities, start, read
        with pytest.raises(ObservationTimeout) as error:
            await handle.reconcile_search(control, timeout=.02, poll_interval=.001)
        assert error.value.request_id == control.request_id and error.value.operation_ref == handle.ref
        job[0] = replace(job[0], state='succeeded', version=2)
        old = port._operations[handle.ref]
        port._operations[handle.ref] = replace(old, result={'execution_reconciliation': {
            'execution_settled': True, 'operation_ref': handle.ref.to_dict(), 'request_id': old.request_id}})
        result = await handle.wait_search_reconciliation(control.request_id, timeout=.1)
        assert result.ref == handle.ref and posts == [control]
    asyncio.run(scenario())
