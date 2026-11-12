"""In-memory scripted-test ReceiverPort with deterministic observable state."""
from __future__ import annotations

import hashlib
import uuid

from ..codecs import canonical_json, fingerprint
from ..errors import (
    ArtifactUnavailable, IdempotencyConflict, StalePrecondition,
    UnsupportedCapability,
)
from ..models import (
    ACTION_TYPES, BASE_ACTION_TYPES, ActionRequest, Admission, ArtifactRef, BudgetAmendment,
    CancelRequest, CapabilitySet, DecisionResponse, Event, OperationRef,
    OperationSnapshot, TERMINAL_LIFECYCLES,
    action_types_for_contract,
)


class ScriptedReceiverPort:
    """Contract test double; every capability explicitly says scripted-test."""

    def __init__(self):
        self._admissions: dict[tuple[str, str], tuple[bytes, Admission]] = {}
        self._operations: dict[OperationRef, OperationSnapshot] = {}
        self._events: dict[OperationRef, list[Event]] = {}
        self._artifacts: dict[tuple[str, str], bytes] = {}
        self._controls: dict[tuple[str, str], tuple[bytes, object]] = {}
        self._budget_versions: dict[OperationRef, int] = {}

    async def capabilities(self, *, contract_version="2.0"):
        if contract_version not in {"2.0", "2.1", "2.2"}:
            raise UnsupportedCapability("scripted-test contract version is unavailable")
        actions = action_types_for_contract(contract_version)
        return CapabilitySet(contract_version, frozenset(actions),
            controls=frozenset(("cancel", "amend-budget", "answer-decision")),
            backend_mode="scripted-test", independent_replay=False,
            receiver_mode="scripted-test")

    def _event(self, ref: OperationRef, event_type: str, version: int):
        sequence = len(self._events.setdefault(ref, [])) + 1
        self._events[ref].append(Event(uuid.uuid4().hex, sequence,
            f"{ref.operation_id}:{sequence}", ref, event_type, version))

    async def submit(self, request: ActionRequest):
        key = (request.workspace_id, request.request_id)
        body = canonical_json(request)
        existing = self._admissions.get(key)
        if existing is not None:
            if existing[0] != body:
                raise IdempotencyConflict("scripted-test request identity conflict", request_id=request.request_id)
            return existing[1]
        ref = OperationRef(request.workspace_id, uuid.uuid4().hex)
        admission = Admission(request.request_id, fingerprint(request), True, ref)
        self._admissions[key] = (body, admission)
        self._operations[ref] = OperationSnapshot(ref, request.request_id, 0, "queued",
            proof_id=getattr(request.action, "proof_id", None))
        self._event(ref, "queued", 0)
        return admission

    async def lookup_request(self, workspace_id, request_id):
        item = self._admissions.get((workspace_id, request_id))
        return item[1] if item else None

    async def get_operation(self, ref):
        return self._operations[ref]

    def finish(self, ref: OperationRef, *, lifecycle: str = "succeeded",
               mathematical_disposition: str | None = None):
        if lifecycle not in TERMINAL_LIFECYCLES:
            raise ValueError("scripted finish requires a terminal lifecycle")
        previous = self._operations[ref]
        if previous.lifecycle in TERMINAL_LIFECYCLES:
            raise ValueError("scripted operation is already terminal")
        current = OperationSnapshot(ref, previous.request_id,
            previous.observation_version + 1, lifecycle,
            mathematical_disposition=mathematical_disposition,
            proof_id=previous.proof_id)
        self._operations[ref] = current
        self._event(ref, lifecycle, current.observation_version)
        return current

    async def list_operations(self, *, workspace_id, proof_id=None, active_only=True, cursor=None):
        return tuple(item for ref, item in self._operations.items()
            if ref.workspace_id == workspace_id and (proof_id is None or item.proof_id == proof_id)
            and (not active_only or item.lifecycle not in TERMINAL_LIFECYCLES))

    async def read_status(self, *, workspace_id, proof_id, at_revision=None):
        raise UnsupportedCapability("scripted-test receiver has no proof state")

    async def read_coordination(self, *, workspace_id, proof_id, cursor=None, page_size=100):
        raise UnsupportedCapability("scripted-test receiver has no proof state")

    async def read_events(self, ref, cursor=None):
        events = self._events[ref]
        if cursor is None:
            start = 0
        else:
            prefix = ref.operation_id + ":"
            if not cursor.startswith(prefix) or not cursor[len(prefix):].isdecimal():
                raise ValueError("scripted event cursor is out of scope")
            start = int(cursor[len(prefix):])
        page = tuple(events[start:])
        return page, (page[-1].cursor if page else cursor)

    def _control(self, control, result_factory):
        key = (control.operation_ref.workspace_id, control.request_id)
        body = canonical_json(control)
        existing = self._controls.get(key)
        if existing is not None:
            if existing[0] != body:
                raise IdempotencyConflict("scripted-test control identity conflict", request_id=control.request_id)
            return existing[1]
        result = result_factory()
        self._controls[key] = (body, result)
        return result

    async def cancel(self, control: CancelRequest):
        def apply():
            previous = self._operations[control.operation_ref]
            if control.expected_observation_version is not None and control.expected_observation_version != previous.observation_version:
                raise StalePrecondition("scripted-test observation version differs")
            return previous if previous.lifecycle in TERMINAL_LIFECYCLES else self.finish(control.operation_ref, lifecycle="cancelled")
        return self._control(control, apply)

    async def amend_budget(self, control: BudgetAmendment):
        def apply():
            actual = self._budget_versions.get(control.operation_ref, 0)
            if actual != control.expected_budget_version:
                raise StalePrecondition("scripted-test budget version differs")
            self._budget_versions[control.operation_ref] = actual + 1
            return {"budget_version": actual + 1, "component_mode": "scripted-test"}
        return self._control(control, apply)

    async def answer_decision(self, control: DecisionResponse):
        return self._control(control, lambda: {"recorded": True, "component_mode": "scripted-test"})

    async def put_artifact(self, source, *, workspace_id, media_type, expected_hash=None):
        if type(source) is not bytes or len(source) > 16 * 1024 * 1024:
            raise ValueError("scripted artifact is not bounded bytes")
        digest = "sha256:" + hashlib.sha256(source).hexdigest()
        if expected_hash is not None and digest != expected_hash:
            raise ValueError("scripted artifact hash mismatch")
        self._artifacts[(workspace_id, digest)] = source
        return ArtifactRef(digest, len(source), media_type)

    async def get_artifact(self, ref, *, workspace_id):
        try:
            return self._artifacts[(workspace_id, ref.hash)]
        except KeyError as exc:
            raise ArtifactUnavailable("scripted artifact is not in this workspace") from exc

    async def aclose(self):
        return None
