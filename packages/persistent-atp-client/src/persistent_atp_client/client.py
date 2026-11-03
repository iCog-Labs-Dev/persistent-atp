"""Client observation and identity handling; no authoritative local ledger."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from .codecs import fingerprint
from .errors import (
    AdmissionRejected, CursorExpired, HashMismatch, ObservationTimeout, SubmissionUncertain,
    TransientUnavailable, UnsupportedCapability, UnsupportedContract,
    ReceiverProtocolError,
)
from .models import (
    ACTION_TYPES, COORDINATION_ACTION_TYPES, ActionRequest, Admission, ArtifactRef, BudgetAmendment,
    CancelRequest, DecisionResponse, Event, FormalResume, FormalSearch,
    LemmaBridge, OperationRef, OperationSnapshot, Preconditions, ProofAudit,
    ProofCreate, ProofCycle, ProofExport, QueueResearchMoves, ResearchGuidance,
    SelectedProofCycle, StatementAlign, TERMINAL_LIFECYCLES, CapabilitySet,
    CoordinationPage, ProjectDigest,
)
from .ports import ReceiverPort


def _response(value, cls):
    if not isinstance(value, cls):
        raise ReceiverProtocolError(f"receiver must return {cls.__name__}")
    return value


def _duration(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _admission(receipt, workspace_id, request_id, expected=None):
    _response(receipt, Admission)
    if receipt.request_id != request_id or (expected is not None and receipt.fingerprint != expected):
        raise ReceiverProtocolError("receiver admission identity mismatch", request_id=request_id)
    for ref in (receipt.operation_ref, receipt.coalesced_to):
        if ref is not None and ref.workspace_id != workspace_id:
            raise ReceiverProtocolError("receiver admission workspace mismatch", request_id=request_id)
    return receipt


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
        version = "2.1" if getattr(action, "kind", None) in COORDINATION_ACTION_TYPES else "2.0"
        return ActionRequest(request_id=request_id or uuid.uuid4().hex,
                             workspace_id=workspace_id, action=action,
                             preconditions=preconditions or Preconditions(), deadline=deadline,
                             contract_version=version)

    async def capabilities(self, *, contract_version: str = "2.0"):
        caps = await (self._port.capabilities() if contract_version == "2.0"
                      else self._port.capabilities(contract_version=contract_version))
        _response(caps, CapabilitySet)
        if caps.contract_version != contract_version:
            raise UnsupportedContract(f"receiver does not support contract {contract_version}")
        return caps

    async def submit(self, request: ActionRequest) -> "OperationHandle":
        if self._closed:
            raise RuntimeError("client is closed")
        if not isinstance(request, ActionRequest):
            raise TypeError("submit requires a prepared ActionRequest")
        caps = await self.capabilities(contract_version=request.contract_version)
        if request.action.kind not in caps.actions:
            raise UnsupportedCapability(f"{request.action.kind} is unavailable", request_id=request.request_id)
        expected = fingerprint(request)
        try:
            receipt = await self._port.submit(request)
            _admission(receipt, request.workspace_id, request.request_id, expected)
        except (TimeoutError, ConnectionError, TransientUnavailable, ReceiverProtocolError, json.JSONDecodeError) as exc:
            try:
                receipt = await self._port.lookup_request(request.workspace_id, request.request_id)
                if receipt is not None:
                    _admission(receipt, request.workspace_id, request.request_id, expected)
            except (TimeoutError, ConnectionError, TransientUnavailable, ReceiverProtocolError, json.JSONDecodeError) as lookup_exc:
                raise SubmissionUncertain("submission and lookup unavailable", request=request) from lookup_exc
            if receipt is None:
                raise SubmissionUncertain("authoritative lookup found no admission; retry the same prepared request", request=request) from exc
        if not receipt.accepted:
            raise AdmissionRejected(receipt)
        return OperationHandle(self, receipt.operation_ref, request_id=request.request_id)

    async def submit_action(self, action, *, workspace_id: str, preconditions: Preconditions | None = None,
                            request_id: str | None = None, deadline: datetime | None = None) -> "OperationHandle":
        request = self.prepare(action, workspace_id=workspace_id, preconditions=preconditions,
                               request_id=request_id, deadline=deadline)
        return await self.submit(request)

    async def proof_create(self, spec_artifact: ArtifactRef, *, workspace_id: str, **request_options):
        return await self.submit_action(ProofCreate(spec_artifact), workspace_id=workspace_id, **request_options)

    async def proof_cycle(self, proof_id: str, *, workspace_id: str,
                          category: str | None = None, **request_options):
        action = ProofCycle(proof_id) if category is None else SelectedProofCycle(proof_id, category)
        return await self.submit_action(action, workspace_id=workspace_id, **request_options)

    async def queue_research_moves(self, proof_id: str, intent_key: str,
                                   moves: tuple[ResearchGuidance, ...], *, workspace_id: str,
                                   preconditions: Preconditions, **request_options):
        if preconditions.expected_revision is None:
            raise ValueError("guidance requires expected_revision")
        return await self.submit_action(QueueResearchMoves(proof_id, intent_key, moves),
                                        workspace_id=workspace_id, preconditions=preconditions,
                                        **request_options)

    async def coordination_page(self, proof_id: str, *, workspace_id: str,
                                cursor: str | None = None, page_size: int = 100):
        caps = await self.capabilities(contract_version="2.1")
        if "coordination-read" not in caps.controls:
            raise UnsupportedCapability("coordination-read is unavailable")
        page = _response(await self._port.read_coordination(workspace_id=workspace_id, proof_id=proof_id,
                                                   cursor=cursor, page_size=page_size), CoordinationPage)
        if page.workspace_id != workspace_id or page.proof_id != proof_id:
            raise ReceiverProtocolError("coordination page belongs to another workspace or proof")
        return page

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
        receipt = await self._port.lookup_request(workspace_id, request_id)
        return _admission(receipt, workspace_id, request_id) if receipt is not None else None

    def attach(self, operation_ref: OperationRef) -> "OperationHandle":
        if not isinstance(operation_ref, OperationRef):
            raise TypeError("attach requires OperationRef")
        return OperationHandle(self, operation_ref)

    async def status(self, *, workspace_id: str, proof_id: str, at_revision: int | None = None) -> ProjectDigest:
        result = _response(await self._port.read_status(workspace_id=workspace_id,
            proof_id=proof_id, at_revision=at_revision), ProjectDigest)
        if result.workspace_id != workspace_id or result.proof_id != proof_id:
            raise ReceiverProtocolError("status belongs to another workspace or proof")
        if at_revision is not None and result.revision != at_revision:
            raise ReceiverProtocolError("status revision differs from the requested revision")
        return result

    async def list_operations(self, *, workspace_id: str, proof_id: str | None = None,
                              active_only: bool = True, cursor: str | None = None):
        results = await self._port.list_operations(workspace_id=workspace_id, proof_id=proof_id,
                                                active_only=active_only, cursor=cursor)
        if not isinstance(results, tuple):
            raise ReceiverProtocolError("operation list must be a tuple")
        seen = set()
        for result in results:
            _response(result, OperationSnapshot)
            if (result.ref.workspace_id != workspace_id
                    or (proof_id is not None and result.proof_id != proof_id)):
                raise ReceiverProtocolError("operation list contains an out-of-scope operation")
            if result.ref in seen:
                raise ReceiverProtocolError("operation list contains duplicate references")
            seen.add(result.ref)
        return results

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
            raise ReceiverProtocolError("receiver returned a mismatched operation")
        if self._request_id is not None and result.request_id != self._request_id:
            raise ReceiverProtocolError("receiver changed operation request identity")
        if result.observation_version < self._last_version:
            raise ReceiverProtocolError("receiver observation version regressed")
        self._last_version = result.observation_version
        return result

    async def wait(self, *, timeout: float, poll_interval: float = 0.1) -> OperationSnapshot:
        _duration(timeout, "timeout")
        _duration(poll_interval, "poll_interval")
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
        _duration(poll_interval, "poll_interval")
        seen: set[str] = set()
        refreshed = False
        last_sequence = -1
        last_observation_version = -1
        while True:
            try:
                response = await self._client._port.read_events(self.ref, cursor)
                if not isinstance(response, tuple) or len(response) != 2:
                    raise ReceiverProtocolError("invalid event page")
                page, next_cursor = response
                if not isinstance(page, tuple) or not all(isinstance(event, Event) for event in page):
                    raise ReceiverProtocolError("invalid event records")
                if next_cursor is not None and (not isinstance(next_cursor, str)
                        or not 1 <= len(next_cursor) <= 128 or any(ord(c) < 32 for c in next_cursor)):
                    raise ReceiverProtocolError("invalid event cursor")
            except CursorExpired:
                if refreshed:
                    raise
                await self.snapshot()
                cursor = None
                refreshed = True
                continue
            refreshed = False
            emitted = False
            for event in page:
                if event.operation_ref != self.ref:
                    raise ReceiverProtocolError("out-of-scope event")
                if event.event_id not in seen:
                    if event.sequence <= last_sequence or event.observation_version < last_observation_version:
                        raise ReceiverProtocolError("receiver event sequence or observation regressed")
                    last_sequence = event.sequence
                    last_observation_version = event.observation_version
                    seen.add(event.event_id)
                    emitted = True
                    yield event
            if next_cursor is not None:
                cursor = next_cursor
            elif emitted:
                cursor = page[-1].cursor
            if not emitted:
                if (await self.snapshot()).lifecycle in TERMINAL_LIFECYCLES:
                    return
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

    def capabilities(self, *, contract_version: str = "2.0"):
        return self._run(self._client.capabilities(contract_version=contract_version))

    def coordination_page(self, proof_id: str, **kwargs):
        return self._run(self._client.coordination_page(proof_id, **kwargs))

    def close(self):
        return self._run(self._client.aclose())
