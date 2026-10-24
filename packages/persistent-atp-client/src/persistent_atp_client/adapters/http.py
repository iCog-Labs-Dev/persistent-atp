"""Optional bounded HTTP binding for a v2 receiver.

The reference transport uses stdlib urllib so the independently installable
core has no network dependency. Calls are synchronous inside async methods;
high-concurrency deployments should provide an async transport implementing
the same ReceiverPort and conformance suite.
"""
from __future__ import annotations

import base64
from dataclasses import fields
import json
import urllib.error
import urllib.parse
import urllib.request

from ..codecs import to_dict
from ..errors import (
    ArtifactUnavailable, BackendUnavailable, CursorExpired, Forbidden, HashMismatch,
    IdempotencyConflict, InvalidRequest, QuotaExceeded, RateLimited, RequestIdExpired,
    ReviewRequired, StalePrecondition, TransientUnavailable, Unauthenticated,
    UnsupportedCapability, UnsupportedContract,
)
from ..models import (
    Admission, ArtifactRef, CapabilitySet, Event, OperationRef,
    OperationSnapshot, ProjectDigest,
)


def _ref(raw):
    if raw is None:
        return None
    _exact(raw, OperationRef)
    return OperationRef(**raw)


def _exact(raw, cls):
    if not isinstance(raw, dict) or set(raw) != {item.name for item in fields(cls)}:
        raise ValueError(f"invalid {cls.__name__} response fields")


def _admission(raw):
    _exact(raw, Admission)
    return Admission(raw["request_id"], raw["fingerprint"], raw["accepted"],
                     _ref(raw.get("operation_ref")), raw.get("error_code"), _ref(raw.get("coalesced_to")))


def _snapshot(raw):
    _exact(raw, OperationSnapshot)
    raw = dict(raw)
    raw["ref"] = _ref(raw["ref"])
    raw["evidence_artifacts"] = tuple(_artifact(ref) for ref in raw["evidence_artifacts"])
    return OperationSnapshot(**raw)


def _artifact(raw):
    _exact(raw, ArtifactRef)
    return ArtifactRef(**raw)


class HTTPReceiverAdapter:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
        if not base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise ValueError("HTTP receiver requires loopback or TLS")
        if not token or timeout <= 0 or timeout > 60:
            raise ValueError("token and bounded timeout are required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                return json.loads(response.read(20 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            codes = {400: InvalidRequest, 401: Unauthenticated, 403: Forbidden, 404: ArtifactUnavailable,
                     409: IdempotencyConflict, 410: CursorExpired, 422: UnsupportedCapability}
            try:
                payload = json.loads(exc.read(4096))
                message = payload.get("error_code", "HTTP_ERROR")
            except (ValueError, OSError):
                message = "HTTP_ERROR"
            named = {cls.code: cls for cls in (
                ArtifactUnavailable, BackendUnavailable, CursorExpired, Forbidden,
                HashMismatch, IdempotencyConflict, InvalidRequest, QuotaExceeded,
                RateLimited, RequestIdExpired, ReviewRequired, StalePrecondition,
                TransientUnavailable, Unauthenticated, UnsupportedCapability,
                UnsupportedContract)}
            raise named.get(message, codes.get(exc.code, TransientUnavailable))(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientUnavailable("receiver transport unavailable") from exc

    @staticmethod
    def _path(*segments):
        return "/".join(urllib.parse.quote(str(item), safe="") for item in segments)

    async def capabilities(self):
        raw = self._request("GET", "/v2/capabilities")
        _exact(raw, CapabilitySet)
        return CapabilitySet(raw["contract_version"], frozenset(raw["actions"]),
                             frozenset(raw["controls"]), raw["backend_mode"],
                             raw["independent_replay"], raw["receiver_mode"])

    async def submit(self, request):
        return _admission(self._request("POST", "/v2/requests", to_dict(request)))

    async def lookup_request(self, workspace_id, request_id):
        raw = self._request("GET", "/v2/requests/" + self._path(workspace_id, request_id))
        return _admission(raw) if raw is not None else None

    async def get_operation(self, ref):
        return _snapshot(self._request("GET", "/v2/operations/" + self._path(ref.workspace_id, ref.operation_id)))

    async def list_operations(self, *, workspace_id, proof_id=None, active_only=True, cursor=None):
        query = urllib.parse.urlencode({"proof_id": proof_id or "", "active_only": str(active_only).lower(), "cursor": cursor or ""})
        raw = self._request("GET", "/v2/operations/" + self._path(workspace_id) + "?" + query)
        return tuple(_snapshot(item) for item in raw)

    async def read_status(self, *, workspace_id, proof_id, at_revision=None):
        path = "/v2/status/" + self._path(workspace_id, proof_id)
        if at_revision is not None:
            path += "?" + urllib.parse.urlencode({"at_revision": at_revision})
        raw = self._request("GET", path)
        _exact(raw, ProjectDigest)
        raw["target_artifact"] = _artifact(raw["target_artifact"])
        return ProjectDigest(**raw)

    async def read_events(self, ref, cursor=None):
        path = "/v2/events/" + self._path(ref.workspace_id, ref.operation_id)
        if cursor is not None:
            path += "?" + urllib.parse.urlencode({"cursor": cursor})
        raw = self._request("GET", path)
        if not isinstance(raw, dict) or set(raw) != {"events", "cursor"}:
            raise ValueError("invalid event page fields")
        for item in raw["events"]:
            _exact(item, Event)
        return tuple(Event(**{**item, "operation_ref": _ref(item["operation_ref"])}) for item in raw["events"]), raw["cursor"]

    async def put_artifact(self, source, *, workspace_id, media_type, expected_hash=None):
        raw = self._request("POST", "/v2/artifacts", {"workspace_id": workspace_id,
            "media_type": media_type, "expected_hash": expected_hash,
            "content_base64": base64.b64encode(source).decode("ascii")})
        return _artifact(raw)

    async def get_artifact(self, ref, *, workspace_id):
        raw = self._request("GET", "/v2/artifacts/" + self._path(workspace_id, ref.hash))
        return base64.b64decode(raw["content_base64"], validate=True)

    async def cancel(self, control):
        raise UnsupportedCapability("HTTP reference control is unavailable")

    async def amend_budget(self, control):
        raise UnsupportedCapability("HTTP reference budget amendment is unavailable")

    async def answer_decision(self, control):
        raise UnsupportedCapability("HTTP reference decision workflow is unavailable")

    async def aclose(self):
        return None
