"""Durable execution associations and canonical provider reconciliation.

These records are operational metadata. Only CommitGate writes proof history.
"""
from __future__ import annotations

import base64
import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote
from abc import ABC, abstractmethod

from artifacts.hashing import hash_content
from mathproof.http_atp import ATPServiceError, ATPRunNotFound, HTTPFormalATP
from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate provider JSON key")
        result[key] = value
    return result


def _finite_json(value):
    raise ValueError("nonfinite provider JSON value")


class BoundedHTTPFormalATP(HTTPFormalATP):
    """Cooperative total request deadline, including streamed body consumption."""
    def _call(self, method, path, payload=None):
        import httpx
        async def send():
            headers = {"Accept": "application/json"}
            if self.token: headers["Authorization"] = "Bearer " + self.token
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                    async with client.stream(method, self.base_url + path, json=payload, headers=headers) as response:
                        if response.status_code == 404:
                            raise ATPRunNotFound("ATP resource was not found")
                        if response.status_code >= 300:
                            raise ATPServiceError(f"ATP service returned HTTP {response.status_code}")
                        raw = bytearray()
                        async for part in response.aiter_bytes(chunk_size=65_536):
                            raw.extend(part)
                            if len(raw) > 24 * 1024 * 1024:
                                raise ATPServiceError("ATP response exceeds the byte limit")
            value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_finite_json)
            if not isinstance(value, dict): raise ValueError("provider response must be an object")
            return value
        try:
            return asyncio.run(send())
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise ATPServiceError("ATP request failed or returned invalid JSON") from exc


class ExecutionPending(Exception):
    """Execution is running or uncertain; retain its original identities."""


class ExecutionStore:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS executions (
            operation_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
            request BLOB NOT NULL, request_artifact TEXT NOT NULL,
            executor_id TEXT NOT NULL, phase TEXT NOT NULL,
            result_artifact TEXT, commit_revision INTEGER, commit_hash TEXT
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS replay_attempts (
            run_id TEXT PRIMARY KEY, certificate_hash TEXT NOT NULL,
            environment_hash TEXT NOT NULL, result_artifact TEXT NOT NULL)""")

    def get(self, operation_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM executions WHERE operation_id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def prepare(self, operation_id: str, request: dict, request_artifact: str, executor_id: str):
        body = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(operation_id)
                if old and (old["request"] != body or old["executor_id"] != executor_id):
                    raise ValueError("operation execution identity changed")
                self.db.execute("INSERT OR IGNORE INTO executions VALUES (?,?,?,?,?,'prepared',NULL,NULL,NULL)",
                                (operation_id, request["run_id"], body, request_artifact, executor_id))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def update(self, operation_id: str, *, phase: str, result_artifact: str | None = None,
               revision: int | None = None, journal_hash: str | None = None):
        with self.lock:
            self.db.execute("""UPDATE executions SET phase=?,
                result_artifact=COALESCE(?,result_artifact),
                commit_revision=COALESCE(?,commit_revision),
                commit_hash=COALESCE(?,commit_hash) WHERE operation_id=?""",
                (phase, result_artifact, revision, journal_hash, operation_id))

    def close(self):
        self.db.close()

    def replay(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM replay_attempts WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def save_replay(self, run_id, certificate_hash, environment_hash, result_artifact):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.replay(run_id)
                row = {"run_id": run_id, "certificate_hash": certificate_hash,
                       "environment_hash": environment_hash, "result_artifact": result_artifact}
                if existing is not None and existing != row:
                    raise ValueError("replay evidence cannot be rebound")
                self.db.execute("INSERT OR IGNORE INTO replay_attempts VALUES (?,?,?,?)",
                    (run_id, certificate_hash, environment_hash, result_artifact))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise


class ProductionExecutor(ABC):
    mode = "real"

    @abstractmethod
    def restore(self, request): ...

    @abstractmethod
    def observe_or_start(self, request, *, allow_start=True): ...

    @abstractmethod
    def materialize(self, pointer, artifacts): ...


class CanonicalExecutor(ProductionExecutor):
    kind = "canonical-http"
    """Real HTTP transport with explicit provider recovery guarantees.

    The provider must advertise these guarantees at GET /capabilities. They
    are part of the trusted deployment contract, not a proof certificate.
    This executor never performs or claims independent replay.
    """
    REQUIRED = ("idempotent_start", "durable_runs", "complete_local_context", "certificate_witnesses")
    mode = "real"

    def __init__(self, endpoint: str, *, token: str | None = None, timeout: float = 10):
        self.adapter = BoundedHTTPFormalATP(endpoint, token=token, timeout=timeout)
        capabilities = self.adapter._call("GET", "/capabilities")
        if capabilities.get("backend_mode") != "real" or any(
            capabilities.get(name) is not True for name in self.REQUIRED
        ):
            raise ValueError("formal provider lacks required production recovery/identity capabilities")
        provider_id = capabilities.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("formal provider must expose a stable provider_id")
        self.identity = hash_content(json.dumps([endpoint.rstrip('/'), provider_id]).encode())

    def observe_or_start(self, request: dict, *, allow_start: bool = True) -> dict:
        self.adapter.restore_request(request)
        try:
            # Always reconcile first, including after an ambiguous start.
            return dict(self.adapter.formal_search_status(request["run_id"]))
        except ATPRunNotFound:
            if not allow_start:
                from persistent_atp_client.errors import StalePrecondition
                raise StalePrecondition("original execution lease/revision no longer permits a start")
            # Advertised idempotency covers a delayed original submission racing
            # with this lookup. Never mint a replacement run ID or request body.
            return dict(self.adapter.formal_search_start(request))

    def restore(self, request):
        self.adapter.restore_request(request)

    def materialize(self, pointer: str, artifacts) -> None:
        if artifacts.exists(pointer):
            content = artifacts.get(pointer)
        else:
            body = self.adapter._call("GET", "/artifacts/" + quote(pointer, safe=""))
            if set(body) != {"content_base64"} or not isinstance(body["content_base64"], str):
                raise ValueError("invalid provider artifact envelope")
            content = base64.b64decode(body["content_base64"], validate=True)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("provider artifact exceeds the byte limit")
        if hash_content(content) != pointer:
            raise ValueError("provider artifact hash mismatch")
        if artifacts.put(content) != pointer:
            raise ValueError("artifact store returned a different content identity")


class BoundedMathsAIHTTPFormalATP(MathsAIHTTPFormalATP):
    def _call(self, method, path, payload=None, *, expected_statuses=frozenset({200})):
        import httpx
        async def send():
            headers = {"Accept": "application/json"}
            if self.token: headers["Authorization"] = "Bearer " + self.token
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                    async with client.stream(method, self.base_url + path, json=payload, headers=headers) as response:
                        if response.status_code not in expected_statuses:
                            raise MathsAIAPIError("Mathematics-ai request was rejected", status_code=response.status_code)
                        raw = bytearray()
                        async for part in response.aiter_bytes(chunk_size=65_536):
                            raw.extend(part)
                            if len(raw) > 24 * 1024 * 1024:
                                raise MathsAIAPIError("Mathematics-ai response exceeds byte limit")
            result = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_finite_json)
            if not isinstance(result, dict): raise ValueError("provider response must be an object")
            return result
        try: return asyncio.run(send())
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise MathsAIAPIError("Mathematics-ai transport failed or returned invalid JSON") from exc


class MathsAIExecutor(ProductionExecutor):
    """Strict translation of the deployed Mathematics-ai provider contract."""
    kind = "mathematics-ai-http"
    def __init__(self, endpoint, *, token=None, timeout=10):
        import re
        self.adapter = BoundedMathsAIHTTPFormalATP(endpoint, token=token, timeout=timeout, strict=True)
        capabilities = self.adapter._call("GET", "/capabilities")
        required = ("idempotent_start", "durable_runs", "complete_local_context", "complete_proof_traces", "step_budget", "replay")
        if (capabilities.get("contract") != "persistent-atp-maths-ai-v1"
                or capabilities.get("backend_mode") != "real"
                or any(capabilities.get(name) is not True for name in required)):
            raise ValueError("Mathematics-ai provider lacks the required real execution contract")
        provider_id = capabilities.get("provider_id")
        self.environment_hash = capabilities.get("environment_hash")
        if (not isinstance(provider_id, str) or not provider_id.strip()
                or not isinstance(self.environment_hash, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.environment_hash)):
            raise ValueError("Mathematics-ai provider lacks stable identity/actual environment evidence")
        self.identity = hash_content(json.dumps([endpoint.rstrip('/'), provider_id, capabilities['contract']]).encode())

    def restore(self, request):
        if request["environment_hash"] != self.environment_hash:
            from persistent_atp_client.errors import StalePrecondition
            raise StalePrecondition("configured Mathematics-ai environment differs from the pinned request")
        self.adapter.restore_request(request)

    def observe_or_start(self, request, *, allow_start=True):
        self.restore(request)
        try: return dict(self.adapter.formal_search_status(request["run_id"]))
        except MathsAIAPIError as exc:
            if exc.status_code != 404: raise
            if not allow_start:
                from persistent_atp_client.errors import StalePrecondition
                raise StalePrecondition("original lease/revision does not permit a new provider start") from exc
            return dict(self.adapter.formal_search_start(request))

    def materialize(self, pointer, artifacts):
        content = artifacts.get(pointer) if artifacts.exists(pointer) else self.adapter.artifact_bytes.get(pointer)
        if content is None:
            raise ATPServiceError("Mathematics-ai artifact bytes are unavailable")
        if len(content) > 16 * 1024 * 1024 or hash_content(content) != pointer:
            raise ValueError("Mathematics-ai artifact bytes differ from their reference")
        if artifacts.put(content) != pointer:
            raise ValueError("artifact store returned a different content identity")
