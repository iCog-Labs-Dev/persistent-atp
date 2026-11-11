"""Generic HTTP transport for providers implementing the canonical contract.

This adapter is intentionally provider-neutral.  A service that implements
persistent-atp's five JSON routes can be used without importing any of its
Python code.  Services with different APIs belong in small translation
adapters such as :mod:`mathproof.maths_ai_http`.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from mathproof.formal_atp import FormalATPAdapter
from mathproof.schemas import validate
from mathproof.soundness import validate_formal_search_result

__all__ = ["ATPServiceError", "ATPRunNotFound", "HTTPFormalATP"]


class ATPServiceError(RuntimeError):
    """Transport failure or response that violates the canonical contract."""


class ATPRunNotFound(ATPServiceError):
    """The provider authoritatively returned HTTP 404 for a requested resource."""


class HTTPFormalATP(FormalATPAdapter):
    """Call a remote ATP that natively implements persistent-atp's contract."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("base_url must be an HTTP(S) URL without query or fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._requests: dict[str, dict[str, Any]] = {}

    def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload_bytes = response.read(24 * 1024 * 1024 + 1)
                if len(payload_bytes) > 24 * 1024 * 1024:
                    raise ATPServiceError("ATP response exceeds the byte limit")
                body = json.loads(payload_bytes)
        except HTTPError as exc:
            if exc.code == 404:
                raise ATPRunNotFound("ATP resource was not found") from exc
            raise ATPServiceError(f"ATP service returned HTTP {exc.code}") from exc
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ATPServiceError(f"ATP service {method} {path} failed: {exc}") from exc
        if not isinstance(body, dict):
            raise ATPServiceError("ATP service response must be a JSON object")
        return body

    @staticmethod
    def _check_search(
        body: Mapping[str, Any], request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        errors = validate("formal-search-result", body)
        if errors:
            raise ATPServiceError("invalid formal-search-result: " + "; ".join(errors))
        for field, expected in (
            ("run_id", request["run_id"]),
            ("proof_id", request["proof_id"]),
            ("declaration_id", request["formal_declaration_id"]),
            ("environment_hash", request["environment_hash"]),
        ):
            if body.get(field) != expected:
                raise ATPServiceError(f"ATP service returned mismatched {field}")
        findings = validate_formal_search_result(
            body,
            pinned_environment_hash=request["environment_hash"],
        )
        if findings:
            raise ATPServiceError(
                "unsound formal-search-result: "
                + ", ".join(str(finding.reason) for finding in findings)
            )
        return body

    def _known(self, run_id: str) -> Mapping[str, Any]:
        if run_id not in self._requests:
            raise ValueError(f"unknown run_id {run_id!r}; start the run first")
        return self._requests[run_id]

    def restore_request(self, request: Mapping[str, Any]) -> None:
        """Restore trusted durable request metadata before observing an old run.

        This performs no delivery or implicit retry. Provider status is still
        checked against this exact canonical request by ``_check_search``.
        """
        errors = validate("formal-search-request", request)
        if errors:
            raise ValueError("invalid restored formal request")
        run_id = str(request["run_id"])
        existing = self._requests.get(run_id)
        if existing is not None and existing != dict(request):
            raise ValueError("run identity is already bound to another request")
        self._requests[run_id] = dict(request)

    def formal_search_start(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        errors = validate("formal-search-request", request)
        if errors:
            raise ValueError("invalid formal-search-request: " + "; ".join(errors))
        body = self._check_search(
            self._call("POST", "/formal-search/start", request),
            request,
        )
        self._requests[str(request["run_id"])] = dict(request)
        return body

    def formal_search_resume(
        self,
        run_id: str,
        budget: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        original = self._known(run_id)
        if budget is not None:
            errors = validate("formal-search-request", {**original, "budget": budget})
            if errors:
                raise ValueError("invalid resume budget: " + "; ".join(errors))
        path = "/formal-search/" + quote(run_id, safe="") + "/resume"
        payload = {"budget": dict(budget)} if budget is not None else {}
        return self._check_search(self._call("POST", path, payload), original)

    def formal_search_status(self, run_id: str) -> Mapping[str, Any]:
        original = self._known(run_id)
        path = "/formal-search/" + quote(run_id, safe="")
        return self._check_search(self._call("GET", path), original)

    def formal_search_cancel(self, run_id: str) -> Mapping[str, Any]:
        self._known(run_id)
        path = "/formal-search/" + quote(run_id, safe="") + "/cancel"
        body = self._call("POST", path, {})
        if body != {"run_id": run_id, "disposition": "cancelled"}:
            raise ATPServiceError("invalid cancellation response")
        return body

    def formal_replay(
        self,
        certificate: Mapping[str, Any],
        environment_hash: str,
    ) -> Mapping[str, Any]:
        body = self._call(
            "POST",
            "/formal-replay",
            {
                "certificate": dict(certificate),
                "environment_hash": environment_hash,
            },
        )
        if (
            body.get("status") not in {"verified", "rejected"}
            or body.get("environment_hash") != environment_hash
            or set(body) - {"status", "environment_hash", "rejection_reason"}
        ):
            raise ATPServiceError("invalid or unbound replay response")
        return body
