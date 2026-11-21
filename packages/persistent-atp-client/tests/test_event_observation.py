"""Event observation must terminate, yield fairly and leave execution alone."""
import asyncio
import math
from dataclasses import replace

import pytest

from persistent_atp_client import AsyncClient
from persistent_atp_client.models import OperationRef
from persistent_atp_client.errors import ReceiverProtocolError
from persistent_atp_client.testing import ScriptedReceiverPort


@pytest.mark.parametrize("invalid", ["scope", "order", "version", "changed-duplicate", "cursor"])
def test_checkpointed_event_page_rejects_invalid_receiver_data(invalid):
    async def scenario():
        port = ScriptedReceiverPort()
        client = AsyncClient(port)
        handle = await client.proof_cycle("p1", workspace_id="lab")
        port.finish(handle.ref)
        page, cursor = await port.read_events(handle.ref)
        if invalid == "scope":
            page = (replace(page[0], operation_ref=OperationRef("other", "op")),)
        elif invalid == "order":
            page = tuple(reversed(page))
        elif invalid == "version":
            page = (replace(page[0], observation_version=2), page[1])
        elif invalid == "changed-duplicate":
            page = (page[0], replace(page[0], event_type="changed"))
        else:
            cursor = "bad\nvalue"
        async def read_events(*args): return page, cursor
        port.read_events = read_events
        with pytest.raises(ReceiverProtocolError):
            await client.events_page(handle.ref)
    asyncio.run(scenario())


def test_terminal_stream_finishes_even_when_empty_page_retains_its_cursor():
    async def scenario():
        port = ScriptedReceiverPort()
        handle = await AsyncClient(port).proof_cycle("p1", workspace_id="lab")
        port.finish(handle.ref)
        original = port.read_events
        calls = 0
        async def bounded(*args):
            nonlocal calls
            calls += 1
            if calls > 4:
                raise AssertionError("event loop did not terminate")
            return await original(*args)
        port.read_events = bounded
        events = [event.event_type async for event in handle.events()]
        assert events == ["queued", "succeeded"]
        assert calls == 2
    asyncio.run(scenario())


def test_duplicate_only_pages_poll_without_spinning_and_allow_cancellation():
    async def scenario():
        port = ScriptedReceiverPort()
        handle = await AsyncClient(port).proof_cycle("p1", workspace_id="lab")
        page = await port.read_events(handle.ref)
        polled = asyncio.Event()
        calls = 0
        async def repeat(*args):
            nonlocal calls
            calls += 1
            if calls > 4:
                raise AssertionError("duplicate pages caused a busy loop")
            if calls > 1:
                polled.set()
            return page
        port.read_events = repeat
        stream = handle.events(poll_interval=1)
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(polled.wait(), timeout=1)
        assert calls == 2
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await handle.snapshot()).lifecycle == "queued"
        await stream.aclose()
    asyncio.run(scenario())


def test_event_page_with_no_next_cursor_uses_the_last_delivered_watermark():
    async def scenario():
        port = ScriptedReceiverPort()
        handle = await AsyncClient(port).proof_cycle("p1", workspace_id="lab")
        port.finish(handle.ref)
        original = port.read_events
        async def final_page(ref, cursor):
            page, _ = await original(ref, cursor)
            return page, None
        port.read_events = final_page
        assert [e.event_type async for e in handle.events()] == ["queued", "succeeded"]
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [0, -1, math.inf, math.nan, True])
def test_observation_intervals_are_finite_and_positive(invalid):
    async def scenario():
        handle = await AsyncClient(ScriptedReceiverPort()).proof_cycle("p1", workspace_id="lab")
        with pytest.raises(ValueError):
            await handle.wait(timeout=invalid)
        with pytest.raises(ValueError):
            await anext(handle.events(poll_interval=invalid))
    asyncio.run(scenario())


def test_event_page_rejects_a_malformed_cursor():
    async def scenario():
        port = ScriptedReceiverPort()
        handle = await AsyncClient(port).proof_cycle("p1", workspace_id="lab")
        async def invalid(*args): return (), {"forged": "cursor"}
        port.read_events = invalid
        with pytest.raises(ReceiverProtocolError):
            await anext(handle.events())
    asyncio.run(scenario())
