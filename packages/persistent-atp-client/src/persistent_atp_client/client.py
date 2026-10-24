"""Client observation and identity handling; no authoritative local ledger."""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from .codecs import fingerprint
from .errors import (
    AdmissionRejected, CursorExpired, HashMismatch, ObservationTimeout, SubmissionUncertain,
    TransientUnavailable, UnsupportedCapability, UnsupportedContract,
)
from .models import (
    ACTION_TYPES, ActionRequest, Admission, ArtifactRef, BudgetAmendment,
    CancelRequest, DecisionResponse, Event, FormalResume, FormalSearch,
    LemmaBridge, OperationRef, OperationSnapshot, Preconditions, ProofAudit,
    ProofCreate, ProofCycle, ProofExport, StatementAlign, TERMINAL_LIFECYCLES,
)
from .ports import ReceiverPort


class AsyncClient:
    def __init__(self, port: ReceiverPort):
        self._port = port
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def prepare(self, action, *, workspace_id: str, preconditions: Preconditions | None = None,
                request_id: str | None = None, deadline: datetime | None = None) -> ActionRequest:
        return ActionRequest(request_id=request_id or uuid.uuid4().hex,
                             workspace_id=workspace_id, action=action,
                             preconditions=preconditions or Preconditions(), deadline=deadline)

    async def capabilities(self):
        caps = await self._port.capabilities()
        if caps.contract_version != "2.0":
            raise UnsupportedContract("receiver does not support contract 2.0")
        return caps

    async def submit(self, request: ActionRequest) -> "OperationHandle":
        if self._closed:
            raise RuntimeError("client is closed")
        if not isinstance(request, ActionRequest):
            raise TypeError("submit requires a prepared ActionRequest")
        caps = await self.capabilities()
        if request.action.kind not in caps.actions:
            raise UnsupportedCapability(f"{request.action.kind} is unavailable", request_id=request.request_id)
        expected = fingerprint(request)
        try:
            receipt = await self._port.submit(request)
        except (TimeoutError, ConnectionError, TransientUnavailable) as exc:
            try:
                receipt = await self._port.lookup_request(request.workspace_id, request.request_id)
            except (TimeoutError, ConnectionError, TransientUnavailable) as lookup_exc:
                raise SubmissionUncertain("submission and lookup unavailable", request=request) from lookup_exc
            if receipt is None:
                raise SubmissionUncertain("authoritative lookup found no admission; retry the same prepared request", request=request) from exc
        if not isinstance(receipt, Admission) or receipt.request_id != request.request_id or receipt.fingerprint != expected:
            raise ValueError("receiver admission identity mismatch")
        if not receipt.accepted:
            raise AdmissionRejected(receipt)
        if receipt.operation_ref.workspace_id != request.workspace_id:
            raise ValueError("receiver admission workspace mismatch")
        return OperationHandle(self, receipt.operation_ref, request_id=request.request_id)

    async def submit_action(self, action, *, workspace_id: str, preconditions: Preconditions | None = None,
                            request_id: str | None = None, deadline: datetime | None = None) -> "OperationHandle":
        request = self.prepare(action, workspace_id=workspace_id, preconditions=preconditions,
                               request_id=request_id, deadline=deadline)
        return await self.submit(request)

    async def proof_create(self, spec_artifact: ArtifactRef, *, workspace_id: str, **request_options):
        return await self.submit_action(ProofCreate(spec_artifact), workspace_id=workspace_id, **request_options)

    async def proof_cycle(self, proof_id: str, *, workspace_id: str, **request_options):
        return await self.submit_action(ProofCycle(proof_id), workspace_id=workspace_id, **request_options)

    async def proof_status(self, proof_id: str, *, workspace_id: str, at_revision: int | None = None):
        return await self.status(workspace_id=workspace_id, proof_id=proof_id, at_revision=at_revision)

    async def proof_audit(self, proof_id: str, *, workspace_id: str, **request_options):
        return await self.submit_action(ProofAudit(proof_id), workspace_id=workspace_id, **request_options)

    async def proof_export(self, proof_id: str, destination_alias: str, *, workspace_id: str, **request_options):
        return await self.submit_action(ProofExport(proof_id, destination_alias), workspace_id=workspace_id, **request_options)

    async def formal_search(self, proof_id: str, claim_id: str, budget, *, workspace_id: str, **request_options):
        return await self.submit_action(FormalSearch(proof_id, claim_id, budget), workspace_id=workspace_id, **request_options)

    async def formal_resume(self, proof_id: str, run_id: str, budget, *, workspace_id: str,
                            checkpoint_artifact: ArtifactRef | None = None, **request_options):
        return await self.submit_action(FormalResume(proof_id, run_id, budget, checkpoint_artifact),
                                        workspace_id=workspace_id, **request_options)

    async def statement_align(self, proof_id: str, claim_id: str, lean_source_artifact: ArtifactRef,
                              *, workspace_id: str, **request_options):
        return await self.submit_action(StatementAlign(proof_id, claim_id, lean_source_artifact),
                                        workspace_id=workspace_id, **request_options)

    async def lemma_bridge(self, proof_id: str, obstruction_id: str, *, workspace_id: str, **request_options):
        return await self.submit_action(LemmaBridge(proof_id, obstruction_id), workspace_id=workspace_id, **request_options)

    async def lookup_request(self, *, workspace_id: str, request_id: str) -> Admission | None:
        return await self._port.lookup_request(workspace_id, request_id)

    def attach(self, operation_ref: OperationRef) -> "OperationHandle":
        if not isinstance(operation_ref, OperationRef):
            raise TypeError("attach requires OperationRef")
        return OperationHandle(self, operation_ref)

    async def status(self, *, workspace_id: str, proof_id: str, at_revision: int | None = None) -> Any:
        return await self._port.read_status(workspace_id=workspace_id, proof_id=proof_id, at_revision=at_revision)

    async def list_operations(self, *, workspace_id: str, proof_id: str | None = None,
                              active_only: bool = True, cursor: str | None = None):
        return await self._port.list_operations(workspace_id=workspace_id, proof_id=proof_id,
                                                active_only=active_only, cursor=cursor)

    async def put_artifact(self, source: bytes, *, workspace_id: str, media_type: str,
                           expected_hash: str | None = None) -> ArtifactRef:
        if type(source) is not bytes or len(source) > 16 * 1024 * 1024:
            raise ValueError("source must be at most 16 MiB of bytes")
        actual = "sha256:" + hashlib.sha256(source).hexdigest()
        if expected_hash is not None and expected_hash != actual:
            raise HashMismatch("uploaded byte hash differs from expected hash")
        ref = await self._port.put_artifact(source, workspace_id=workspace_id,
                                            media_type=media_type, expected_hash=actual)
        if not isinstance(ref, ArtifactRef) or ref.hash != actual or ref.size_bytes != len(source):
            raise HashMismatch("receiver artifact identity mismatch")
        return ref

    async def get_artifact(self, ref: ArtifactRef, *, workspace_id: str) -> bytes:
        data = await self._port.get_artifact(ref, workspace_id=workspace_id)
        if type(data) is not bytes or len(data) > 16 * 1024 * 1024 or "sha256:" + hashlib.sha256(data).hexdigest() != ref.hash:
            raise HashMismatch("downloaded artifact bytes do not match reference")
        if ref.size_bytes is not None and len(data) != ref.size_bytes:
            raise HashMismatch("downloaded artifact size does not match reference")
        return data

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._port.aclose()


class OperationHandle:
    def __init__(self, client: AsyncClient, ref: OperationRef, *, request_id: str | None = None):
        self._client = client
        self.ref = ref
        self._request_id = request_id
        self._last_version = -1

    async def snapshot(self) -> OperationSnapshot:
        result = await self._client._port.get_operation(self.ref)
        if not isinstance(result, OperationSnapshot) or result.ref != self.ref:
            raise ValueError("receiver returned a mismatched operation")
        if self._request_id is not None and result.request_id != self._request_id:
            raise ValueError("receiver changed operation request identity")
        if result.observation_version < self._last_version:
            raise ValueError("receiver observation version regressed")
        self._last_version = result.observation_version
        return result

    async def wait(self, *, timeout: float, poll_interval: float = 0.1) -> OperationSnapshot:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise ObservationTimeout(operation_ref=self.ref)
            try:
                result = await asyncio.wait_for(self.snapshot(), timeout=left)
            except asyncio.TimeoutError as exc:
                raise ObservationTimeout(operation_ref=self.ref) from exc
            if result.lifecycle in TERMINAL_LIFECYCLES:
                return result
            await asyncio.sleep(min(poll_interval, max(0, end - time.monotonic())))

    async def events(self, *, cursor: str | None = None, poll_interval: float = 0.1) -> AsyncIterator[Event]:
        seen: set[str] = set()
        refreshed = False
        last_sequence = -1
        last_observation_version = -1
        while True:
            try:
                page, next_cursor = await self._client._port.read_events(self.ref, cursor)
            except CursorExpired:
                if refreshed:
                    raise
                await self.snapshot()
                cursor = None
                refreshed = True
                continue
            refreshed = False
            for event in page:
                if event.operation_ref != self.ref:
                    raise ValueError("out-of-scope event")
                if event.event_id not in seen:
                    if event.sequence <= last_sequence or event.observation_version < last_observation_version:
                        raise ValueError("receiver event sequence or observation regressed")
                    last_sequence = event.sequence
                    last_observation_version = event.observation_version
                    seen.add(event.event_id)
                    yield event
            if next_cursor is not None:
                cursor = next_cursor
            elif not page:
                await asyncio.sleep(poll_interval)

    async def cancel(self, request: CancelRequest):
        if request.operation_ref != self.ref:
            raise ValueError("control operation mismatch")
        return await self._client._port.cancel(request)

    async def amend_budget(self, request: BudgetAmendment):
        if request.operation_ref != self.ref:
            raise ValueError("control operation mismatch")
        return await self._client._port.amend_budget(request)

    async def answer_decision(self, request: DecisionResponse):
        if request.operation_ref != self.ref:
            raise ValueError("control operation mismatch")
        return await self._client._port.answer_decision(request)


class SyncClient:
    """Explicit synchronous facade for hosts without an active event loop."""
    def __init__(self, client: AsyncClient):
        self._client = client

    @staticmethod
    def _run(awaitable):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        awaitable.close()
        raise RuntimeError("SyncClient cannot run inside an active event loop")

    def prepare(self, *args, **kwargs):
        return self._client.prepare(*args, **kwargs)

    def submit(self, request):
        return self._run(self._client.submit(request))

    def status(self, **kwargs):
        return self._run(self._client.status(**kwargs))

    def wait(self, handle: OperationHandle, *, timeout: float):
        return self._run(handle.wait(timeout=timeout))

    def put_artifact(self, source: bytes, **kwargs):
        return self._run(self._client.put_artifact(source, **kwargs))

    def get_artifact(self, ref: ArtifactRef, **kwargs):
        return self._run(self._client.get_artifact(ref, **kwargs))

    def lookup_request(self, **kwargs):
        return self._run(self._client.lookup_request(**kwargs))

    def attach(self, ref: OperationRef):
        return self._client.attach(ref)

    def capabilities(self):
        return self._run(self._client.capabilities())

    def close(self):
        return self._run(self._client.aclose())
