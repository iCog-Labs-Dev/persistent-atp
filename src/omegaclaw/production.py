"""Configured MORK/PostgreSQL receiver service; no reference executor fallback."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from pathlib import Path

from artifacts.errors import ArtifactNotFoundError, ArtifactStoreError
from artifacts.hashing import hash_content
from artifacts.store import ArtifactStore
from commit_gate.canon import canonical_json
from commit_gate.gate import CommitGate, ProjectionError
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore
from mathproof.http_atp import ATPServiceError
from mathproof.projection import result_operations
from mathproof.scheduler import GlobalScheduler
from mork.backend import MorkSpace, MorkView, ProjectionUnavailable
from persistent_atp_client.errors import ArtifactUnavailable, StalePrecondition, UnsupportedCapability
from persistent_atp_client.models import (
    FormalSearch, ProofAudit, ProofCreate, ProofCycle, ProofExport, ProofStatus,
    QueueResearchMoves, SelectedProofCycle,
)

from .execution import ProductionExecutor, ExecutionPending, ExecutionStore
from .jobs import JobLedger
from .service import OperationService

_process_lock = threading.Lock()
_process_root = None


class PostgresArtifacts:
    """One owned connection per operation, including background receiver work."""
    mode = "postgres"

    def __init__(self, db_url: str):
        from psycopg.conninfo import make_conninfo
        self._db_url = make_conninfo(db_url, connect_timeout=3)
        self.exists("sha256:" + "0" * 64)  # Validate connectivity and applied schema.

    def _call(self, method, *args):
        try:
            with ArtifactStore(self._db_url) as store:
                result = getattr(store, method)(*args)
                if method == "put" and store.get(result) != args[0]:
                    raise ValueError("configured artifact store contains corrupt bytes")
                if method == "get" and hash_content(result) != args[0]:
                    raise ValueError("configured artifact bytes do not match their hash")
                if method == "exists" and result and hash_content(store.get(args[0])) != args[0]:
                    raise ValueError("configured artifact bytes do not match their hash")
                return result
        except ArtifactNotFoundError as exc:
            raise ArtifactUnavailable("artifact bytes are missing") from exc
        except ArtifactStoreError as exc:
            raise ArtifactUnavailable("configured artifact store is unavailable") from exc

    def put(self, data): return self._call("put", data)
    def get(self, pointer): return self._call("get", pointer)
    def exists(self, pointer): return self._call("exists", pointer)


class ProductionService(OperationService):
    """One receiver owner per state root; asynchronous runs keep durable identity.

    Initial canonical formal runs are supported. Alignment, creative workers,
    resume/budget controls and replay promotion require their own integrations;
    unsupported actions are rejected before admission, never scripted.
    """
    component_mode = "production"
    production = True
    ACTIONS = frozenset({"proof-create", "proof-cycle", "proof-cycle-selected", "proof-status",
                         "proof-audit", "proof-export", "formal-search", "queue-research-moves"})

    def __init__(self, root, *, workspace, artifacts: PostgresArtifacts,
                 executor: ProductionExecutor, snapshot: str | None = None):
        if type(artifacts) is not PostgresArtifacts or not isinstance(executor, ProductionExecutor):
            raise ValueError("production requires configured PostgreSQL and a real production executor")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.lock = threading.RLock()
        global _process_root
        if not _process_lock.acquire(blocking=False):
            raise ValueError("one production receiver may own native MORK in this process")
        if _process_root is not None and _process_root != self.root.resolve():
            _process_lock.release()
            raise ValueError("changing the authoritative journal requires a fresh MORK process")
        try:
            self._owner = (self.root / "receiver.lock").open("a+")
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if hasattr(self, "_owner"): self._owner.close()
            _process_lock.release()
            raise ValueError("another receiver owns this state root") from None
        self.artifacts = artifacts
        self.executor = executor
        self._closed = False
        try:
            profile = {"mode": "production", "workspace": workspace}
            manifest = self.root / "receiver-profile.json"
            if manifest.exists():
                if json.loads(manifest.read_text()) != profile:
                    raise ValueError("receiver state root belongs to a different mode/workspace")
            else:
                if any((self.root / name).exists() for name in ("journal.sqlite3", "receiver-v2.sqlite3", "operations.sqlite3")):
                    raise ValueError("existing receiver data requires explicit production migration")
                temporary = manifest.with_suffix(".json.tmp")
                with temporary.open("w") as stream:
                    json.dump(profile, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(manifest)
                directory = os.open(self.root, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
            self.store = JournalStore(str(self.root / "journal.sqlite3"), allow_cross_thread=True)
            self.jobs = JobLedger(self.root / "operations.sqlite3")
            self.executions = ExecutionStore(self.root / "operations.sqlite3")
            self.space = MorkSpace()
            from mork.provenance import RECORD, attest, verify
            verify(json.loads(RECORD.read_text()), attest(self.space))
            _process_root = self.root.resolve()
            self.view = MorkView(self.space)
            self.gate = CommitGate(self.view, self.store)
            for proof in self.store.proof_ids():
                self.store.verify_chain(proof)
            if snapshot:
                from mork.snapshot import restore_backup
                restore_backup(self.space, self.view, self.store, snapshot)
            self.gate.catch_up()
            if not self.health()["ready"]:
                raise ValueError("MORK projection is not ready")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        for name in ("executions", "jobs", "store"):
            resource = getattr(self, name, None)
            if resource is not None:
                if name == "jobs": resource.conn.close()
                else: resource.close()
        fcntl.flock(self._owner, fcntl.LOCK_UN)
        self._owner.close()
        _process_lock.release()

    def health(self):
        with self.lock:
            try:
                proofs = set(self.store.proof_ids())
                ready = all(self.view.projected_revision(proof) == self.store.head(proof)[0] for proof in proofs)
                ready = ready and not self.space.unavailable and not self.space.invalid_proofs.intersection(proofs)
            except Exception:
                ready = False
            return {"ready": ready, "receiver_mode": self.component_mode,
                    "graph_backend": "mork", "artifact_backend": self.artifacts.mode,
                    "backend_mode": self.executor.mode, "formal_provider": self.executor.kind,
                    "environment_hash": getattr(self.executor, "environment_hash", None), "independent_replay": False}

    def catch_up(self, proof_id):
        self.gate.catch_up(proof_id)

    def submit(self, raw):
        raise UnsupportedCapability("production receiver requires the typed v2 API")

    def validate_action(self, action):
        if action.kind not in self.ACTIONS:
            raise UnsupportedCapability("action has no configured production executor")
        if isinstance(action, SelectedProofCycle) and action.category != "formal-run":
            raise UnsupportedCapability("only formal-run execution is configured")
        if isinstance(action, FormalSearch) and (
            action.budget.max_steps > 10_000 or action.budget.wall_clock_ms > 600_000
        ):
            raise UnsupportedCapability("formal budget exceeds configured production bounds")

    def execute_request(self, request, operation_id):
        try:
            return self._execute_request(request, operation_id)
        except ArtifactUnavailable as exc:
            raise ExecutionPending("configured artifact storage is unavailable") from exc
        except (ProjectionUnavailable, ProjectionError) as exc:
            raise ExecutionPending("proof is committed or MORK is unavailable; catch-up/restart required") from exc
        except ExecutionPending:
            raise
        except Exception:
            record = self.executions.get(operation_id)
            if record:
                canonical = json.loads(record["request"])
                with self.lock:
                    if any(op.get("op") == "upsert_node" and op.get("id") == record["run_id"]
                           and op.get("fields", {}).get("operation_id") == operation_id
                           and op["fields"].get("request_artifact") == record["request_artifact"]
                           for event in self.store.read_events(canonical["proof_id"]) for op in event["ops"]):
                        raise ExecutionPending("journal commit exists; operational receipt reconciliation required")
                    self.store.release_lease(canonical["proof_id"], canonical["lease_id"],
                                             fencing_token=canonical["fencing_token"])
                    self.executions.update(operation_id, phase="failed")
            raise

    def _execute_request(self, request, operation_id):
        self.validate_action(request.action)
        action = request.action
        if isinstance(action, (FormalSearch, ProofCycle, SelectedProofCycle)):
            return self._formal(request, operation_id)
        with self.lock:
            if isinstance(action, ProofCreate):
                return self._create(self.artifacts.get(action.spec_artifact.hash).decode(), operation_id)
            if isinstance(action, QueueResearchMoves):
                result = self.queue_research_moves(action, expected_revision=request.preconditions.expected_revision)
                return {**result, "component_mode": self.component_mode}
            if isinstance(action, ProofStatus): return self._status(action.proof_id)
            if isinstance(action, ProofAudit): return self._audit(action.proof_id)
            if isinstance(action, ProofExport): return self._export(action.proof_id, action.destination_alias)
        raise UnsupportedCapability("production action is unavailable")

    def _status(self, proof_id):
        return {**super()._status(proof_id), "component_mode": self.component_mode}

    def _aligned(self, proof_id, claim_id, declaration_id):
        for edge in self.view.edges_to(declaration_id, "ALIGNS_DECLARATION"):
            alignment = self.view.node(edge.src_id)
            if (alignment and alignment.fields.get("lifecycle") == "reviewed"
                    and alignment.fields.get("verdict") == "aligned"
                    and any(link.dst_id == claim_id for link in self.view.edges_from(edge.src_id, "ALIGNS_CLAIM"))):
                return alignment
        return None

    def _prepare_formal(self, action, expected_revision, operation_id):
        proof_id = action.proof_id
        self.catch_up(proof_id)
        revision = self.store.head(proof_id)[0]
        if expected_revision is not None and revision != expected_revision:
            raise StalePrecondition("proof changed before formal dispatch")
        claim_id = action.claim_id if isinstance(action, FormalSearch) else f"{proof_id}/c-1"
        scheduler = GlobalScheduler(self.view, self.store, worker_actors={"formal-atp": "receiver-formal-atp"})
        candidates = [item for item in scheduler.build_frontier(proof_id)
                      if item.category == "formal-run" and (node := self.view.node(item.move_id))
                      and node.label == "FormalDeclaration" and self._aligned(proof_id, claim_id, node.node_id)]
        if not candidates:
            if isinstance(action, FormalSearch):
                raise UnsupportedCapability("no eligible declaration with reviewed exact alignment")
            return None
        # Select through the scheduler, constrained to this action's aligned claim.
        eligible = {item.move_id for item in candidates}
        lease_id = f"{proof_id}/ls-{int(hashlib.sha256(operation_id.encode()).hexdigest(), 16) + 1}"
        active = {row.lease_id for row in self.store.active_leases(proof_id)}
        lease = self.store.read_lease(proof_id, lease_id)
        if lease is not None:
            if (lease.lease_id not in active or lease.base_revision != revision
                    or lease.selected_move_id not in eligible or lease.actor != "receiver-formal-atp"
                    or lease.worker_class != "formal-atp"):
                raise StalePrecondition("original dispatch lease cannot be recovered without reassessment")
        else:
            lease = scheduler.lease_best_global_move(proof_id, category="formal-run",
                expected_revision=revision, eligible_move_ids=eligible, ttl_seconds=720, lease_id_hint=lease_id)
        if lease is None:
            raise ExecutionPending("eligible work is leased; dispatch has not started")
        try:
            declaration = self.view.node(lease.selected_move_id)
            source = declaration.fields.get("lean_source_artifact") or declaration.fields.get("source_hash")
            alignment = self._aligned(proof_id, claim_id, declaration.node_id)
            if not source or alignment.fields.get("source_hash") != source:
                raise ValueError("alignment must bind the exact declaration source artifact")
            if hash_content(self.artifacts.get(source)) != source:
                raise ValueError("declaration source hash mismatch")
            goal = declaration.fields.get("lean_type")
            context = declaration.fields.get("local_context", [])
            if not isinstance(goal, str) or not goal.strip() or not isinstance(context, list):
                raise ValueError("reviewed declaration lacks an exact Lean goal/context")
            if context:
                raise UnsupportedCapability("initial search requires a closed reviewed declaration; context elaboration is not configured")
            environments = self.view.edges_from(declaration.node_id, "PINNED_ENVIRONMENT")
            if len(environments) != 1:
                raise ValueError("declaration must pin exactly one environment")
            environment = self.view.node(environments[0].dst_id)
            if environment is None or environment.label != "Environment":
                raise ValueError("pinned environment is missing")
            budget = ({"max_steps": action.budget.max_steps, "wall_clock_ms": action.budget.wall_clock_ms}
                      if isinstance(action, FormalSearch) else {"max_steps": 100, "wall_clock_ms": 10_000})
            serial = int(hashlib.sha256(operation_id.encode()).hexdigest(), 16) + 1
            canonical = {"proof_id": proof_id, "claim_id": claim_id,
                "formal_declaration_id": declaration.node_id, "run_id": f"{proof_id}/fr-{serial}",
                "base_revision": revision, "lease_id": lease.lease_id, "fencing_token": lease.fencing_token,
                "lean_source_artifact": source, "environment_id": environment.node_id,
                "environment_hash": environment.fields["environment_hash"],
                "goal_text": goal, "hypotheses": context, "budget": budget}
            self.executor.restore(canonical)
            pointer = self.artifacts.put(canonical_json(canonical))
            self.executions.prepare(operation_id, canonical, pointer, self.executor.identity)
            return self.executions.get(operation_id)
        except BaseException:
            scheduler.release(proof_id, lease.lease_id, fencing_token=lease.fencing_token)
            raise

    def _commit_receipt(self, operation_id, record):
        proof_id = json.loads(record["request"])["proof_id"]
        run = self.view.node(record["run_id"])
        if run is None:
            return None
        if run.fields.get("operation_id") != operation_id or run.fields.get("request_artifact") != record["request_artifact"]:
            raise ValueError("run is bound to a different receiver operation")
        self.store.verify_chain(proof_id)
        for revision, event in enumerate(self.store.read_events(proof_id), 1):
            if any(op.get("op") == "upsert_node" and op.get("id") == record["run_id"]
                   and op.get("fields", {}).get("operation_id") == operation_id for op in event["ops"]):
                chain = self.store.read_chain(proof_id)
                journal_hash = next(row[1] for row in chain if row[0] == revision)
                self.executions.update(operation_id, phase="committed", revision=revision, journal_hash=journal_hash)
                failed = run.fields["status"] in {"internal-error", "environment-error", "invalid-request", "cancelled"}
                return {**self._status(proof_id), "run_id": record["run_id"],
                        **({"phase": "failed", "error_code": run.fields["status"].upper().replace("-", "_")} if failed else {}),
                        "cycle_disposition": run.fields["status"], "commit_revision": revision,
                        "commit_hash": journal_hash, "request_ref": record["request_artifact"],
                        "result_ref": record["result_artifact"]}
        raise ValueError("run has no authoritative receiver commit")

    def _formal(self, action_request, operation_id):
        with self.lock:
            record = self.executions.get(operation_id)
            if record is None:
                record = self._prepare_formal(action_request.action, action_request.preconditions.expected_revision, operation_id)
                if record is None:
                    proof = action_request.action.proof_id
                    frontier = GlobalScheduler(self.view, self.store).build_frontier(proof)
                    return {**self._status(proof), "cycle_disposition": "blocked" if frontier else "idle",
                            **({"error_code": "NO_CONFIGURED_EXECUTOR_FOR_FRONTIER"} if frontier else {})}
            if record["executor_id"] != self.executor.identity:
                raise ExecutionPending("original formal executor is not configured")
            canonical = json.loads(record["request"])
            self.catch_up(canonical["proof_id"])
            receipt = self._commit_receipt(operation_id, record)
            if receipt is not None:
                return receipt
            self.executions.update(operation_id, phase="reconciling")
            allow_start = self.store.head(canonical["proof_id"])[0] == canonical["base_revision"] and any(
                row.lease_id == canonical["lease_id"] and row.fencing_token == canonical["fencing_token"]
                for row in self.store.active_leases(canonical["proof_id"]))
        # No journal transaction or service read lock spans network execution.
        try:
            result = self.executor.observe_or_start(canonical, allow_start=allow_start)
        except ATPServiceError as exc:
            raise ExecutionPending("formal run outcome is unavailable; reconciliation required") from exc
        pointer = self.artifacts.put(canonical_json(result))
        self.executions.update(operation_id, phase="result-saved", result_artifact=pointer)
        if result["disposition"] == "searching":
            raise ExecutionPending("formal run is still searching")
        if result.get("states"):
            roots = [state for state in result["states"] if state["state_id"] == result.get("root_state_id")]
            if len(roots) != 1 or roots[0]["goal_text"] != canonical["goal_text"] or roots[0].get("local_context") != canonical["hypotheses"]:
                raise ValueError("formal result root differs from the exact dispatched goal/context")
        if any(state.get("status") == "lean-verified" for state in result["states"]) or (
            result.get("certificate") and result["certificate"].get("status") not in {"candidate", "replay-pending"}
        ):
            raise ValueError("independent replay promotion is not an ATP search result")
        # The helper rejects missing exact identities and certificate witnesses.
        ops = list(result_operations(result, environment_id=canonical["environment_id"], actor="receiver-formal-atp"))
        pointers = {item["sha256"] for item in result.get("artifacts", [])}
        if result.get("certificate"):
            pointers.add(result["certificate"]["artifact_hash"])
        for obstruction in result.get("obstructions", []):
            pointers.update(obstruction.get("diagnostic_artifacts", []))
            if obstruction.get("minimal_state_artifact"): pointers.add(obstruction["minimal_state_artifact"])
        try:
            for artifact in pointers:
                self.executor.materialize(artifact, self.artifacts)
        except ATPServiceError as exc:
            raise ExecutionPending("formal result artifacts are unavailable") from exc
        run_op = ops[0]
        ops[0] = UpsertNode(run_op.label, run_op.node_id, {**run_op.fields,
            "operation_id": operation_id, "request_artifact": record["request_artifact"],
            "result_artifact": pointer, "executor_id": self.executor.identity})
        with self.lock:
            try:
                committed = self.gate.commit(Proposal(canonical["proof_id"], "receiver-formal-atp", "formal-atp",
                    ops=tuple(ops), base_revision=canonical["base_revision"], lease_id=canonical["lease_id"],
                    fencing_token=canonical["fencing_token"], selected_move_id=canonical["formal_declaration_id"]))
                if not committed.accepted:
                    self.executions.update(operation_id, phase="gate-rejected")
                    return {**self._status(canonical["proof_id"]), "phase": "failed",
                            "error_code": "GATE_REJECTED", "run_id": record["run_id"], "result_ref": pointer,
                            "rejections": [item.to_dict() for item in committed.rejections]}
            except ProjectionError as exc:
                self.executions.update(operation_id, phase="committed-unprojected", revision=exc.revision,
                                       journal_hash=exc.event_hash)
                raise ExecutionPending("result committed; MORK catch-up required") from exc
            finally:
                self.store.release_lease(canonical["proof_id"], canonical["lease_id"], fencing_token=canonical["fencing_token"])
            return self._commit_receipt(operation_id, self.executions.get(operation_id))
