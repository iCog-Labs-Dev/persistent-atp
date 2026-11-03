"""A prepared request deadline bounds delivery and reconciliation together."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from persistent_atp_client import AsyncClient, ProofCycle
from persistent_atp_client.codecs import fingerprint
from persistent_atp_client.errors import StalePrecondition, SubmissionUncertain
from persistent_atp_client.testing import ScriptedReceiverPort


def test_expired_request_is_rejected_before_any_port_call():
    class NoCalls(ScriptedReceiverPort):
        async def capabilities(self): raise AssertionError("expired request reached receiver")
    async def scenario():
        client = AsyncClient(NoCalls())
        request = client.prepare(ProofCycle("p1"), workspace_id="lab",
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(StalePrecondition):
            await client.submit(request)
    asyncio.run(scenario())


def test_deadline_during_negotiation_never_submits():
    class SlowCapabilities(ScriptedReceiverPort):
        async def capabilities(self): await asyncio.Event().wait()
    async def scenario():
        port = SlowCapabilities()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab",
            deadline=datetime.now(timezone.utc) + timedelta(seconds=.03))
        with pytest.raises(StalePrecondition):
            await client.submit(request)
        assert not port._admissions
    asyncio.run(scenario())


def test_delivery_timeout_retains_identity_without_starting_a_new_lookup_budget():
    class LostAcknowledgment(ScriptedReceiverPort):
        lookups = 0
        async def submit(self, request):
            await super().submit(request)
            await asyncio.Event().wait()
        async def lookup_request(self, *args):
            self.lookups += 1
            return await super().lookup_request(*args)
    async def scenario():
        port = LostAcknowledgment()
        client = AsyncClient(port)
        request = client.prepare(ProofCycle("p1"), workspace_id="lab",
            deadline=datetime.now(timezone.utc) + timedelta(seconds=.03))
        before = fingerprint(request)
        with pytest.raises(SubmissionUncertain) as error:
            await client.submit(request)
        assert error.value.request is request and fingerprint(request) == before
        assert len(port._admissions) == 1 and port.lookups == 0
        # The caller may subsequently reconcile the same identity explicitly.
        assert await client.lookup_request(workspace_id="lab", request_id=request.request_id)
    asyncio.run(scenario())
