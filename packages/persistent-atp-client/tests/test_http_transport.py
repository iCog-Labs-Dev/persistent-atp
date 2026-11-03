"""HTTP fault injection with the real async codec/adapter and a scripted receiver."""
import asyncio
import base64
import hashlib
import builtins
import json

import pytest

httpx = pytest.importorskip("httpx", reason="install the client [dev] or [http] extra")

from persistent_atp_client import (AsyncClient, CapabilitySet, OperationRef, OperationSnapshot,
    ProjectDigest, ArtifactRef, ProofCycle, SyncClient)
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.codecs import request_from_json, to_dict
from persistent_atp_client.errors import (ObservationTimeout, ReceiverProtocolError,
    SubmissionUncertain, TransientUnavailable, Forbidden, RateLimited)
from persistent_atp_client.testing import ScriptedReceiverPort

HASH = "sha256:" + "a" * 64


@pytest.fixture
def transport(monkeypatch):
    factory = httpx.AsyncClient
    def install(handler, **options):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
            factory(transport=httpx.MockTransport(handler), **kwargs))
        return HTTPReceiverAdapter("http://127.0.0.1:8765", "test-only-token", **options)
    return install


@pytest.mark.parametrize("lookup_bad", [False, True])
def test_admitted_request_with_truncated_http_ack_is_reconciled(transport, lookup_bad):
    port = ScriptedReceiverPort()
    methods = []
    async def handler(request):
        methods.append(request.method)
        assert request.headers["Authorization"] == "Bearer test-only-token"
        if request.url.path == "/v2/capabilities":
            return httpx.Response(200, json=to_dict(await port.capabilities()))
        if request.method == "POST":
            await port.submit(request_from_json(request.content))
            return httpx.Response(200, content=b'{"accepted":')
        workspace, request_id = request.url.path.split("/")[-2:]
        if lookup_bad:
            return httpx.Response(200, content=b'{"bad":')
        receipt = await port.lookup_request(workspace, request_id)
        return httpx.Response(200, json=to_dict(receipt))
    async def scenario():
        client = AsyncClient(transport(handler))
        request = client.prepare(ProofCycle("p1"), workspace_id="lab", request_id="stable")
        if lookup_bad:
            with pytest.raises(SubmissionUncertain) as error:
                await client.submit(request)
            assert error.value.request is request
        else:
            handle = await client.submit(request)
            assert handle.ref == (await port.lookup_request("lab", "stable")).operation_ref
        assert methods == ["GET", "POST", "GET"]
        assert len(port._admissions) == 1
    asyncio.run(scenario())


def test_http_observation_deadline_interrupts_an_inflight_get(transport):
    entered = cancelled = None
    async def scenario():
        nonlocal entered, cancelled
        entered, cancelled = asyncio.Event(), asyncio.Event()
        requests = []
        async def handler(request):
            requests.append(request.method)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client = AsyncClient(transport(handler, timeout=1))
        handle = client.attach(OperationRef("lab", "op1"))
        async with asyncio.timeout(2):
            with pytest.raises(ObservationTimeout):
                await handle.wait(timeout=.03)
        assert entered.is_set() and cancelled.is_set()
        assert requests == ["GET"]  # Observation does not send a remote cancel.
    asyncio.run(scenario())


def test_http_calls_for_independent_operations_overlap(transport):
    async def scenario():
        both = asyncio.Event()
        arrived = 0
        async def handler(request):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=1)
            workspace, operation_id = request.url.path.split("/")[-2:]
            return httpx.Response(200, json=to_dict(OperationSnapshot(
                OperationRef(workspace, operation_id), "r1", 1, "succeeded")))
        client = AsyncClient(transport(handler))
        results = await asyncio.gather(*(client.attach(OperationRef("lab", op)).snapshot() for op in ("op1", "op2")))
        assert {item.ref.operation_id for item in results} == {"op1", "op2"}
    asyncio.run(scenario())


def test_transport_has_a_total_response_deadline(transport):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.01)
                yield b" "
    async def handler(request):
        return httpx.Response(200, stream=SlowBody())
    async def scenario():
        adapter = transport(handler, timeout=.03)
        with pytest.raises(TransientUnavailable):
            await adapter.capabilities()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["string-flag", "string-actions", "duplicate-actions", "duplicate-json", "nonfinite-json"])
def test_http_capability_data_is_strict(transport, bad):
    def handler(request):
        raw = to_dict(CapabilitySet("2.0", frozenset({"proof-cycle"})))
        if bad == "string-flag": raw["independent_replay"] = "false"
        if bad == "string-actions": raw["actions"] = "proof-cycle"
        if bad == "duplicate-actions": raw["actions"] = ["proof-cycle", "proof-cycle"]
        if bad == "duplicate-json":
            return httpx.Response(200, content=b'{"actions":[],"actions":[]}')
        if bad == "nonfinite-json":
            return httpx.Response(200, content=b'{"independent_replay":NaN}')
        return httpx.Response(200, json=raw)
    async def scenario():
        with pytest.raises(ReceiverProtocolError):
            await AsyncClient(transport(handler)).capabilities()
    asyncio.run(scenario())


def test_http_status_scope_is_checked_after_decoding(transport):
    def handler(request):
        return httpx.Response(200, json=to_dict(ProjectDigest("other", "p2", 1, HASH, 1,
            ArtifactRef(HASH), "Other theorem", "lean-verified", True, "scripted mismatch")))
    async def scenario():
        with pytest.raises(ReceiverProtocolError):
            await AsyncClient(transport(handler)).status(workspace_id="lab", proof_id="p1")
    asyncio.run(scenario())


@pytest.mark.parametrize("status,error", [(403, Forbidden), (429, RateLimited)])
def test_non_json_http_errors_preserve_status_semantics(transport, status, error):
    async def scenario():
        adapter = transport(lambda request: httpx.Response(status, content=b"not JSON"))
        with pytest.raises(error):
            await adapter.capabilities()
    asyncio.run(scenario())


def test_http_response_byte_limit_is_enforced(transport, monkeypatch):
    from persistent_atp_client.adapters import http
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 32)
    async def scenario():
        adapter = transport(lambda request: httpx.Response(200, content=b" " * 33))
        with pytest.raises(ReceiverProtocolError, match="byte limit"):
            await adapter.capabilities()
    asyncio.run(scenario())


def test_http_redirects_do_not_forward_credentials_to_a_different_origin(transport):
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://different.example/v2/capabilities"})
    async def scenario():
        with pytest.raises(ReceiverProtocolError, match="redirects"):
            await transport(handler).capabilities()
        assert len(requests) == 1
    asyncio.run(scenario())


def test_a_maximum_size_artifact_fits_the_bounded_json_envelope(transport):
    data = b"x" * (16 * 1024 * 1024)
    ref = ArtifactRef("sha256:" + hashlib.sha256(data).hexdigest(), len(data))
    def handler(request):
        return httpx.Response(200, json={"content_base64": base64.b64encode(data).decode("ascii")})
    async def scenario():
        client = AsyncClient(transport(handler))
        assert await client.get_artifact(ref, workspace_id="lab") == data
    asyncio.run(scenario())


def test_http_adapter_can_be_reused_by_sync_client_across_loops(transport):
    raw = to_dict(CapabilitySet("2.0", frozenset({"proof-cycle"})))
    client = SyncClient(AsyncClient(transport(lambda request: httpx.Response(200, json=raw))))
    assert client.capabilities() == client.capabilities()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.capabilities()


@pytest.mark.parametrize("url", ["http://localhost:8765@evil.example", "http://localhost.evil.example",
    "https://user:password@example.org", "https://example.org?token=secret"])
def test_receiver_urls_cannot_redirect_credentials_through_userinfo(url):
    with pytest.raises(ValueError):
        HTTPReceiverAdapter(url, "token")


def test_missing_optional_transport_dependency_is_an_explicit_blocker(monkeypatch):
    original = builtins.__import__
    def without_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_httpx)
    with pytest.raises(ImportError, match=r"persistent-atp-client\[http\]"):
        HTTPReceiverAdapter("http://127.0.0.1:8765", "token")
