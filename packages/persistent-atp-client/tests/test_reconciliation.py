"""Recovery must distinguish an absent request from an unavailable lookup."""
import asyncio
from dataclasses import replace

import pytest

from persistent_atp_client import AsyncClient, ProofCycle
from persistent_atp_client.errors import ReceiverProtocolError, SubmissionUncertain, TransientUnavailable
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
