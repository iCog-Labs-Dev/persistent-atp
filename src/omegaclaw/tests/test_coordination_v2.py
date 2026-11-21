"""Coordinator-neutral reads and guidance through the trusted reference gate."""
import asyncio
import hashlib
import json

import pytest

from persistent_atp_client import AsyncClient, Preconditions, QueueResearchMoves, ResearchGuidance
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import StalePrecondition
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService


def test_coordination_read_guidance_selected_cycle_and_restart(tmp_path):
    async def scenario():
        service = OperationService(tmp_path)
        receiver = ReferenceReceiver(service)
        client = AsyncClient(LocalReceiverAdapter(receiver))
        assert "coordination-read" in (await client.capabilities(contract_version="2.1")).controls
        assert "queue-research-moves" not in (await client.capabilities()).actions
        spec = json.dumps({"proof_id": "coord", "title": "Coordination",
                           "literal_target": "∀ n : Nat, n = n"}).encode()
        artifact = await client.put_artifact(spec, workspace_id="reference", media_type="application/json")
        created = await client.proof_create(artifact, workspace_id="reference")
        assert (await created.wait(timeout=5)).lifecycle == "succeeded"

        first = await client.coordination_page("coord", workspace_id="reference", page_size=1)
        all_items = list(first.items)
        cursor = first.next_cursor
        while cursor:
            page = await client.coordination_page("coord", workspace_id="reference", cursor=cursor,
                                                  page_size=1)
            assert (page.revision, page.journal_hash) == (first.revision, first.journal_hash)
            all_items.extend(page.items)
            cursor = page.next_cursor
        assert len(all_items) == first.total_items
        assert {item.record_id for item in all_items if item.item_type == "node"} == {
            "coord/proof", "coord/c-1"}

        moves = (ResearchGuidance("bridge-lemma", "Investigate an intermediate equality", "coord/c-1"),
                 ResearchGuidance("representation-change", "Try a structural induction", "coord/c-1"))
        prepared = client.prepare(
            QueueResearchMoves("coord", "stall-round-one", moves),
            workspace_id="reference", request_id="guidance-one",
            preconditions=Preconditions(expected_revision=first.revision))
        handle = await client.submit(prepared)
        outcome = await handle.wait(timeout=5)
        assert outcome.lifecycle == "succeeded", outcome.result
        assert len(outcome.result["research_move_ids"]) == 2
        assert outcome.result["formal_verified"] is False
        assert service.store.verify_chain("coord") == 2
        assert (await client.submit(prepared)).ref == handle.ref
        changed = await client.queue_research_moves("coord", "stall-round-one",
            (ResearchGuidance("bridge-lemma", "Different guidance", "coord/c-1"),),
            workspace_id="reference", preconditions=Preconditions(expected_revision=2))
        rejected = await changed.wait(timeout=5)
        assert rejected.lifecycle == "failed"
        assert rejected.result["error_code"] == "ContractError"
        assert service.store.verify_chain("coord") == 2
        after = await client.coordination_page("coord", workspace_id="reference")
        assert {item.record_id for item in after.items if item.item_type == "frontier"} >= set(
            outcome.result["research_move_ids"])
        assert {item.record_id for item in after.items if item.item_type == "edge"} >= {
            "coord/coord-proposes-" + hashlib.sha256(b"stall-round-one").hexdigest()[:24] + "-0"}
        with pytest.raises(StalePrecondition):
            await client.coordination_page("coord", workspace_id="reference", cursor=first.next_cursor)
        with pytest.raises(StalePrecondition):
            await client.queue_research_moves("coord", "stale", moves,
                workspace_id="reference", preconditions=Preconditions(expected_revision=first.revision))

        research = await client.proof_cycle("coord", workspace_id="reference", category="research-move")
        research_result = await research.wait(timeout=5)
        assert research_result.lifecycle == "succeeded"
        assert research_result.result["error_code"] == "THREADKEEPER_UNAVAILABLE"
        assert research_result.result["selected_move_id"] in outcome.result["research_move_ids"]
        assert service.store.verify_chain("coord") == 2
        receiver.close()

        restored_service = OperationService(tmp_path)
        restored_receiver = ReferenceReceiver(restored_service)
        restored_client = AsyncClient(LocalReceiverAdapter(restored_receiver))
        assert (await restored_client.attach(handle.ref).snapshot()).result == outcome.result
        assert (await restored_client.submit(prepared)).ref == handle.ref
        assert restored_service.store.verify_chain("coord") == 2
        restored_page = await restored_client.coordination_page("coord", workspace_id="reference")
        assert restored_page.journal_hash == after.journal_hash
        restored_receiver.close()

    asyncio.run(scenario())
