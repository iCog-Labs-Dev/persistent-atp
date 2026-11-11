"""Contract-v2 reference receiver over the existing trusted Omega service.

The durable ledger is operational metadata, never a source of mathematical
truth. This receiver is explicitly single-workspace/reference-mode and does
not advertise formal replay or production authorization.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from persistent_atp_client.codecs import canonical_json, fingerprint, request_from_json
from persistent_atp_client.errors import (
    ArtifactUnavailable, Forbidden, IdempotencyConflict, StalePrecondition,
    UnsupportedCapability,
)
from persistent_atp_client.models import (
    ACTION_TYPES, BASE_ACTION_TYPES, action_types_for_contract, ActionRequest, Admission, ArtifactRef, BudgetAmendment,
    CancelRequest, CapabilitySet, DecisionResponse, Event, FormalResume,
    FormalSearch, LemmaBridge, OperationRef, OperationSnapshot, ProofAudit,
    ProofCreate, ProofCycle, ProofExport, ProofStatus, ProjectDigest,
    StatementAlign, TERMINAL_LIFECYCLES, SelectedProofCycle, QueueResearchMoves,
    CoordinationPage, CoordinationItem,
)
from mathproof.scheduler import GlobalScheduler

from .service import OperationService
from .execution import ExecutionPending


class ReferenceReceiver:
    """Service-owned queue and request ledger for the labeled reference tier."""

    def __init__(self, service: OperationService, *, principal: str = "trusted-reference"):
        if not principal:
            raise ValueError("trusted principal is required")
        if getattr(service, "production", False):
            with service.lock:
                if getattr(service, "_receiver_attached", False):
                    raise ValueError("production service already has an active receiver")
                service._receiver_attached = True
        self.service = service
        self.principal = principal
        self.workspace_id = service.workspace
        self.component_mode = getattr(service, "component_mode", "reference-scripted")
        self._closed = False
        self._scheduled = set()
        self._retry_timers = {}
        self._retry_counts = {}
        self._lock = threading.RLock()
        # JournalStore has one SQLite connection. Serialize receiver reads with
        # worker commits so an HTTP poll cannot inspect a transaction in flight.
        self._service_lock = getattr(service, "lock", threading.RLock())
        self._db = sqlite3.connect(Path(service.root) / "receiver-v2.sqlite3", isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                principal TEXT NOT NULL, workspace_id TEXT NOT NULL,
                request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                body BLOB NOT NULL, operation_id TEXT NOT NULL UNIQUE,
                lifecycle TEXT NOT NULL, observation_version INTEGER NOT NULL,
                result TEXT, PRIMARY KEY(principal, workspace_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS artifact_grants (
                principal TEXT NOT NULL, workspace_id TEXT NOT NULL,
                hash TEXT NOT NULL, PRIMARY KEY(principal, workspace_id, hash)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL,
                event_type TEXT NOT NULL, observation_version INTEGER NOT NULL
            );
        """)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atp-reference-receiver")
        self._pending = set()
        with self._lock:
            rows = self._db.execute("SELECT operation_id FROM requests WHERE principal=? AND workspace_id=? AND lifecycle IN ('queued','running')",
                                    (self.principal, self.workspace_id)).fetchall()
        for row in rows:
            self._schedule(row["operation_id"])

    def _scope(self, workspace_id: str):
        if workspace_id != self.workspace_id:
            raise Forbidden("workspace is not available to this receiver")

    def _schedule(self, operation_id: str):
        with self._lock:
            if self._closed or operation_id in self._scheduled:
                return
            self._retry_timers.pop(operation_id, None)
            self._scheduled.add(operation_id)
            future = self._executor.submit(self._execute, operation_id)
            self._pending.add(future)
        def finished(done):
            try:
                retry = done.result() is True
            except Exception:
                retry = False  # Durable running row remains recoverable on restart.
            with self._lock:
                self._pending.discard(done)
                self._scheduled.discard(operation_id)
                if retry and not self._closed:
                    count = self._retry_counts.get(operation_id, 0)
                    self._retry_counts[operation_id] = count + 1
                    timer = threading.Timer(min(10, 0.5 * 2 ** min(count, 5)), self._schedule, (operation_id,))
                    timer.daemon = True
                    self._retry_timers[operation_id] = timer
                    timer.start()
                elif not retry:
                    self._retry_counts.pop(operation_id, None)
        future.add_done_callback(finished)

    def _event(self, operation_id: str, event_type: str, version: int):
        self._db.execute("INSERT INTO events(event_id,operation_id,event_type,observation_version) VALUES (?,?,?,?)",
                         (uuid.uuid4().hex, operation_id, event_type, version))

    async def capabilities(self, contract_version: str = "2.0") -> CapabilitySet:
        if contract_version not in {"2.0", "2.1"}:
            raise UnsupportedCapability("unsupported contract version")
        actions = action_types_for_contract(contract_version)
        if getattr(self.service, "production", False):
            actions = {name: kind for name, kind in actions.items() if name in self.service.ACTIONS}
        return CapabilitySet(contract_version, frozenset(actions),
                             controls=frozenset({"coordination-read"}) if contract_version == "2.1" else frozenset(),
                             backend_mode="real" if getattr(self.service, "production", False) else "unavailable",
                             independent_replay=False, receiver_mode=self.component_mode)

    async def submit(self, request: ActionRequest) -> Admission:
        if not isinstance(request, ActionRequest):
            raise TypeError("receiver requires ActionRequest")
        self._scope(request.workspace_id)
        capabilities = await self.capabilities(request.contract_version)
        if request.action.kind not in capabilities.actions:
            raise UnsupportedCapability("action is unavailable under this contract version")
        if getattr(self.service, "production", False):
            self.service.validate_action(request.action)
        if isinstance(request.action, QueueResearchMoves) and request.preconditions.expected_revision is None:
            raise ValueError("guidance requires an expected proof revision")
        digest = fingerprint(request)
        body = canonical_json(request)
        with self._lock:
            existing = self._db.execute("SELECT * FROM requests WHERE principal=? AND workspace_id=? AND request_id=?",
                                        (self.principal, self.workspace_id, request.request_id)).fetchone()
        if existing is not None:
            if existing["fingerprint"] != digest or existing["body"] != body:
                raise IdempotencyConflict("request ID reused with a different body", request_id=request.request_id)
            return Admission(request.request_id, digest, True,
                             OperationRef(self.workspace_id, existing["operation_id"]))
        if request.deadline is not None and request.deadline <= datetime.now(timezone.utc):
            raise StalePrecondition("request deadline has elapsed", request_id=request.request_id)
        pre = request.preconditions
        if any((pre.environment_hash, pre.declaration_hash, pre.checkpoint_hash)):
            raise UnsupportedCapability("reference receiver cannot validate environment, declaration or checkpoint pins")
        proof_id = getattr(request.action, "proof_id", None)
        if isinstance(request.action, ProofCreate):
            try:
                spec = json.loads(self.service.artifacts.get(request.action.spec_artifact.hash))
                proof_id = spec["proof_id"]
            except (ValueError, KeyError, TypeError):
                proof_id = None
        if proof_id is not None:
            with self._service_lock:
                revision = self.service.store.head(proof_id)[0]
                if pre.expected_revision is not None and revision != pre.expected_revision:
                    raise StalePrecondition("proof revision differs from precondition", request_id=request.request_id)
                if pre.expected_theorem_hash is not None:
                    target = self.service.view.node(f"{proof_id}/proof")
                    target_hash = target.fields.get("target_artifact") if target else None
                    if pre.expected_theorem_hash != target_hash:
                        raise StalePrecondition("target identity differs from precondition", request_id=request.request_id)
        elif pre.expected_revision is not None or pre.expected_theorem_hash is not None:
            raise StalePrecondition("cannot check precondition against an unknown proof", request_id=request.request_id)
        if isinstance(request.action, ProofStatus) and request.action.at_revision is not None:
            raise UnsupportedCapability("historical proof-status action is unavailable")
        # Verify the exact uploaded bytes before reserving execution responsibility.
        refs = []
        if isinstance(request.action, ProofCreate):
            refs.append(request.action.spec_artifact)
        elif isinstance(request.action, StatementAlign):
            refs.append(request.action.lean_source_artifact)
        elif isinstance(request.action, FormalResume) and request.action.checkpoint_artifact is not None:
            refs.append(request.action.checkpoint_artifact)
        for ref in refs:
            with self._lock:
                grant = self._db.execute("SELECT 1 FROM artifact_grants WHERE principal=? AND workspace_id=? AND hash=?",
                                         (self.principal, self.workspace_id, ref.hash)).fetchone()
            if not grant or not self.service.artifacts.exists(ref.hash):
                raise ArtifactUnavailable("referenced artifact is unavailable")
            exact = self.service.artifacts.get(ref.hash)
            if ref.size_bytes is not None and ref.size_bytes != len(exact):
                raise ArtifactUnavailable("referenced artifact size differs")
        created = False
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM requests WHERE principal=? AND workspace_id=? AND request_id=?",
                                       (self.principal, self.workspace_id, request.request_id)).fetchone()
                if row is None:
                    operation_id = uuid.uuid4().hex
                    self._db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?, ?,?)",
                                     (self.principal, self.workspace_id, request.request_id, digest, body,
                                      operation_id, "queued", 0, None))
                    self._event(operation_id, "queued", 0)
                    created = True
                else:
                    if row["fingerprint"] != digest or row["body"] != body:
                        raise IdempotencyConflict("request ID reused with a different body", request_id=request.request_id)
                    operation_id = row["operation_id"]
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        if created:
            self._schedule(operation_id)
        return Admission(request.request_id, digest, True, OperationRef(self.workspace_id, operation_id))

    def _legacy_request(self, request: ActionRequest) -> dict:
        action = request.action
        if isinstance(action, ProofCreate):
            raw = self.service.artifacts.get(action.spec_artifact.hash).decode("utf-8")
            args = {"spec": raw}
        elif isinstance(action, ProofCycle): args = {"proof": action.proof_id}
        elif isinstance(action, ProofStatus): args = {"proof": action.proof_id}
        elif isinstance(action, ProofAudit): args = {"proof": action.proof_id}
        elif isinstance(action, ProofExport): args = {"proof": action.proof_id, "destination": action.destination_alias}
        elif isinstance(action, FormalSearch):
            args = {"proof": action.proof_id, "claim": action.claim_id,
                    "budget": json.dumps({"max_steps": action.budget.max_steps, "wall_clock_ms": action.budget.wall_clock_ms})}
        elif isinstance(action, FormalResume):
            args = {"proof": action.proof_id, "run": action.run_id,
                    "budget": json.dumps({"max_steps": action.budget.max_steps, "wall_clock_ms": action.budget.wall_clock_ms})}
        elif isinstance(action, StatementAlign):
            args = {"proof": action.proof_id, "claim": action.claim_id,
                    "lean_source": action.lean_source_artifact.hash}
        elif isinstance(action, LemmaBridge): args = {"proof": action.proof_id, "obstruction": action.obstruction_id}
        else: raise TypeError("unknown action")
        return {"schema_version": 1, "request_id": request.request_id,
                "workspace": self.workspace_id, "operation": action.kind, "arguments": args}

    def _execute(self, operation_id: str):
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE operation_id=? AND principal=? AND workspace_id=?",
                                   (operation_id, self.principal, self.workspace_id)).fetchone()
            if row is None or row["lifecycle"] in TERMINAL_LIFECYCLES:
                return
            version = row["observation_version"] + 1
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("UPDATE requests SET lifecycle='running', observation_version=? WHERE operation_id=?", (version, operation_id))
                self._event(operation_id, "running", version)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        try:
            request = request_from_json(row["body"])
            if request.workspace_id != self.workspace_id or request.request_id != row["request_id"] or fingerprint(request) != row["fingerprint"]:
                raise ValueError("persisted admission identity is inconsistent")
            with (nullcontext() if getattr(self.service, "production", False) else self._service_lock):
                if getattr(self.service, "production", False):
                    outcome = self.service.execute_request(request, operation_id)
                elif isinstance(request.action, SelectedProofCycle):
                    action = request.action
                    outcome = self.service._cycle(action.proof_id, operation_id,
                        category=action.category, record_progress=False,
                        expected_revision=request.preconditions.expected_revision)
                elif isinstance(request.action, QueueResearchMoves):
                    outcome = self.service.queue_research_moves(request.action,
                        expected_revision=request.preconditions.expected_revision)
                else:
                    outcome = self.service.submit(self._legacy_request(request))
            lifecycle = "failed" if outcome.get("phase") == "failed" or outcome.get("transport_status") == "error" else "succeeded"
        except ExecutionPending as exc:
            lifecycle = "running"
            outcome = {"phase": "reconciling", "error_code": "EXECUTION_UNCERTAIN", "summary": str(exc)}
        except Exception as exc:
            lifecycle = "failed"
            outcome = {"error_code": type(exc).__name__, "summary": str(exc)[:400]}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if outcome is not None:
                    pointers = list(outcome.get("artifact_pointers", ()))
                    pointers += [outcome[key] for key in ("packet_ref", "request_ref", "result_ref", "report_artifact", "export_artifact")
                                 if isinstance(outcome.get(key), str)]
                    for pointer in pointers:
                        try:
                            ArtifactRef(pointer)
                        except (ValueError, TypeError):
                            continue
                        if self.service.artifacts.exists(pointer):
                            self._db.execute("INSERT OR IGNORE INTO artifact_grants VALUES (?,?,?)",
                                (self.principal, self.workspace_id, pointer))
                self._db.execute("UPDATE requests SET lifecycle=?, observation_version=?, result=? WHERE operation_id=?",
                                 (lifecycle, version + 1, json.dumps(outcome, sort_keys=True), operation_id))
                self._event(operation_id, lifecycle, version + 1)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return lifecycle == "running"

    async def lookup_request(self, workspace_id: str, request_id: str) -> Admission | None:
        self._scope(workspace_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE principal=? AND workspace_id=? AND request_id=?",
                                   (self.principal, workspace_id, request_id)).fetchone()
        return (Admission(request_id, row["fingerprint"], True, OperationRef(workspace_id, row["operation_id"])) if row else None)

    async def get_operation(self, ref: OperationRef) -> OperationSnapshot:
        self._scope(ref.workspace_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE principal=? AND workspace_id=? AND operation_id=?",
                                   (self.principal, ref.workspace_id, ref.operation_id)).fetchone()
        if row is None:
            raise KeyError("operation not found")
        request = request_from_json(row["body"])
        outcome = json.loads(row["result"]) if row["result"] else None
        proof_id = getattr(request.action, "proof_id", None) or (outcome or {}).get("proof_id")
        with self._service_lock:
            head = self.service.store.head(proof_id) if proof_id else (None, None)
            try:
                watermark = self.service.view.snapshot(proof_id)[0] if proof_id else None
            except Exception:
                if not getattr(self.service, "production", False):
                    raise
                watermark = None  # Durable operation remains observable while MORK is unavailable.
        return OperationSnapshot(ref, row["request_id"], row["observation_version"], row["lifecycle"],
            execution_phase=(outcome or {}).get("phase"),
            mathematical_disposition=(outcome or {}).get("cycle_disposition"),
            proof_id=proof_id, project_revision=head[0], journal_hash=head[1],
            projection_watermark=watermark, result=outcome)

    async def list_operations(self, *, workspace_id: str, proof_id: str | None = None,
                              active_only: bool = True, cursor: str | None = None):
        self._scope(workspace_id)
        with self._lock:
            rows = self._db.execute("SELECT operation_id FROM requests WHERE principal=? AND workspace_id=? ORDER BY rowid",
                                    (self.principal, workspace_id)).fetchall()
        snapshots = [await self.get_operation(OperationRef(workspace_id, row["operation_id"])) for row in rows]
        return tuple(item for item in snapshots if (proof_id is None or item.proof_id == proof_id)
                     and (not active_only or item.lifecycle not in TERMINAL_LIFECYCLES))

    async def read_status(self, *, workspace_id: str, proof_id: str, at_revision: int | None = None) -> ProjectDigest:
        self._scope(workspace_id)
        # The reference status read is bounded and journal-backed; production
        # receivers should use their own async storage or bounded executor.
        with self._service_lock:
            if self.service.store.head(proof_id)[0] == 0:
                raise KeyError("proof not found")
            raw = self.service._status(proof_id)
        if at_revision is not None and raw["revision"] != at_revision:
            raise UnsupportedCapability("historical revision read is unavailable")
        target_ref = raw["artifact_pointers"][0]
        target = self.service.artifacts.get(target_ref)
        return ProjectDigest(workspace_id, proof_id, raw["revision"], raw["event_hash"],
                             raw["revision"] - raw["projection_lag"],
                             ArtifactRef(target_ref, len(target), "text/plain; charset=utf-8"),
                             target.decode("utf-8"), raw["claim_status"], bool(raw["formal_verified"]), raw["summary"],
                             self.component_mode)

    async def read_coordination(self, *, workspace_id: str, proof_id: str,
                                cursor: str | None = None, page_size: int = 100) -> CoordinationPage:
        with self._service_lock:
            return self._read_coordination(workspace_id=workspace_id, proof_id=proof_id,
                                           cursor=cursor, page_size=page_size)

    def _read_coordination(self, *, workspace_id, proof_id, cursor=None, page_size=100):
        """Page through one journal-pinned proof graph and its full frontier."""
        self._scope(workspace_id)
        with self._service_lock:
            return self._read_coordination_locked(workspace_id, proof_id, cursor, page_size)

    def _read_coordination_locked(self, workspace_id: str, proof_id: str,
                                  cursor: str | None, page_size: int) -> CoordinationPage:
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValueError("page_size must be within 1..200")
        self.service.catch_up(proof_id)
        head_revision, head_hash = self.service.store.head(proof_id)
        if head_revision == 0:
            raise KeyError("proof not found")
        revision, snapshot = self.service.view.snapshot(proof_id)
        if revision != head_revision:
            raise StalePrecondition("projection is behind the journal")
        if cursor is None:
            offset = 0
        else:
            prefix = f"{proof_id}:{revision}:"
            if not cursor.startswith(prefix) or not cursor[len(prefix):].isdecimal():
                raise StalePrecondition("coordination cursor is stale or belongs to another proof")
            offset = int(cursor[len(prefix):])
        nodes = snapshot.list_nodes(proof_id)
        edges = snapshot.list_edges(proof_id)
        if len(nodes) + len(edges) > 10_000:
            raise UnsupportedCapability("reference coordination read exceeds its 10000-record bound")
        items = [CoordinationItem("node", node.node_id, node.label, dict(node.fields))
                 for node in nodes]
        items.extend(CoordinationItem("edge", edge.edge_id, edge.rel_type,
            {"src_id": edge.src_id, "dst_id": edge.dst_id, **dict(edge.fields)})
            for edge in edges)
        frontier = GlobalScheduler(snapshot, self.service.store).build_frontier(proof_id)
        if len(items) + len(frontier) > 10_000:
            raise UnsupportedCapability("reference coordination read exceeds its 10000-record bound")
        items.extend(CoordinationItem("frontier", candidate.move_id, candidate.category,
            {"detail": candidate.detail, "features": dict(candidate.features),
             "conflict_ids": list(candidate.conflict_ids)}) for candidate in frontier)
        if self.service.store.head(proof_id) != (head_revision, head_hash):
            raise StalePrecondition("proof changed during coordination read")
        if offset > len(items):
            raise ValueError("coordination cursor exceeds item count")
        selected = tuple(items[offset:offset + page_size])
        next_offset = offset + len(selected)
        next_cursor = f"{proof_id}:{revision}:{next_offset}" if next_offset < len(items) else None
        return CoordinationPage(workspace_id, proof_id, revision, head_hash, revision,
            selected, len(items), next_cursor, self.component_mode)

    async def read_events(self, ref: OperationRef, cursor: str | None = None):
        self._scope(ref.workspace_id)
        await self.get_operation(ref)
        if cursor is None:
            after = 0
        else:
            prefix = ref.operation_id + ":"
            if not cursor.startswith(prefix) or not cursor[len(prefix):].isdecimal():
                raise ValueError("event cursor does not belong to this operation")
            after = int(cursor[len(prefix):])
        with self._lock:
            rows = self._db.execute("SELECT * FROM events WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT 100",
                                    (ref.operation_id, after)).fetchall()
        items = tuple(Event(row["event_id"], row["sequence"], f"{ref.operation_id}:{row['sequence']}", ref,
                            row["event_type"], row["observation_version"]) for row in rows)
        return items, (items[-1].cursor if items else cursor)

    async def put_artifact(self, source: bytes, *, workspace_id: str, media_type: str, expected_hash: str | None = None) -> ArtifactRef:
        self._scope(workspace_id)
        if type(source) is not bytes or len(source) > 16 * 1024 * 1024:
            raise ValueError("artifact is not bounded bytes")
        pointer = self.service.artifacts.put(source)
        if expected_hash is not None and pointer != expected_hash:
            raise ValueError("artifact hash mismatch")
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO artifact_grants VALUES (?,?,?)", (self.principal, workspace_id, pointer))
        return ArtifactRef(pointer, len(source), media_type)

    async def get_artifact(self, ref: ArtifactRef, *, workspace_id: str) -> bytes:
        self._scope(workspace_id)
        with self._lock:
            grant = self._db.execute("SELECT 1 FROM artifact_grants WHERE principal=? AND workspace_id=? AND hash=?",
                                     (self.principal, workspace_id, ref.hash)).fetchone()
        if not grant:
            raise ArtifactUnavailable("artifact is not available in this workspace")
        return self.service.artifacts.get(ref.hash)

    async def cancel(self, control: CancelRequest):
        raise UnsupportedCapability("reference executor cancellation is unavailable")

    async def amend_budget(self, control: BudgetAmendment):
        raise UnsupportedCapability("reference budget amendment is unavailable")

    async def answer_decision(self, control: DecisionResponse):
        raise UnsupportedCapability("reference decision workflow is unavailable")

    async def aclose(self):
        # Service lifecycle, not caller-client lifecycle.
        return None

    def close(self):
        with self._lock:
            self._closed = True
            for timer in self._retry_timers.values():
                timer.cancel()
        self._executor.shutdown(wait=True)
        with self._lock:
            self._db.close()
        if getattr(self.service, "production", False):
            with self._service_lock:
                self.service._receiver_attached = False
