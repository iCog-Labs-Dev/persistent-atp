"""Optional async HTTP binding. Install persistent-atp-client[http].

Connections belong to one request, allowing SyncClient to reuse an adapter
across separate event loops without retaining loop-bound connection pools.
Cancellation stops observation, never establishes server cancellation.
"""
from __future__ import annotations

import base64
import asyncio
from dataclasses import fields
from functools import wraps
import json
import math
import urllib.parse

from ..codecs import to_dict
from ..errors import (
    ArtifactUnavailable, BackendUnavailable, CursorExpired, Forbidden, HashMismatch,
    IdempotencyConflict, InvalidRequest, QuotaExceeded, RateLimited, RequestIdExpired,
    ReviewRequired, StalePrecondition, TransientUnavailable, Unauthenticated,
    UnsupportedCapability, UnsupportedContract,
    ReceiverProtocolError,
)

# Allows a 16 MiB artifact in its JSON/base64 envelope.
MAX_RESPONSE_BYTES = 24 * 1024 * 1024


def _protocol(method):
    @wraps(method)
    async def checked(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except ReceiverProtocolError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ReceiverProtocolError(f"invalid receiver response for {method.__name__}") from exc
    return checked


def _array(raw):
    if not isinstance(raw, list):
        raise ReceiverProtocolError("receiver collection must be an array")
    return raw


def _string_set(raw):
    values = _array(raw)
    if not all(isinstance(item, str) for item in values) or len(set(values)) != len(values):
        raise ReceiverProtocolError("capabilities must contain unique strings")
    return frozenset(values)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate receiver JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite receiver JSON")))
from ..models import (
    Admission, ArtifactRef, CapabilitySet, CoordinationItem, CoordinationPage, Event, OperationRef,
    OperationSnapshot, ProjectDigest,
)


def _ref(raw):
    if raw is None:
        return None
    _exact(raw, OperationRef)
    return OperationRef(**raw)


def _exact(raw, cls):
    if not isinstance(raw, dict) or set(raw) != {item.name for item in fields(cls)}:
        raise ReceiverProtocolError(f"invalid {cls.__name__} response fields")


def _admission(raw):
    _exact(raw, Admission)
    return Admission(raw["request_id"], raw["fingerprint"], raw["accepted"],
                     _ref(raw.get("operation_ref")), raw.get("error_code"), _ref(raw.get("coalesced_to")))


def _snapshot(raw):
    _exact(raw, OperationSnapshot)
    raw = dict(raw)
    raw["ref"] = _ref(raw["ref"])
    raw["evidence_artifacts"] = tuple(_artifact(ref) for ref in _array(raw["evidence_artifacts"]))
    return OperationSnapshot(**raw)


def _artifact(raw):
    _exact(raw, ArtifactRef)
    return ArtifactRef(**raw)


class HTTPReceiverAdapter:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            parsed.port
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and (parsed.scheme == "https" or parsed.hostname in {"127.0.0.1", "localhost", "::1"}))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("HTTP receiver requires a loopback or TLS URL without embedded credentials")
        if (not isinstance(token, str) or not token or any(ord(c) < 32 for c in token)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError("token and finite bounded timeout are required")
        try:
            import httpx
        except ImportError:
            raise ImportError("HTTP transport requires installing persistent-atp-client[http]") from None
        self._httpx = httpx
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._closed = False

    async def _request(self, method, path, body=None):
        if self._closed:
            raise RuntimeError("HTTP adapter is closed")
        data = None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._httpx.AsyncClient(timeout=self._timeout,
                        follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, self.base_url + path, content=data,
                            headers={"Authorization": "Bearer " + self._token,
                                     "Content-Type": "application/json"}) as response:
                        content = bytearray()
                        limit = 4096 if response.status_code >= 400 else MAX_RESPONSE_BYTES
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(content) + len(chunk) > limit:
                                if response.status_code >= 400:
                                    content.extend(chunk[:max(0, limit - len(content))])
                                    break
                                raise ReceiverProtocolError("receiver response exceeds the byte limit")
                            content.extend(chunk)
                        if 300 <= response.status_code < 400:
                            raise ReceiverProtocolError("receiver redirects are unsupported")
                        if response.status_code >= 400:
                            self._raise_http(response.status_code, content)
                        try:
                            return _json(content)
                        except (ValueError, UnicodeError) as exc:
                            raise ReceiverProtocolError("receiver returned malformed JSON") from exc
        except (self._httpx.TransportError, TimeoutError) as exc:
            raise TransientUnavailable("receiver transport unavailable") from exc

    @staticmethod
    def _raise_http(status, content):
        fallback = {400: InvalidRequest, 401: Unauthenticated, 403: Forbidden, 404: ArtifactUnavailable,
                    409: IdempotencyConflict, 410: CursorExpired, 422: UnsupportedCapability, 429: RateLimited}
        message = "HTTP_ERROR"
        try:
            payload = _json(content)
            if isinstance(payload, dict) and isinstance(payload.get("error_code"), str):
                message = payload["error_code"]
        except (ValueError, UnicodeError):
            pass
        named = {cls.code: cls for cls in (
            ArtifactUnavailable, BackendUnavailable, CursorExpired, Forbidden, HashMismatch,
            IdempotencyConflict, InvalidRequest, QuotaExceeded, RateLimited, RequestIdExpired,
            ReviewRequired, StalePrecondition, TransientUnavailable, Unauthenticated,
            UnsupportedCapability, UnsupportedContract)}
        raise named.get(message, fallback.get(status, TransientUnavailable))(message)

    @staticmethod
    def _path(*segments):
        return "/".join(urllib.parse.quote(str(item), safe="") for item in segments)

    @_protocol
    async def capabilities(self, *, contract_version="2.0"):
        path = "/v2/capabilities"
        if contract_version != "2.0":
            path += "?" + urllib.parse.urlencode({"contract_version": contract_version})
        raw = await self._request("GET", path)
        _exact(raw, CapabilitySet)
        return CapabilitySet(raw["contract_version"], _string_set(raw["actions"]),
                             _string_set(raw["controls"]), raw["backend_mode"],
                             raw["independent_replay"], raw["receiver_mode"])

    @_protocol
    async def submit(self, request):
        return _admission(await self._request("POST", "/v2/requests", to_dict(request)))

    @_protocol
    async def lookup_request(self, workspace_id, request_id):
        raw = await self._request("GET", "/v2/requests/" + self._path(workspace_id, request_id))
        return _admission(raw) if raw is not None else None

    @_protocol
    async def get_operation(self, ref):
        return _snapshot(await self._request("GET", "/v2/operations/" + self._path(ref.workspace_id, ref.operation_id)))

    @_protocol
    async def list_operations(self, *, workspace_id, proof_id=None, active_only=True, cursor=None):
        query = urllib.parse.urlencode({"proof_id": proof_id or "", "active_only": str(active_only).lower(), "cursor": cursor or ""})
        raw = await self._request("GET", "/v2/operations/" + self._path(workspace_id) + "?" + query)
        return tuple(_snapshot(item) for item in _array(raw))

    @_protocol
    async def read_status(self, *, workspace_id, proof_id, at_revision=None):
        path = "/v2/status/" + self._path(workspace_id, proof_id)
        if at_revision is not None:
            path += "?" + urllib.parse.urlencode({"at_revision": at_revision})
        raw = await self._request("GET", path)
        _exact(raw, ProjectDigest)
        raw["target_artifact"] = _artifact(raw["target_artifact"])
        return ProjectDigest(**raw)

    @_protocol
    async def read_coordination(self, *, workspace_id, proof_id, cursor=None, page_size=100):
        query = urllib.parse.urlencode({"page_size": page_size, "cursor": cursor or ""})
        raw = await self._request("GET", "/v2/coordination/" + self._path(workspace_id, proof_id) + "?" + query)
        _exact(raw, CoordinationPage)
        if not isinstance(raw["items"], list):
            raise ValueError("coordination items must be a list")
        items = []
        for item in raw["items"]:
            _exact(item, CoordinationItem)
            items.append(CoordinationItem(**item))
        return CoordinationPage(**{**raw, "items": tuple(items)})

    @_protocol
    async def read_events(self, ref, cursor=None):
        path = "/v2/events/" + self._path(ref.workspace_id, ref.operation_id)
        if cursor is not None:
            path += "?" + urllib.parse.urlencode({"cursor": cursor})
        raw = await self._request("GET", path)
        if not isinstance(raw, dict) or set(raw) != {"events", "cursor"}:
            raise ValueError("invalid event page fields")
        for item in _array(raw["events"]):
            _exact(item, Event)
        return tuple(Event(**{**item, "operation_ref": _ref(item["operation_ref"])}) for item in raw["events"]), raw["cursor"]

    @_protocol
    async def put_artifact(self, source, *, workspace_id, media_type, expected_hash=None):
        raw = await self._request("POST", "/v2/artifacts", {"workspace_id": workspace_id,
            "media_type": media_type, "expected_hash": expected_hash,
            "content_base64": base64.b64encode(source).decode("ascii")})
        return _artifact(raw)

    @_protocol
    async def get_artifact(self, ref, *, workspace_id):
        raw = await self._request("GET", "/v2/artifacts/" + self._path(workspace_id, ref.hash))
        if not isinstance(raw, dict) or set(raw) != {"content_base64"} or not isinstance(raw["content_base64"], str):
            raise ReceiverProtocolError("invalid artifact response")
        return base64.b64decode(raw["content_base64"], validate=True)

    async def cancel(self, control):
        raise UnsupportedCapability("HTTP reference control is unavailable")

    async def amend_budget(self, control):
        raise UnsupportedCapability("HTTP reference budget amendment is unavailable")

    async def answer_decision(self, control):
        raise UnsupportedCapability("HTTP reference decision workflow is unavailable")

    async def aclose(self):
        self._closed = True
