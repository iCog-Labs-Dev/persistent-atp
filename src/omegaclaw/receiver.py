"""Contract-v2 receiver over the existing trusted Omega service.

The durable ledger is operational metadata, never a source of mathematical
truth. Storage and formal execution capabilities follow the explicitly
configured profile. Authorization remains scoped to one trusted workspace.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from persistent_atp_client.codecs import canonical_json, fingerprint, request_from_json, to_dict
from persistent_atp_client.errors import (
    ArtifactUnavailable, HashMismatch, Forbidden, IdempotencyConflict, StalePrecondition,
    UnsupportedCapability,
    ReviewRequired, QuotaExceeded, TransientUnavailable,
)
from persistent_atp_client.models import (
    action_types_for_contract, ActionRequest, Admission, ArtifactRef, BudgetAmendment,
    CancelRequest, SearchReconciliationRequest, SearchReconciliationJob, CapabilitySet, DecisionResponse, Event, FormalResume,
    FormalSearch, HypothesisFormalize, HypothesisRevise, ParentRefine, LemmaBridge, OperationRef, OperationSnapshot, ProofAudit,
    ProofCreate, ProofCycle, ProofExport, ProofStatus, ProjectDigest,
    StatementAlign, TERMINAL_LIFECYCLES, SelectedProofCycle, QueueResearchMoves,
    CoordinationPage, CoordinationItem,
)
from mathproof.scheduler import GlobalScheduler
from artifacts.errors import ArtifactIntegrityError

from .service import OperationService

MAX_RECONCILIATION_JOBS = 32
READINESS_TTL_SECONDS = 5


class ReferenceReceiver:
    """Durable request queue with explicitly selected storage/execution tiers."""

    def __init__(self, service: OperationService, *, principal: str = "trusted-reference"):
        if not principal:
            raise ValueError("trusted principal is required")
        self.service = service
        self.principal = principal
        self.workspace_id = service.workspace
        self._lock = threading.RLock()
        self._reconcile_locks = {}
        # JournalStore has one SQLite connection. Serialize receiver reads with
        # worker commits so an HTTP poll cannot inspect a transaction in flight.
        self._service_lock = service.lock
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
            CREATE TABLE IF NOT EXISTS controls (
                principal TEXT NOT NULL, workspace_id TEXT NOT NULL,
                request_id TEXT NOT NULL, body BLOB NOT NULL,
                PRIMARY KEY(principal, workspace_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS search_reconciliations (
                operation_id TEXT PRIMARY KEY REFERENCES requests(operation_id),
                request_fingerprint TEXT NOT NULL, receipt_artifact TEXT NOT NULL,
                summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_jobs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                principal TEXT NOT NULL, workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, body BLOB NOT NULL,
                state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, summary TEXT,
                deadline REAL NOT NULL,
                UNIQUE(principal, workspace_id, request_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_reconciliation_job
                ON reconciliation_jobs(operation_id) WHERE state IN ('queued','running');
        """)
        columns = {row['name'] for row in self._db.execute('PRAGMA table_info(reconciliation_jobs)')}
        if 'deadline' not in columns:
            self._db.execute('ALTER TABLE reconciliation_jobs ADD COLUMN deadline REAL NOT NULL DEFAULT 0')
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atp-reference-receiver")
        self._reconciliation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="atp-reconciliation")
        self._readiness_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='atp-readiness')
        self._readiness_clock = time.monotonic
        self._readiness_record = None
        self._readiness_pending = None
        self._closing = False
        self._reconciliation_pending = set()
        self._pending = set()
        with self._lock:
            rows = self._db.execute("SELECT operation_id FROM requests WHERE lifecycle IN ('queued','running','cancelling')").fetchall()
        for row in rows:
            self._schedule(row["operation_id"])
        with self._lock:
            # A crash after the receipt transaction needs no more provider I/O,
            # even if the observation deadline or restart allowance elapsed.
            self._db.execute("UPDATE reconciliation_jobs SET state='succeeded', version=version+1, error_code=NULL, summary=NULL "
                "WHERE principal=? AND workspace_id=? AND state IN ('queued','running') AND operation_id IN "
                "(SELECT operation_id FROM search_reconciliations)", (self.principal, self.workspace_id))
            self._db.execute("UPDATE reconciliation_jobs SET state='blocked', version=version+1, error_code='REVIEW_REQUIRED', "
                "summary='Reconciliation restart allowance exhausted; review a new control.' "
                "WHERE principal=? AND workspace_id=? AND state IN ('queued','running') AND attempts>=3",
                (self.principal, self.workspace_id))
            self._db.execute("UPDATE reconciliation_jobs SET state='queued', version=version+1 "
                "WHERE principal=? AND workspace_id=? AND state='running'", (self.principal, self.workspace_id))
            jobs = self._db.execute("SELECT request_id FROM reconciliation_jobs WHERE principal=? AND workspace_id=? AND state='queued'",
                (self.principal, self.workspace_id)).fetchall()
        for job in jobs:
            self._schedule_reconciliation(job['request_id'])
        self._formal_readiness()

    def _artifact(self, address):
        try:
            return self.service.artifacts.get(address)
        except ArtifactIntegrityError as exc:
            raise HashMismatch("stored artifact bytes do not match their content address") from exc

    def _scope(self, workspace_id: str):
        if workspace_id != self.workspace_id:
            raise Forbidden("workspace is not available to this receiver")

    def _schedule(self, operation_id: str):
        future = self._executor.submit(self._execute, operation_id)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    def _event(self, operation_id: str, event_type: str, version: int):
        self._db.execute("INSERT INTO events(event_id,operation_id,event_type,observation_version) VALUES (?,?,?,?)",
                         (uuid.uuid4().hex, operation_id, event_type, version))

    def _readiness_key(self):
        provider = self.service.formal
        return provider, getattr(provider, 'config', None)

    def _formal_readiness(self):
        """Return True/False for fresh observations, None while unknown/stale.

        Only a worker may contact the provider. Cache data is operational,
        expires conservatively and is discarded on provider/config changes.
        """
        key = self._readiness_key()
        if key[0] is None:
            return False
        with self._lock:
            record = self._readiness_record
            if (record is not None and record['key'] == key
                    and self._readiness_clock() - record['checked_at'] < READINESS_TTL_SECONDS):
                return record['available']
            if self._readiness_pending is None and not self._closing:
                self._readiness_pending = key
                self._readiness_executor.submit(self._check_readiness, key)
            return None

    def _check_readiness(self, key):
        try:
            try:
                available = key[0].ready() is True
            except Exception:
                available = False
            with self._lock:
                if not self._closing and self._readiness_key() == key:
                    self._readiness_record = {'key': key, 'available': available,
                        'checked_at': self._readiness_clock()}
        finally:
            with self._lock:
                self._readiness_pending = None
                changed = not self._closing and self._readiness_key() != key
            if changed:
                self._formal_readiness()

    async def capabilities(self, contract_version: str = "2.0") -> CapabilitySet:
        if contract_version not in {"2.0", "2.1", "2.2", "2.3"}:
            raise UnsupportedCapability("unsupported contract version")
        actions = action_types_for_contract(contract_version)
        readiness = self._formal_readiness()
        available = readiness is True
        # Pending readiness keeps configured actions discoverable so admission
        # can defer with a retryable error rather than reject the user's intent.
        supports_formal = self.service.formal is not None and readiness is not False
        if self.service.profile == "production":
            actions = {kind: value for kind, value in actions.items() if kind in {
                "proof-create", "proof-status", "proof-audit", "proof-export",
                *({'formal-search', 'proof-cycle', 'proof-cycle-selected'} if supports_formal else set()),
                *({'lemma-bridge'} if self.service.research is not None else set()),
                *({'hypothesis-formalize', 'hypothesis-revise'} if self.service.intake is not None else set()),
                *({'parent-refine'} if self.service.refinement is not None else set())}}
        elif self.service.formal is not None:
            actions = {kind: value for kind, value in actions.items() if kind != 'formal-resume'
                       and (kind != 'formal-search' or supports_formal)}
        if self.service.intake is None:
            actions.pop('hypothesis-formalize', None)
            actions.pop('hypothesis-revise', None)
        if (self.service.refinement is None or self.service.intake.config.lemma_reuse == 'disabled'
                or not self.service.formal.config.reuse_lean or not supports_formal):
            actions.pop('parent-refine', None)
        controls = {'coordination-read'} if contract_version == '2.1' else set()
        if self.service.formal is not None:
            controls.add('cancel')
        if self.service.formal is not None:
            controls.update({'reconcile-search', 'reconcile-search-jobs'})
        return CapabilitySet(contract_version, frozenset(actions),
                             controls=frozenset(controls),
                             backend_mode=self.service.formal.config.mode if available else 'unavailable',
                             independent_replay=bool(available and self.service.formal.config.mode == 'real'),
                             receiver_mode=self.service.receiver_mode)

    async def reconciliation_capabilities(self):
        """Configuration-only negotiation; recovery must not probe a slow provider."""
        controls = {'reconcile-search', 'reconcile-search-jobs'} if self.service.formal is not None else set()
        return CapabilitySet('2.0', frozenset(), frozenset(controls), receiver_mode=self.service.receiver_mode)

    async def submit(self, request: ActionRequest) -> Admission:
        if not isinstance(request, ActionRequest):
            raise TypeError("receiver requires ActionRequest")
        self._scope(request.workspace_id)
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
        if (self.service.cycles is not None and isinstance(request.action, SelectedProofCycle)
                and request.action.category != 'formal-run'):
            raise UnsupportedCapability('selected category has no configured cycle executor')
        capabilities = await self.capabilities(request.contract_version)
        if request.action.kind not in capabilities.actions:
            raise UnsupportedCapability("action is unavailable under this contract version")
        formal_action = isinstance(request.action, (FormalSearch, ParentRefine)) or (
            self.service.cycles is not None and isinstance(request.action, (ProofCycle, SelectedProofCycle)))
        if formal_action and self.service.formal is not None and self._formal_readiness() is not True:
            raise TransientUnavailable('formal provider readiness is pending; retry the original request')
        if isinstance(request.action, QueueResearchMoves) and request.preconditions.expected_revision is None:
            raise ValueError("guidance requires an expected proof revision")
        if request.deadline is not None and request.deadline <= datetime.now(timezone.utc):
            raise StalePrecondition("request deadline has elapsed", request_id=request.request_id)
        pre = request.preconditions
        if any((pre.environment_hash, pre.declaration_hash, pre.checkpoint_hash)):
            raise UnsupportedCapability("reference receiver cannot validate environment, declaration or checkpoint pins")
        proof_id = getattr(request.action, "proof_id", None)
        if isinstance(request.action, ProofCreate):
            try:
                spec = json.loads(self._artifact(request.action.spec_artifact.hash))
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
        if isinstance(request.action, (ProofCreate, HypothesisRevise)):
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
            exact = self._artifact(ref.hash)
            if ref.size_bytes is not None and ref.size_bytes != len(exact):
                raise ArtifactUnavailable("referenced artifact size differs")
        created = False
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM requests WHERE principal=? AND workspace_id=? AND request_id=?",
                                       (self.principal, self.workspace_id, request.request_id)).fetchone()
                if row is None:
                    if formal_action and self.service.formal is not None and self._formal_readiness() is not True:
                        raise TransientUnavailable('formal provider readiness changed before admission')
                    # Artifact validation and lock acquisition may cross the
                    # deadline. Linearize new admission only after rechecking;
                    # an existing admission remains recoverable after expiry.
                    if request.deadline is not None and request.deadline <= datetime.now(timezone.utc):
                        raise StalePrecondition("request deadline elapsed during admission", request_id=request.request_id)
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
            raw = self._artifact(action.spec_artifact.hash).decode("utf-8")
            args = {"spec": raw}
        elif isinstance(action, SelectedProofCycle): args = {"proof": action.proof_id, "category": action.category}
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
        elif isinstance(action, HypothesisFormalize):
            args = {"proof": action.proof_id, "hypothesis": action.hypothesis_id,
                    "expected_revision": str(request.preconditions.expected_revision)}
        elif isinstance(action, HypothesisRevise):
            args = {"proof": action.proof_id, "hypothesis": action.hypothesis_id,
                    "spec_artifact": action.spec_artifact.hash, "previous_claim": action.previous_claim_id or "",
                    "expected_revision": str(request.preconditions.expected_revision)}
        elif isinstance(action, ParentRefine):
            args = {'proof': action.proof_id, 'previous_declaration': action.previous_declaration_id,
                'lemma': action.lemma_claim_id, 'budget': json.dumps(to_dict(action.budget)),
                'expected_revision': str(request.preconditions.expected_revision),
                'expected_theorem_hash': request.preconditions.expected_theorem_hash}
        else: raise TypeError("unknown action")
        return {"schema_version": 1, "request_id": request.request_id,
                "workspace": self.workspace_id, "operation": action.kind, "arguments": args}

    def _execute(self, operation_id: str):
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row["lifecycle"] in TERMINAL_LIFECYCLES:
                return
            version = row["observation_version"] + 1
            self._db.execute("BEGIN IMMEDIATE")
            try:
                lifecycle = 'cancelling' if row['lifecycle'] == 'cancelling' else 'running'
                self._db.execute("UPDATE requests SET lifecycle=?, observation_version=? WHERE operation_id=?", (lifecycle, version, operation_id))
                self._event(operation_id, "running", version)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        job = None
        try:
            request = request_from_json(row["body"])
            native_cycle = isinstance(request.action, (ProofCycle, SelectedProofCycle)) and self.service.cycles is not None
            native_refinement = isinstance(request.action, ParentRefine) and self.service.refinement is not None
            if (isinstance(request.action, FormalSearch) and self.service.formal is not None) or native_cycle or native_refinement:
                raw = self._legacy_request(request)
                with self._service_lock:
                    job, _ = self.service.jobs.submit(raw)
                if job['phase'] in {'finished', 'failed'}:
                    outcome = job
                else:
                    def cancelled():
                        with self._lock:
                            return self._db.execute('SELECT lifecycle FROM requests WHERE operation_id=?',
                                (operation_id,)).fetchone()['lifecycle'] == 'cancelling'
                    if native_refinement:
                        action = request.action
                        outcome = self.service.refinement.run(job['operation_id'], action.proof_id,
                            action.previous_declaration_id, action.lemma_claim_id, to_dict(action.budget),
                            expected_revision=request.preconditions.expected_revision,
                            expected_theorem_hash=request.preconditions.expected_theorem_hash,
                            deadline=request.deadline.timestamp() if request.deadline else None,
                            cancelled=cancelled, on_progress=lambda report: self._progress(operation_id, report))
                    elif native_cycle:
                        outcome = self.service.cycles.run(job['operation_id'], request.action.proof_id,
                            category=getattr(request.action, 'category', None), cancelled=cancelled,
                            expected_revision=request.preconditions.expected_revision,
                            on_progress=lambda report: self._progress(operation_id, report))
                    else:
                        outcome = self.service.formal.run(job['operation_id'], request.action.proof_id,
                            request.action.claim_id, {'max_steps': request.action.budget.max_steps,
                                'wall_clock_ms': request.action.budget.wall_clock_ms}, cancelled=cancelled,
                            expected_revision=request.preconditions.expected_revision,
                            on_progress=lambda report: self._progress(operation_id, report))
                    with self._service_lock:
                        self.service.jobs.update(job['operation_id'], {**outcome, 'phase': 'finished',
                            'transport_status': 'ok', 'operation_id': job['operation_id']})
            else:
                outcome = self._execute_reference(request, operation_id)
            lifecycle = outcome.get('lifecycle') or ('failed' if outcome.get('phase') == 'failed'
                or outcome.get('transport_status') == 'error' else 'succeeded')
        except Exception as exc:
            lifecycle = 'failed'
            outcome = {'error_code': getattr(exc, 'code', None) or type(exc).__name__,
                'summary': getattr(exc, 'public_summary',
                    'Operation requires reconciliation; no mathematical outcome is inferred.')}
            if job is not None:
                with self._service_lock:
                    self.service.jobs.update(job['operation_id'], {**outcome, 'phase': 'failed',
                        'lifecycle': 'failed', 'transport_status': 'error', 'operation_id': job['operation_id']})
        self._finish(operation_id, lifecycle, version, outcome)

    def _execute_reference(self, request, operation_id):
        if isinstance(request.action, SelectedProofCycle):
            action = request.action
            with self._service_lock:
                outcome = self.service._cycle(action.proof_id, operation_id,
                    category=action.category, record_progress=False)
        elif isinstance(request.action, QueueResearchMoves):
            with self._service_lock:
                outcome = self.service.queue_research_moves(request.action,
                    expected_revision=request.preconditions.expected_revision)
        else:
            # Service admission/completion and its workflow stages own their
            # locks. An enclosing lock would also cover provider I/O and waits
            # for another execution of the same operation.
            outcome = self.service.submit(self._legacy_request(request))
        return outcome

    def _progress(self, operation_id, report):
        encoded = json.dumps(report, sort_keys=True)
        with self._lock:
            row = self._db.execute('SELECT * FROM requests WHERE operation_id=?', (operation_id,)).fetchone()
            if row['lifecycle'] in TERMINAL_LIFECYCLES or row['result'] == encoded:
                return
            version = row['observation_version'] + 1
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute('UPDATE requests SET result=?, observation_version=? WHERE operation_id=?',
                    (encoded, version, operation_id))
                self._event(operation_id, report['phase'], version)
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def _finish(self, operation_id, lifecycle, version, outcome):
        with self._lock:
            owner = self._db.execute('SELECT * FROM requests WHERE operation_id=?', (operation_id,)).fetchone()
        request = request_from_json(owner['body'])
        code = (outcome or {}).get('error_code') or ''
        read_only = isinstance(request.action, (ProofStatus, ProofAudit, ProofExport))
        unsettled = isinstance(code, str) and code.endswith('RECONCILIATION_REQUIRED')
        # This describes executor settlement, never mathematical truth. A
        # provider failure can leave remote search/replay activity unresolved.
        settled = (True if read_only or lifecycle in {'succeeded', 'cancelled'} and not unsettled
                   else None if lifecycle in TERMINAL_LIFECYCLES else False)
        outcome = {**outcome, 'execution_settled': settled}
        pointers = []
        if isinstance(outcome, dict):
            pointers = list(outcome.get("artifact_pointers", ()))
            pointers += [outcome[key] for key in ("packet_ref", "result_ref", "report_artifact", "export_artifact")
                         if isinstance(outcome.get(key), str)]
            pointers += self._bound_result_artifacts(request_from_json(owner['body']), outcome)
        pointers = self._available_artifacts(pointers)
        with self._lock:
            version = max(version, self._db.execute('SELECT observation_version FROM requests WHERE operation_id=?',
                (operation_id,)).fetchone()['observation_version'])
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for pointer in pointers:
                    self._db.execute("INSERT OR IGNORE INTO artifact_grants VALUES (?,?,?)",
                        (owner['principal'], owner['workspace_id'], pointer))
                self._db.execute("UPDATE requests SET lifecycle=?, observation_version=?, result=? WHERE operation_id=?",
                                 (lifecycle, version + 1, json.dumps(outcome, sort_keys=True), operation_id))
                self._event(operation_id, lifecycle, version + 1)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _available_artifacts(self, pointers):
        available = set()
        for pointer in pointers:
            try:
                ArtifactRef(pointer)
            except (ValueError, TypeError):
                continue
            if self.service.artifacts.exists(pointer):
                available.add(pointer)
        return available

    def _bound_result_artifacts(self, request, outcome):
        """New result pointers must match committed records in the requested proof.

        Do not recurse through model output or grant access from arbitrary hash
        strings. These are the receiver's published research/obstruction fields.
        """
        action = request.action
        proof = getattr(action, 'proof_id', None)
        if not isinstance(outcome, dict) or not proof or outcome.get('proof_id') != proof:
            return []
        pointers = []
        with self._service_lock:
            self.service.catch_up(proof)
            if isinstance(action, LemmaBridge) and outcome.get('obstruction_id') == action.obstruction_id:
                ids = outcome.get('hypothesis_ids')
                if isinstance(ids, list) and 1 <= len(ids) <= 3:
                    for node_id in ids:
                        if not isinstance(node_id, str) or not node_id.startswith(proof + '/'):
                            break
                        node = self.service.view.node(node_id)
                        if (node is None or node.label != 'SpeculativeHypothesis'
                                or action.obstruction_id not in node.fields.get('source_ids', [])
                                or any(not isinstance(outcome.get(key), str)
                                    or node.fields.get(key) != outcome[key]
                                    for key in ('packet_artifact', 'result_artifact'))):
                            break
                    else:
                        pointers.extend(outcome[key] for key in ('packet_artifact', 'result_artifact'))
            if isinstance(action, (FormalSearch, ProofCycle, SelectedProofCycle, ParentRefine)):
                claim = outcome.get('claim_id')
                if isinstance(claim, str) and claim.startswith(proof + '/'):
                    for key, format_ in (('lemma_use_audit_artifact', 'persistent-atp-lemma-use-audit-v1'),
                                         ('refinement_artifact', 'persistent-atp-parent-refinement-v1')):
                        ref = outcome.get(key)
                        node = self.service.view.node(ref) if isinstance(ref, str) else None
                        if (node is not None and node.label == 'Artifact' and node.fields.get('format') == format_
                                and any(e.dst_id == ref for e in self.service.view.edges_from(claim, 'HAS_ARTIFACT'))):
                            pointers.append(ref)
                for context in outcome.get('obstruction_contexts', []):
                    if not isinstance(context, dict):
                        continue
                    obstruction_id = context.get('obstruction_id')
                    if not isinstance(obstruction_id, str) or not obstruction_id.startswith(proof + '/'):
                        continue
                    obstruction = self.service.view.node(obstruction_id)
                    run = outcome.get('run_id')
                    if (obstruction is None or obstruction.label != 'Obstruction'
                            or obstruction.fields.get('formal_run_id') != run
                            or not any(edge.src_id == run for edge in
                                self.service.view.edges_to(obstruction_id, 'RAISED_OBSTRUCTION'))):
                        continue
                    bindings = {'packet_artifact': 'artifact_hash', 'manifest_artifact': 'report_artifact',
                                'payload_artifact': 'result_artifact'}
                    for edge in self.service.view.edges_from(obstruction_id, 'HAS_ARTIFACT'):
                        record = self.service.view.node(edge.dst_id)
                        if (record is not None and record.label == 'Artifact'
                                and all(isinstance(context.get(key), str)
                                    and record.fields.get(field) == context[key] for key, field in bindings.items())):
                            pointers.extend(context[key] for key in bindings)
        return pointers

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
            resolution = self._db.execute('SELECT * FROM search_reconciliations WHERE operation_id=?',
                (ref.operation_id,)).fetchone()
        if row is None:
            raise KeyError("operation not found")
        request = request_from_json(row["body"])
        outcome = json.loads(row["result"]) if row["result"] else None
        if resolution:
            if resolution['request_fingerprint'] != row['fingerprint']:
                raise ValueError('reconciliation differs from original receiver request')
            # Original result/lifecycle remain immutable. This separate receipt
            # changes only the operational observation version, never proof truth.
            outcome = {**(outcome or {}), 'execution_reconciliation': json.loads(resolution['summary'])}
        if row['lifecycle'] in TERMINAL_LIFECYCLES and outcome is not None:
            # Repair missing grants for already completed operations from an
            # older receiver without changing their observation or proof revision.
            pointers = self._available_artifacts(self._bound_result_artifacts(request, outcome))
            with self._lock:
                self._db.executemany('INSERT OR IGNORE INTO artifact_grants VALUES (?,?,?)',
                    [(row['principal'], row['workspace_id'], pointer) for pointer in pointers])
        proof_id = getattr(request.action, "proof_id", None) or (outcome or {}).get("proof_id")
        with self._service_lock:
            head = self.service.store.head(proof_id) if proof_id else (None, None)
            watermark = self.service.view.snapshot(proof_id)[0] if proof_id else None
        return OperationSnapshot(ref, row["request_id"], row["observation_version"], row["lifecycle"],
            execution_phase=(outcome or {}).get("phase"),
            mathematical_disposition=(outcome or {}).get("mathematical_disposition")
                or (outcome or {}).get("cycle_disposition"),
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
        target = self._artifact(target_ref)
        return ProjectDigest(workspace_id, proof_id, raw["revision"], raw["event_hash"],
                             raw["revision"] - raw["projection_lag"],
                             ArtifactRef(target_ref, len(target), raw["target_media_type"]),
                             raw.get("literal_target", target.decode("utf-8")), raw["claim_status"], bool(raw["formal_verified"]), raw["summary"],
                             self.service.receiver_mode)

    async def read_coordination(self, *, workspace_id: str, proof_id: str,
                                cursor: str | None = None, page_size: int = 100) -> CoordinationPage:
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
            selected, len(items), next_cursor, self.service.receiver_mode)

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
        return self._artifact(ref.hash)

    async def cancel(self, control: CancelRequest):
        if self.service.formal is None:
            raise UnsupportedCapability('executor cancellation is unavailable')
        self._scope(control.operation_ref.workspace_id)
        ref = control.operation_ref
        await self.get_operation(ref)  # Authorized receiver-owned operation.
        body = canonical_json(control)
        with self._lock:
            row = self._db.execute('SELECT * FROM requests WHERE operation_id=?', (ref.operation_id,)).fetchone()
            action = request_from_json(row['body']).action
            if not (isinstance(action, FormalSearch) or self.service.cycles is not None
                    and isinstance(action, (ProofCycle, SelectedProofCycle, ParentRefine))):
                raise UnsupportedCapability('operation has no configured cancellation handler')
            previous = self._db.execute('SELECT body FROM controls WHERE principal=? AND workspace_id=? AND request_id=?',
                (self.principal, self.workspace_id, control.request_id)).fetchone()
            if previous:
                if previous['body'] != body:
                    raise IdempotencyConflict('control identity was reused with another body')
            else:
                if control.expected_observation_version is not None and row['observation_version'] != control.expected_observation_version:
                    raise StalePrecondition('operation changed before cancellation')
                self._db.execute('BEGIN IMMEDIATE')
                try:
                    self._db.execute('INSERT INTO controls VALUES (?,?,?,?)',
                        (self.principal, self.workspace_id, control.request_id, body))
                    if row['lifecycle'] not in TERMINAL_LIFECYCLES and row['lifecycle'] != 'cancelling':
                        version = row['observation_version'] + 1
                        self._db.execute("UPDATE requests SET lifecycle='cancelling', observation_version=? WHERE operation_id=?",
                            (version, ref.operation_id))
                        self._event(ref.operation_id, 'cancelling', version)
                    self._db.execute('COMMIT')
                except BaseException:
                    self._db.execute('ROLLBACK')
                    raise
        return await self.get_operation(ref)

    @staticmethod
    def _search_reconciliation_action(row):
        """Eligibility describes uncertainty, independently of success/failure."""
        action = request_from_json(row['body']).action
        if row['lifecycle'] not in TERMINAL_LIFECYCLES or not isinstance(action, (FormalSearch, ProofCycle, SelectedProofCycle, ParentRefine)):
            raise UnsupportedCapability('only terminal receiver-owned formal searches can be reconciled')
        result = json.loads(row['result']) if row['result'] else {}
        code = result.get('error_code') or ''
        if (row['lifecycle'] != 'failed' and result.get('execution_settled') is True
                and not (isinstance(code, str) and code.endswith('RECONCILIATION_REQUIRED'))):
            raise UnsupportedCapability('formal search execution is already settled')
        return action

    async def reconcile_search(self, control: SearchReconciliationRequest):
        """Durably admit one observation job; never wait on provider I/O here."""
        if not isinstance(control, SearchReconciliationRequest):
            raise TypeError('typed search reconciliation control is required')
        self._scope(control.operation_ref.workspace_id)
        if self.service.formal is None:
            raise UnsupportedCapability('formal search reconciliation is unavailable')
        body = canonical_json({'kind': 'reconcile-search', 'request': control})
        with self._lock:
            previous = self._db.execute('SELECT body FROM controls WHERE principal=? AND workspace_id=? AND request_id=?',
                (self.principal, self.workspace_id, control.request_id)).fetchone()
            if previous and previous['body'] != body:
                raise IdempotencyConflict('control identity was reused with another body')
            job = self._job_row(control.request_id)
            if job:
                return self._job_snapshot(job)
            row = self._db.execute('SELECT * FROM requests WHERE principal=? AND workspace_id=? AND operation_id=?',
                (self.principal, self.workspace_id, control.operation_ref.operation_id)).fetchone()
            if row is None:
                raise KeyError('operation not found')
            if not previous and row['observation_version'] != control.expected_observation_version:
                raise StalePrecondition('operation changed before reconciliation')
            self._search_reconciliation_action(row)
            resolved = self._db.execute('SELECT 1 FROM search_reconciliations WHERE operation_id=?', (row['operation_id'],)).fetchone()
        if not resolved:
            with self._service_lock:
                original = self.service.jobs.conn.execute('SELECT phase FROM operations WHERE request_id=? AND workspace=?',
                    (row['request_id'], self.workspace_id)).fetchone()
            if original is None or original['phase'] not in {'failed', 'finished'}:
                raise UnsupportedCapability('original terminal formal job is unavailable')
        with self._lock:
            # Recheck after crossing the service lock: concurrent admission may
            # have reserved this control or the same operation in the meantime.
            job = self._job_row(control.request_id)
            if job:
                if job['body'] != body:
                    raise IdempotencyConflict('control identity was reused with another body')
                return self._job_snapshot(job)
            previous = self._db.execute('SELECT body FROM controls WHERE principal=? AND workspace_id=? AND request_id=?',
                (self.principal, self.workspace_id, control.request_id)).fetchone()
            if previous and previous['body'] != body:
                raise IdempotencyConflict('control identity was reused with another body')
            current = self._db.execute('SELECT observation_version FROM requests WHERE operation_id=?', (row['operation_id'],)).fetchone()
            if not previous and current['observation_version'] != control.expected_observation_version:
                raise StalePrecondition('operation changed before reconciliation admission')
            resolved = self._db.execute('SELECT 1 FROM search_reconciliations WHERE operation_id=?', (row['operation_id'],)).fetchone()
            if self._db.execute("SELECT 1 FROM reconciliation_jobs WHERE operation_id=? AND state IN ('queued','running')",
                    (row['operation_id'],)).fetchone():
                raise ReviewRequired('another reconciliation job already owns this operation')
            count = self._db.execute("SELECT COUNT(*) FROM reconciliation_jobs WHERE state IN ('queued','running')").fetchone()[0]
            if not resolved and count >= MAX_RECONCILIATION_JOBS:
                raise QuotaExceeded('reconciliation queue is full')
            self._db.execute('BEGIN IMMEDIATE')
            try:
                if not previous:
                    self._db.execute('INSERT INTO controls VALUES (?,?,?,?)',
                        (self.principal, self.workspace_id, control.request_id, body))
                self._db.execute('INSERT INTO reconciliation_jobs(principal,workspace_id,request_id,operation_id,body,state,deadline) VALUES (?,?,?,?,?,?,?)',
                    (self.principal, self.workspace_id, control.request_id, row['operation_id'], body,
                     'succeeded' if resolved else 'queued', time.time() + 120))
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
            job = self._job_snapshot(self._job_row(control.request_id))
        if job.state == 'queued':
            self._schedule_reconciliation(control.request_id)
        return job

    def _job_row(self, request_id):
        return self._db.execute('SELECT * FROM reconciliation_jobs WHERE principal=? AND workspace_id=? AND request_id=?',
            (self.principal, self.workspace_id, request_id)).fetchone()

    @staticmethod
    def _job_snapshot(row):
        return SearchReconciliationJob(row['request_id'], OperationRef(row['workspace_id'], row['operation_id']),
            row['state'], row['version'], row['error_code'], row['summary'])

    async def read_search_reconciliation(self, *, workspace_id, request_id):
        self._scope(workspace_id)
        with self._lock:
            row = self._job_row(request_id)
        if row is None:
            raise KeyError('reconciliation job not found')
        return self._job_snapshot(row)

    def _schedule_reconciliation(self, request_id):
        with self._lock:
            if request_id in self._reconciliation_pending:
                return
            self._reconciliation_pending.add(request_id)
        def run():
            try:
                self._run_reconciliation(request_id)
            finally:
                with self._lock:
                    self._reconciliation_pending.discard(request_id)
        try:
            self._reconciliation_executor.submit(run)
        except BaseException:
            with self._lock:
                self._reconciliation_pending.discard(request_id)
            raise

    def _run_reconciliation(self, request_id):
        with self._lock:
            row = self._job_row(request_id)
            if row is None or row['state'] != 'queued':
                return
            self._db.execute("UPDATE reconciliation_jobs SET state='running', version=version+1, attempts=attempts+1 "
                "WHERE sequence=?", (row['sequence'],))
        try:
            raw = json.loads(row['body'])['request']
            control = SearchReconciliationRequest(raw['request_id'], OperationRef(**raw['operation_ref']), raw['expected_observation_version'])
            if time.time() >= row['deadline']:
                raise ReviewRequired('Reconciliation observation deadline elapsed; review a new control.')
            self._reconcile_search(control, deadline=row['deadline'])
        except Exception as exc:
            error = 'REVIEW_REQUIRED'
            summary = str(exc)[:500] if isinstance(exc, ReviewRequired) else 'Search reconciliation failed; review the original control and provider binding.'
            state = 'blocked'
        else:
            state, error, summary = 'succeeded', None, None
        with self._lock:
            self._db.execute('UPDATE reconciliation_jobs SET state=?, version=version+1, error_code=?, summary=? WHERE sequence=?',
                (state, error, summary, row['sequence']))

    def _reconcile_search(self, control, *, deadline):
        """Worker-only observation and gate admission for the original search."""
        if not isinstance(control, SearchReconciliationRequest):
            raise TypeError('typed search reconciliation control is required')
        self._scope(control.operation_ref.workspace_id)
        if self.service.formal is None:
            raise UnsupportedCapability('formal search reconciliation is unavailable')
        with self._lock:
            lock = self._reconcile_locks.setdefault(control.operation_ref.operation_id, threading.Lock())
        with lock:
            body = canonical_json({'kind': 'reconcile-search', 'request': control})
            with self._lock:
                row = self._db.execute('SELECT * FROM requests WHERE operation_id=?',
                    (control.operation_ref.operation_id,)).fetchone()
                previous = self._db.execute('SELECT body FROM controls WHERE principal=? AND workspace_id=? AND request_id=?',
                    (self.principal, self.workspace_id, control.request_id)).fetchone()
                if previous and previous['body'] != body:
                    raise IdempotencyConflict('control identity was reused with another body')
                resolved = self._db.execute('SELECT 1 FROM search_reconciliations WHERE operation_id=?',
                    (row['operation_id'],)).fetchone()
                if not previous and row['observation_version'] != control.expected_observation_version:
                    raise StalePrecondition('operation changed before reconciliation')
                if resolved and not previous:
                    self._db.execute('INSERT INTO controls VALUES (?,?,?,?)',
                        (self.principal, self.workspace_id, control.request_id, body))
            if resolved:
                return
            request = request_from_json(row['body'])
            self._search_reconciliation_action(row)
            with self._service_lock:
                job = self.service.jobs.conn.execute('SELECT operation_id, phase FROM operations WHERE request_id=? AND workspace=?',
                    (row['request_id'], self.workspace_id)).fetchone()
            if job is None or job['phase'] not in {'failed', 'finished'}:
                raise UnsupportedCapability('original terminal formal job is unavailable')
            with self._lock:
                # Reserve the identity before provider observation or a gate
                # commit. A crash/retry retains it; no new work is authorized.
                previous = self._db.execute('SELECT body FROM controls WHERE principal=? AND workspace_id=? AND request_id=?',
                    (self.principal, self.workspace_id, control.request_id)).fetchone()
                if previous and previous['body'] != body:
                    raise IdempotencyConflict('control identity was reused with another body')
                if not previous:
                    self._db.execute('INSERT INTO controls VALUES (?,?,?,?)',
                        (self.principal, self.workspace_id, control.request_id, body))
            # Network I/O never holds either SQLite connection's lock.
            from mathproof.maths_ai_http import MathsAIAPIError
            from context_compiler.errors import ContextValidationError
            try:
                receipt = self.service.formal.reconcile_search(job['operation_id'], request.action.proof_id, deadline=deadline)
            except (MathsAIAPIError, ContextValidationError) as exc:
                raise ReviewRequired(str(exc)) from exc
            receipt.update(operation_ref=control.operation_ref.to_dict(), request_id=row['request_id'],
                request_fingerprint=row['fingerprint'], service_operation_id=job['operation_id'])
            with self._service_lock:
                artifact = self.service.artifacts.put(canonical_json(receipt))
            summary = {key: receipt[key] for key in ('format', 'operation_ref', 'request_id',
                'request_fingerprint', 'proof_id', 'run_id', 'execution_settled', 'replay_performed')}
            summary['receipt_artifact'] = artifact
            with self._lock:
                self._db.execute('BEGIN IMMEDIATE')
                try:
                    self._db.execute('INSERT INTO search_reconciliations VALUES (?,?,?,?)',
                        (row['operation_id'], row['fingerprint'], artifact, json.dumps(summary, sort_keys=True)))
                    self._db.execute('INSERT OR IGNORE INTO artifact_grants VALUES (?,?,?)',
                        (self.principal, self.workspace_id, artifact))
                    version = row['observation_version'] + 1
                    self._db.execute('UPDATE requests SET observation_version=? WHERE operation_id=?',
                        (version, row['operation_id']))
                    self._event(row['operation_id'], 'search-reconciled', version)
                    self._db.execute('COMMIT')
                except BaseException:
                    self._db.execute('ROLLBACK')
                    raise

    async def amend_budget(self, control: BudgetAmendment):
        raise UnsupportedCapability("reference budget amendment is unavailable")

    async def answer_decision(self, control: DecisionResponse):
        raise UnsupportedCapability("reference decision workflow is unavailable")

    async def aclose(self):
        # Service lifecycle, not caller-client lifecycle.
        return None

    def close(self):
        with self._lock:
            self._closing = True
        self._executor.shutdown(wait=True)
        self._reconciliation_executor.shutdown(wait=True)
        self._readiness_executor.shutdown(wait=True)
        with self._lock:
            self._db.close()
