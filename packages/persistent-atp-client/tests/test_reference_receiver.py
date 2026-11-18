"""Opt-in integration with the existing reference service; no embedded service dependency."""
import asyncio
import json
import os
import uuid

import pytest

from persistent_atp_client import AsyncClient, OperationRef, Preconditions, ProofCreate, ResearchGuidance


@pytest.mark.skipif(not os.environ.get("PERSISTENT_ATP_CLIENT_TEST_URL"),
    reason="set PERSISTENT_ATP_CLIENT_TEST_URL and TOKEN for a reference receiver integration")
def test_reference_receiver_round_trip():
    pytest.importorskip("httpx", reason="install the client [http] extra")
    from persistent_atp_client.adapters.http import HTTPReceiverAdapter
    async def scenario():
        url = os.environ["PERSISTENT_ATP_CLIENT_TEST_URL"]
        token = os.environ["PERSISTENT_ATP_CLIENT_TEST_TOKEN"]
        workspace = os.environ.get("PERSISTENT_ATP_CLIENT_TEST_WORKSPACE", "reference")
        proof_id = "client_test_" + uuid.uuid4().hex[:12]
        async with AsyncClient(HTTPReceiverAdapter(url, token)) as client:
            assert (await client.capabilities()).receiver_mode == "reference-scripted"
            artifact = await client.put_artifact(json.dumps({"proof_id": proof_id,
                "title": "SDK integration", "literal_target": "∀ n : Nat, n = n"},
                ensure_ascii=False).encode(), workspace_id=workspace, media_type="application/json")
            prepared = client.prepare(ProofCreate(artifact),
                workspace_id=workspace)
            handle = await client.submit(prepared)
            assert (await handle.wait(timeout=10)).lifecycle == "succeeded"
            assert (await client.submit(prepared)).ref == handle.ref
            digest = await client.status(workspace_id=workspace, proof_id=proof_id)
            assert digest.literal_target == "∀ n : Nat, n = n" and not digest.formal_verified
            assert await client.get_artifact(artifact, workspace_id=workspace)
            cycle = await client.proof_cycle(proof_id, workspace_id=workspace, category="critic-task")
            outcome = await cycle.wait(timeout=10)
            assert outcome.lifecycle == "succeeded", outcome.result
            assert outcome.result["cycle_disposition"] == "pending-review"
            async def consume(): return [event.event_type async for event in cycle.events()]
            assert await asyncio.wait_for(consume(), timeout=5) == ["queued", "running", "succeeded"]
            digest = await client.status(workspace_id=workspace, proof_id=proof_id)
            guidance = await client.queue_research_moves(proof_id, "integration-guidance",
                (ResearchGuidance("bridge-lemma", "Investigate an intermediate equality", proof_id + "/c-1"),),
                workspace_id=workspace, preconditions=Preconditions(expected_revision=digest.revision))
            assert (await guidance.wait(timeout=10)).lifecycle == "succeeded"
            first = page = await client.coordination_page(proof_id, workspace_id=workspace, page_size=1)
            records = list(page.items)
            while page.next_cursor:
                page = await client.coordination_page(proof_id, workspace_id=workspace, cursor=page.next_cursor, page_size=1)
                assert (page.revision, page.journal_hash) == (first.revision, first.journal_hash)
                records.extend(page.items)
            assert len(records) == page.total_items
            ref = OperationRef.from_dict(guidance.ref.to_dict())
        async with AsyncClient(HTTPReceiverAdapter(url, token)) as replacement:
            assert (await replacement.attach(ref).snapshot()).lifecycle == "succeeded"
    asyncio.run(scenario())
