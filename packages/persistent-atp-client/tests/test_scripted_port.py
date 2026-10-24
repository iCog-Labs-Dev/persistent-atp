"""Interface behavior only: this port cannot provide proof or replay evidence."""
import asyncio

import pytest

from persistent_atp_client import (
    AsyncClient, Budget, BudgetAmendment, CancelRequest, DecisionResponse,
    ProofCycle,
)
from persistent_atp_client.errors import IdempotencyConflict, StalePrecondition, UnsupportedCapability
from persistent_atp_client.testing import ScriptedReceiverPort


def test_scripted_port_admission_events_and_no_formal_authority():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        caps = await client.capabilities()
        assert caps.receiver_mode == caps.backend_mode == "scripted-test"
        assert caps.independent_replay is False
        request = client.prepare(ProofCycle("p1"), workspace_id="lab", request_id="stable")
        handle = await client.submit(request)
        assert len(await client.list_operations(workspace_id="lab", proof_id="p1")) == 1
        assert (await client.submit(request)).ref == handle.ref
        assert (await client.lookup_request(workspace_id="lab", request_id="stable")).operation_ref == handle.ref
        page, cursor = await port.read_events(handle.ref)
        assert [event.event_type for event in page] == ["queued"]
        port.finish(handle.ref, mathematical_disposition="stagnated")
        result = await handle.wait(timeout=0.2)
        assert result.lifecycle == "succeeded"
        assert result.mathematical_disposition == "stagnated"
        page, next_cursor = await port.read_events(handle.ref, cursor)
        assert [event.event_type for event in page] == ["succeeded"]
        assert next_cursor != cursor
        with pytest.raises(UnsupportedCapability):
            await client.status(workspace_id="lab", proof_id="p1")
        with pytest.raises(IdempotencyConflict):
            await client.submit(client.prepare(ProofCycle("p2"), workspace_id="lab", request_id="stable"))
    asyncio.run(scenario())


def test_scripted_controls_are_idempotent_and_versioned():
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        handle = await client.proof_cycle("p1", workspace_id="lab")
        budget = BudgetAmendment("budget-1", handle.ref, 0, Budget(2, 1000))
        assert await handle.amend_budget(budget) == await handle.amend_budget(budget)
        with pytest.raises(StalePrecondition):
            await handle.amend_budget(BudgetAmendment("budget-2", handle.ref, 0, Budget(2, 1000)))
        decision = DecisionResponse("decision-1", handle.ref, "d1", 0, "keep-target", "sha256:" + "a" * 64)
        assert await handle.answer_decision(decision) == await handle.answer_decision(decision)
        cancel = CancelRequest("cancel-1", handle.ref, expected_observation_version=0)
        assert (await handle.cancel(cancel)).lifecycle == "cancelled"
        assert (await handle.cancel(cancel)).lifecycle == "cancelled"
        with pytest.raises(StalePrecondition):
            await handle.cancel(CancelRequest("cancel-2", handle.ref, expected_observation_version=0))
    asyncio.run(scenario())
