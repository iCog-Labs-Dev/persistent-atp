"""Trusted operation facade: Omega never sees a Proposal or lease token."""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore, ConcurrencyError
from commit_gate.reasons import Reason
from persistent_atp_client.errors import StalePrecondition
from mathproof.scheduler import GlobalScheduler
from mathproof.schemas import validate as validate_schema
from alignment.models import AlignmentCriteria, AlignmentReviewRequest, AlignmentReviewResult
from alignment.reviewer import RuleBasedAlignmentReviewer
from alignment.worker import build_alignment_proposal
from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict

from .artifact_bytes import ReferenceArtifactStore
from .contracts import ContractError, validate_request
from .jobs import JobLedger
from .export import check_export, referenced_artifacts
from .context import compile_packet, verify_packet

_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
ACTOR = "omega-coordinator"


class OperationService:
    def __init__(self, root: str | Path, *, workspace: str = "reference", artifacts=None,
                 critic_command: tuple[str, ...] | None = None, critic_timeout_s: float = 2.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.store = JournalStore(str(self.root / "journal.sqlite3"), allow_cross_thread=True)
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.store)
        self.jobs = JobLedger(self.root / "operations.sqlite3")
        self.artifacts = artifacts or ReferenceArtifactStore(self.root / "artifacts")
        if critic_timeout_s <= 0 or critic_timeout_s > 10:
            raise ValueError("reference critic timeout must be within (0, 10] seconds")
        self.critic_command = critic_command or (sys.executable, "-m", "omegaclaw.reference_critic")
        self.critic_timeout_s = critic_timeout_s
        for proof_id in self.store.proof_ids():
            self.catch_up(proof_id)

    def catch_up(self, proof_id: str) -> None:
        while self.view.snapshot(proof_id)[0] < self.store.head(proof_id)[0]:
            self.view.apply_journal_event(self.store, proof_id, self.view.snapshot(proof_id)[0] + 1)

    def submit(self, raw: object) -> dict:
        request = validate_request(raw)
        if request["workspace"] != self.workspace:
            raise ContractError("workspace is not registered")
        if request["operation"] == "proof-cycle":
            active = self.jobs.active_cycle(request["arguments"]["proof"], self.workspace,
                excluding_request_id=request["request_id"])
            if active:
                return active
        record, created = self.jobs.submit(request)
        if not created and record["phase"] in ("finished", "failed"):
            return record
        operation_id = record["operation_id"]
        try:
            result = self._execute(request, operation_id)
            response = {"operation_id": operation_id, "phase": "finished", "transport_status": "ok", **result}
        except (ContractError, ValueError, KeyError) as exc:
            response = {"operation_id": operation_id, "phase": "failed", "transport_status": "error", "error_code": type(exc).__name__, "summary": str(exc)[:500]}
        self.jobs.update(operation_id, response)
        return response

    def _execute(self, request: dict, operation_id: str) -> dict:
        operation = request["operation"]
        args = request["arguments"]
        if operation == "proof-create":
            return self._create(args["spec"], operation_id)
        proof_id = args["proof"]
        if not _ID.fullmatch(proof_id):
            raise ContractError("invalid proof id")
        if self.store.head(proof_id)[0] == 0:
            raise ContractError("unknown proof")
        self.catch_up(proof_id)
        if operation == "proof-status":
            return self._status(proof_id)
        if operation == "proof-audit":
            return self._audit(proof_id)
        if operation == "proof-cycle":
            return self._cycle(proof_id, operation_id)
        if operation == "proof-export":
            return self._export(proof_id, args["destination"])
        if operation in ("formal-search", "formal-resume"):
            budget = self._parse_budget(args["budget"])
            if operation == "formal-search":
                if args["claim"] != f"{proof_id}/c-1":
                    raise ContractError("formal search claim differs from literal target")
                declaration = self.view.node(f"{proof_id}/fd-1")
                if declaration is None or declaration.fields.get("status") != "aligned":
                    return {**self._status(proof_id), "summary": "reviewed exact statement alignment is required",
                        "error_code": "ALIGNMENT_REQUIRED", "component_mode": "blocked", "budget": budget}
                return {**self._status(proof_id), "summary": "configured formal backend is unavailable",
                    "error_code": "FORMAL_BACKEND_UNAVAILABLE", "component_mode": "blocked", "budget": budget}
            run = self.view.node(args["run"])
            if run is None or run.label != "FormalRun":
                raise ContractError("unknown formal run")
            return {**self._status(proof_id), "summary": "backend checkpoint continuation is unsupported",
                "error_code": "UNSUPPORTED_CHECKPOINT_RESUME", "component_mode": "blocked", "budget": budget}
        if operation == "statement-align":
            return self._align(proof_id, args["claim"], args["lean_source"], operation_id)
        if operation == "lemma-bridge":
            return self._bridge(proof_id, args["obstruction"], operation_id)
        raise ContractError("unknown operation")

    @staticmethod
    def _parse_budget(raw: str) -> dict:
        try:
            budget = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError("budget is not JSON") from exc
        if not isinstance(budget, dict) or set(budget) != {"max_steps", "wall_clock_ms"}:
            raise ContractError("budget requires max_steps and wall_clock_ms")
        if any(type(value) is not int or value <= 0 for value in budget.values()):
            raise ContractError("budget values must be positive integers")
        return {"max_steps": min(budget["max_steps"], 100),
            "wall_clock_ms": min(budget["wall_clock_ms"], 30_000)}

    def _align(self, proof_id: str, claim_id: str, source_ref: str, operation_id: str) -> dict:
        if claim_id != f"{proof_id}/c-1" or self.view.node(claim_id) is None:
            raise ContractError("claim is not the registered literal target")
        if not self.artifacts.exists(source_ref):
            raise ContractError("Lean source artifact is missing")
        try:
            source = self.artifacts.get(source_ref).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("Lean source is not UTF-8") from exc
        declaration_id = f"{proof_id}/fd-1"
        alignment_id = f"{proof_id}/al-{source_ref.split(':')[1][:16]}"
        previous = self.view.node(declaration_id)
        if previous and previous.fields.get("source_hash") != source_ref:
            raise ContractError("a different declaration is already registered")
        if not previous:
            lease_id = f"{proof_id}/align-init-{uuid.uuid4().hex}"
            token = self.store.acquire_lease(proof_id, lease_id, actor=ACTOR, worker_class="coordinator", ttl_seconds=30)
            try:
                revision = self.store.head(proof_id)[0]
                proposal = Proposal(proof_id, ACTOR, "coordinator", ops=(
                    UpsertNode("FormalDeclaration", declaration_id,
                        {"status": "draft", "source_hash": source_ref, "lean_source_artifact": source_ref}),
                ), base_revision=revision, lease_id=lease_id, fencing_token=token)
                committed = self.gate.commit(proposal)
                if not committed.accepted:
                    raise ContractError("gate rejected declaration: " + ", ".join(r.reason.value for r in committed.rejections))
                self.catch_up(proof_id)
            finally:
                self.store.release_lease(proof_id, lease_id, fencing_token=token)
        existing = self.view.node(alignment_id)
        if existing:
            return {**self._status(proof_id), "alignment_id": alignment_id,
                "alignment_verdict": existing.fields["verdict"],
                "alignment_lifecycle": existing.fields["lifecycle"],
                "report_artifact": existing.fields["report_artifact"]}
        request = AlignmentReviewRequest(proof_id=proof_id, claim_id=claim_id,
            declaration_id=declaration_id,
            informal_statement=self.view.node(claim_id).fields["claim_text"],
            formal_statement=source, source_hash=source_ref)
        heuristic = RuleBasedAlignmentReviewer(reviewer_id="reference-heuristic").review(request)
        # A lexical heuristic cannot establish quantifier/domain equivalence.
        result = AlignmentReviewResult(verdict=AlignmentVerdict.AMBIGUOUS,
            lifecycle=AlignmentLifecycle.REVIEW_NEEDED,
            criteria=AlignmentCriteria(relation="reformulation",
                quantifier_correspondence="unreviewed", domain_correspondence="unreviewed",
                selected_definitions_match=False, implication_to_target_explicit=False,
                notes=f"heuristic suggested {heuristic.verdict.value}; competent semantic review required"),
            reviewer="reference-heuristic", reasoning="No formal alignment authority in reference mode")
        report = json.dumps({"verdict": result.verdict.value,
            "lifecycle": result.lifecycle.value, "criteria": result.criteria.to_dict(),
            "claim_id": claim_id, "declaration_id": declaration_id,
            "source_hash": source_ref}, sort_keys=True).encode()
        report_ref = self.artifacts.put(report)
        lease_id = f"{proof_id}/align-{uuid.uuid4().hex}"
        row = self.store.issue_lease(proof_id, lease_id, actor="reference-heuristic",
            worker_class="alignment-reviewer", selected_move_id=declaration_id,
            base_revision=self.store.head(proof_id)[0], ttl_seconds=30)
        try:
            base = build_alignment_proposal(proof_id=proof_id, alignment_id=alignment_id,
                request=request, result=result, actor=row.actor,
                base_revision=row.base_revision, lease_id=row.lease_id,
                fencing_token=row.fencing_token, selected_move_id=declaration_id)
            # The report pointer is immutable evidence included with the node.
            first = base.ops[0]
            enriched = UpsertNode(first.label, first.node_id, {**first.fields, "report_artifact": report_ref})
            proposal = Proposal(proof_id, row.actor, row.worker_class,
                ops=(enriched, *base.ops[1:]), base_revision=row.base_revision,
                lease_id=row.lease_id, fencing_token=row.fencing_token,
                selected_move_id=declaration_id)
            committed = self.gate.commit(proposal)
            if not committed.accepted:
                raise ContractError("gate rejected alignment: " + ", ".join(r.reason.value for r in committed.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "alignment_id": alignment_id,
                "alignment_verdict": result.verdict.value,
                "alignment_lifecycle": result.lifecycle.value,
                "report_artifact": report_ref}
        finally:
            self.store.release_lease(proof_id, lease_id, fencing_token=row.fencing_token)

    def _bridge(self, proof_id: str, obstruction_id: str, operation_id: str) -> dict:
        obstruction = self.view.node(obstruction_id)
        if obstruction is None or obstruction.label != "Obstruction" or not obstruction_id.startswith(proof_id + "/"):
            raise ContractError("unknown obstruction")
        if validate_schema("obstruction", dict(obstruction.fields)):
            raise ContractError("obstruction does not satisfy the typed schema")
        run_id = obstruction.fields["formal_run_id"]
        run = self.view.node(run_id)
        if run is None or run.label != "FormalRun" or run.fields.get("status") != "stagnated":
            raise ContractError("obstruction is not from a stagnated formal run")
        if not any(edge.dst_id == obstruction_id for edge in self.view.edges_from(run_id, "RAISED_OBSTRUCTION")):
            raise ContractError("obstruction is not linked to its formal run")
        hypothesis_id = f"{proof_id}/hyp-{operation_id.replace('-', '')}"
        existing = self.view.node(hypothesis_id)
        if existing:
            return {**self._status(proof_id), "hypothesis_id": hypothesis_id,
                "component_mode": "scripted-reference", "hypothesis_status": "speculative",
                "result_ref": existing.fields["result_artifact"]}
        state_ids = obstruction.fields.get("formal_state_ids", [])
        diagnostic_refs = obstruction.fields.get("diagnostic_artifacts", [])
        if not isinstance(state_ids, list) or not state_ids:
            raise ContractError("obstruction has no exact formal states")
        if not isinstance(diagnostic_refs, list) or not diagnostic_refs:
            raise ContractError("obstruction has no exact diagnostic artifacts")
        if any((node := self.view.node(state_id)) is None or node.label != "FormalState" for state_id in state_ids):
            raise ContractError("obstruction references an unknown formal state")
        linked_states = {edge.dst_id for edge in self.view.edges_from(obstruction_id, "AT_STATE")}
        if set(state_ids) != linked_states:
            raise ContractError("obstruction state IDs differ from committed links")
        if any(not self.artifacts.exists(ref) for ref in diagnostic_refs):
            raise ContractError("obstruction diagnostic bytes are missing")
        sources = [obstruction_id, *state_ids]
        packet = json.dumps({"proof_id": proof_id, "obstruction_id": obstruction_id,
            "source_ids": sources, "diagnostic_artifacts": diagnostic_refs,
            "snapshot_revision": self.store.head(proof_id)[0],
            "theorem_hash": self.view.node(f"{proof_id}/proof").fields["target_artifact"]},
            sort_keys=True).encode()
        packet_ref = self.artifacts.put(packet)
        # Deterministic reference worker: a research suggestion, never evidence.
        result_bytes = json.dumps({"mode": "scripted-reference-lemma-inventor",
            "statement": f"Investigate a bridge for {obstruction_id}"}, sort_keys=True).encode()
        result_ref = self.artifacts.put(result_bytes)
        payload = {"schema_version": 1, "hypothesis_id": hypothesis_id,
            "statement": f"Investigate a bridge for {obstruction_id}",
            "source_ids": sources, "prerequisites": sources[1:],
            "consequences": [f"Address {obstruction_id} if independently verified"],
            "novelty": "scripted bridge strategy distinct from direct tactic search",
            "support": "unverified research proposal only", "cost": "bounded reference step",
            "risk": "may be false or irrelevant",
            "falsification_test": "independent Lean replay against the exact target",
            "expiry_review": "review before any promotion", "packet_artifact": packet_ref,
            "result_artifact": result_ref}
        errors = validate_schema("speculative-hypothesis", payload)
        if errors:
            raise ContractError("bridge candidate schema invalid: " + "; ".join(errors))
        row = self.store.issue_lease(proof_id, f"{proof_id}/bridge-{uuid.uuid4().hex}",
            actor="reference-lemma-inventor", worker_class="llm-research",
            selected_move_id=obstruction_id, base_revision=self.store.head(proof_id)[0],
            ttl_seconds=30)
        try:
            proposal = Proposal(proof_id, row.actor, row.worker_class,
                ops=(UpsertNode("SpeculativeHypothesis", hypothesis_id, payload),),
                base_revision=row.base_revision, lease_id=row.lease_id,
                fencing_token=row.fencing_token, selected_move_id=obstruction_id)
            committed = self.gate.commit(proposal)
            if not committed.accepted:
                raise ContractError("gate rejected speculative bridge: " + ", ".join(r.reason.value for r in committed.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "hypothesis_id": hypothesis_id,
                "hypothesis_status": "speculative", "component_mode": "scripted-reference",
                "packet_ref": packet_ref, "result_ref": result_ref}
        finally:
            self.store.release_lease(proof_id, row.lease_id, fencing_token=row.fencing_token)

    def _create(self, raw_spec: str, operation_id: str) -> dict:
        try:
            spec = json.loads(raw_spec)
        except json.JSONDecodeError as exc:
            raise ContractError("spec is not JSON") from exc
        if not isinstance(spec, dict) or set(spec) != {"proof_id", "literal_target", "title"}:
            raise ContractError("spec requires only proof_id, literal_target and title")
        proof_id, target = spec["proof_id"], spec["literal_target"]
        if not isinstance(proof_id, str) or not _ID.fullmatch(proof_id):
            raise ContractError("invalid proof id")
        if not isinstance(target, str) or not target.strip() or len(target.encode()) > 16_384:
            raise ContractError("invalid literal target")
        if not isinstance(spec["title"], str) or not spec["title"].strip():
            raise ContractError("invalid title")
        claim_id = f"{proof_id}/c-1"
        if validate_schema("proof-spec", {"proof_id": proof_id, "actor": ACTOR, "title": spec["title"], "target_claim_ids": [claim_id]}):
            raise ContractError("proof spec failed schema validation")
        self.catch_up(proof_id)
        revision, head = self.store.head(proof_id)
        if revision:
            node = self.view.node(f"{proof_id}/proof")
            if node and node.fields.get("operation_id") == operation_id:
                return self._status(proof_id)
            raise ContractError("proof id already exists")
        target_ref = self.artifacts.put(target.encode("utf-8"))
        lease_id = f"{proof_id}/init-{uuid.uuid4().hex}"
        token = self.store.acquire_lease(proof_id, lease_id, actor=ACTOR, worker_class="coordinator", ttl_seconds=30)
        try:
            proposal = Proposal(proof_id=proof_id, actor=ACTOR, worker_class="coordinator",
                ops=(UpsertNode("Proof", f"{proof_id}/proof", {"title": spec["title"], "actor": ACTOR, "operation_id": operation_id, "target_artifact": target_ref}),
                     UpsertNode("Claim", claim_id, {"status": "provisional", "claim_text": target, "literal_target_hash": target_ref}),
                     AddEdge("HAS_TARGET", f"{proof_id}/proof", claim_id, f"{proof_id}/target-1")),
                base_revision=revision, lease_id=lease_id, fencing_token=token)
            result = self.gate.commit(proposal)
            if not result.accepted:
                raise ContractError("gate rejected bootstrap: " + ", ".join(r.reason.value for r in result.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "artifact_pointers": [target_ref], "event_hash": result.event_hash}
        finally:
            self.store.release_lease(proof_id, lease_id, fencing_token=token)

    def _status(self, proof_id: str) -> dict:
        self.catch_up(proof_id)
        revision, event_hash = self.store.head(proof_id)
        claim = self.view.node(f"{proof_id}/c-1")
        status = claim.fields.get("status") if claim else None
        return {"proof_id": proof_id, "revision": revision, "event_hash": event_hash,
            "claim_status": status, "formal_verified": status == "lean-verified",
            "projection_lag": revision - self.view.snapshot(proof_id)[0],
            "artifact_pointers": [claim.fields["literal_target_hash"]] if claim else [],
            "summary": f"claim status: {status}; independent Lean verification " +
                       ("recorded" if status == "lean-verified" else "not established")}

    def _expect_cycle_revision(self, proof_id: str, expected_revision: int | None):
        if expected_revision is not None and self.store.head(proof_id)[0] != expected_revision:
            raise StalePrecondition("proof revision changed before selected-cycle execution")

    def _cycle(self, proof_id: str, operation_id: str, *, category: str | None = None,
               record_progress: bool = True, expected_revision: int | None = None) -> dict:
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("expected_revision must be a nonnegative integer")
        attempt_id = f"{proof_id}/at-{operation_id.replace('-', '')}"
        self.catch_up(proof_id)
        existing = self.view.node(attempt_id)
        if existing is not None:
            # A crash after the gate append but before the receiver's terminal
            # update must recover the original work, not reject its advanced head.
            if expected_revision is not None:
                packet = json.loads(self.artifacts.get(existing.fields["packet_artifact"]))
                if packet["snapshot_revision"] != expected_revision or packet["proof_id"] != proof_id:
                    raise StalePrecondition("committed attempt belongs to another execution revision")
            return {**self._status(proof_id), "cycle_disposition": "pending-review",
                "component_mode": "scripted-reference", "packet_ref": existing.fields["packet_artifact"],
                "result_ref": existing.fields["result_artifact"], "attempt_id": attempt_id,
                "selected_move_id": existing.fields["selected_move_id"]}
        self._expect_cycle_revision(proof_id, expected_revision)
        scheduler = GlobalScheduler(self.view, self.store, worker_actors={
            "critic": "omega-critic", "formal-atp": "omega-formal-adapter",
            "llm-research": "omega-research-dispatch"})
        try:
            lease = scheduler.lease_best_global_move(proof_id, ttl_seconds=15, category=category,
                                                     expected_revision=expected_revision)
        except ConcurrencyError as exc:
            if expected_revision is not None and exc.reason == Reason.STALE_BASE_REVISION:
                raise StalePrecondition("proof revision changed before selected-cycle dispatch") from exc
            raise
        if lease is None:
            self._expect_cycle_revision(proof_id, expected_revision)
            frontier = [item for item in scheduler.build_frontier(proof_id)
                        if category is None or item.category == category]
            disposition = "waiting-on-lease" if frontier and self.store.active_leases(proof_id) else "idle"
            return {**self._status(proof_id), "cycle_disposition": disposition,
                "component_mode": "scripted-reference",
                "viable_move_ids": [candidate.move_id for candidate in frontier]}
        if record_progress:
            self.jobs.update(operation_id, {"operation_id": operation_id, "proof_id": proof_id, "phase": "leased", "selected_move_id": lease.selected_move_id})
        try:
            self._expect_cycle_revision(proof_id, expected_revision)
            packet = compile_packet(proof_id, lease, self.view, self.artifacts)
            packet_ref = self.artifacts.put(packet)
            verify_packet(self.artifacts.get(packet_ref), lease=lease,
                view=self.view, artifacts=self.artifacts)
            if record_progress:
                self.jobs.update(operation_id, {"operation_id": operation_id, "proof_id": proof_id, "phase": "request-saved", "packet_ref": packet_ref})
            if lease.worker_class != "critic":
                blocker = ("FORMAL_BACKEND_UNAVAILABLE" if lease.worker_class == "formal-atp"
                    else "THREADKEEPER_UNAVAILABLE")
                return {**self._status(proof_id), "cycle_disposition": "blocked",
                    "component_mode": "blocked", "error_code": blocker,
                    "packet_ref": packet_ref, "selected_move_id": lease.selected_move_id,
                    "required_worker_class": lease.worker_class}
            pending_reviews = [edge.src_id for edge in self.view.edges_to(
                lease.selected_move_id, "REVIEWS_CLAIM")
                if (attempt := self.view.node(edge.src_id)) is not None
                and attempt.fields.get("status") == "pending"]
            if pending_reviews:
                return {**self._status(proof_id), "cycle_disposition": "waiting-on-review",
                    "component_mode": "scripted-reference", "pending_attempt_ids": pending_reviews,
                    "selected_move_id": lease.selected_move_id}
            # The reference worker runs in a bounded child with a minimal
            # environment. Exact bytes are saved before parsing or gate use.
            self._expect_cycle_revision(proof_id, expected_revision)
            try:
                worker = subprocess.run(self.critic_command, input=packet,
                    capture_output=True, timeout=self.critic_timeout_s,
                    env={"PATH": os.environ.get("PATH", "")}, check=False)
                result_bytes, error_bytes = worker.stdout, worker.stderr
                return_code = worker.returncode
            except subprocess.TimeoutExpired as exc:
                result_ref = self.artifacts.put(exc.stdout or b"")
                error_ref = self.artifacts.put(exc.stderr or b"")
                return {**self._status(proof_id), "cycle_disposition": "blocked",
                    "component_mode": "scripted-reference", "error_code": "EXECUTOR_TIMEOUT",
                    "packet_ref": packet_ref, "result_ref": result_ref, "diagnostic_ref": error_ref,
                    "selected_move_id": lease.selected_move_id}
            result_ref = self.artifacts.put(result_bytes)
            diagnostic_ref = self.artifacts.put(error_bytes)
            if record_progress:
                self.jobs.update(operation_id, {"operation_id": operation_id, "proof_id": proof_id,
                    "phase": "result-saved", "packet_ref": packet_ref, "result_ref": result_ref,
                    "diagnostic_ref": diagnostic_ref})
            try:
                parsed = json.loads(result_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if return_code != 0 or parsed != {"status": "pending", "mode": "scripted-reference-critic"}:
                return {**self._status(proof_id), "cycle_disposition": "blocked",
                    "component_mode": "scripted-reference", "error_code": "MALFORMED_WORKER_OUTPUT",
                    "packet_ref": packet_ref, "result_ref": result_ref, "diagnostic_ref": diagnostic_ref,
                    "selected_move_id": lease.selected_move_id}
            proposal = Proposal(proof_id, lease.actor, lease.worker_class,
                ops=(UpsertNode("Attempt", attempt_id, {"status": "pending", "worker_class": "critic",
                    "actor": lease.actor, "operation_id": operation_id,
                    "packet_artifact": packet_ref, "result_artifact": result_ref,
                    "selected_move_id": lease.selected_move_id}),
                    AddEdge("REVIEWS_CLAIM", attempt_id, lease.selected_move_id,
                        f"{attempt_id}/reviews")),
                base_revision=lease.base_revision, lease_id=lease.lease_id,
                fencing_token=lease.fencing_token, selected_move_id=lease.selected_move_id)
            committed = self.gate.commit(proposal)
            if not committed.accepted:
                raise ContractError("gate rejected scripted attempt: " + ", ".join(r.reason.value for r in committed.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "cycle_disposition": "pending-review",
                "component_mode": "scripted-reference", "packet_ref": packet_ref,
                "result_ref": result_ref, "attempt_id": attempt_id,
                "selected_move_id": lease.selected_move_id}
        finally:
            scheduler.release(proof_id, lease.lease_id, fencing_token=lease.fencing_token)

    def queue_research_moves(self, action, *, expected_revision: int) -> dict:
        """Translate bounded coordinator guidance into gate-checked speculative moves."""
        proof_id = action.proof_id
        self.catch_up(proof_id)
        if self.store.head(proof_id)[0] == 0:
            raise ContractError("unknown proof")
        guidance = {"proof_id": action.proof_id, "intent_key": action.intent_key,
                    "moves": [{"move_kind": move.move_kind, "detail": move.detail,
                               "target_id": move.target_id} for move in action.moves]}
        digest = hashlib.sha256(json.dumps(guidance, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        suffix = hashlib.sha256(action.intent_key.encode()).hexdigest()[:24]
        state_id = f"{proof_id}/rs-coordination-{suffix}"
        move_ids = [f"{proof_id}/rm-coordination-{suffix}-{index}" for index in range(len(action.moves))]
        existing = self.view.node(state_id)
        if existing is not None:
            if existing.fields.get("guidance_digest") != digest:
                raise ContractError("coordination intent key was reused with different guidance")
            if any(self.view.node(move_id) is None for move_id in move_ids):
                raise ContractError("committed guidance is incomplete")
            return {**self._status(proof_id), "research_state_id": state_id,
                    "research_move_ids": move_ids, "component_mode": "reference-scripted"}
        revision = self.store.head(proof_id)[0]
        if revision != expected_revision:
            raise ContractError("coordination revision changed before commit")
        ops = [UpsertNode("ResearchState", state_id,
                          {"status": "open", "guidance_digest": digest,
                           "coordination_intent_key": action.intent_key})]
        for index, move in enumerate(action.moves):
            if move.target_id is not None:
                target = self.view.node(move.target_id)
                if not move.target_id.startswith(proof_id + "/") or target is None:
                    raise ContractError("guidance target does not belong to this proof")
            else:
                target = None
            move_id = move_ids[index]
            ops.append(UpsertNode("ResearchMove", move_id,
                {"status": "queued", "move_kind": move.move_kind,
                 "strategy": move.detail, "target_id": move.target_id,
                 "guidance_digest": digest}))
            ops.append(AddEdge("PROPOSES", state_id, move_id,
                f"{proof_id}/coord-proposes-{suffix}-{index}"))
            if target is not None and target.label == "Claim":
                ops.append(AddEdge("MOVE_TARGETS", move_id, move.target_id,
                    f"{proof_id}/coord-targets-{suffix}-{index}"))
        lease_id = f"{proof_id}/coordination-{uuid.uuid4().hex}"
        token = self.store.acquire_lease(proof_id, lease_id, actor=ACTOR,
            worker_class="coordinator", ttl_seconds=30)
        try:
            if self.store.head(proof_id)[0] != expected_revision:
                raise ContractError("coordination revision changed before commit")
            proposal = Proposal(proof_id, ACTOR, "coordinator", ops=tuple(ops),
                base_revision=expected_revision, lease_id=lease_id, fencing_token=token)
            committed = self.gate.commit(proposal)
            if not committed.accepted:
                raise ContractError("gate rejected guidance: " + ", ".join(
                    item.reason.value for item in committed.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "research_state_id": state_id,
                    "research_move_ids": move_ids, "component_mode": "reference-scripted"}
        finally:
            self.store.release_lease(proof_id, lease_id, fencing_token=token)

    def _audit(self, proof_id: str) -> dict:
        count = self.store.verify_chain(proof_id)
        status = self._status(proof_id)
        pointers = self._artifact_pointers(proof_id)
        missing = [ref for ref in pointers if not self.artifacts.exists(ref)]
        return {**status, "chain_events": count, "artifact_pointers": pointers,
            "missing_artifacts": missing,
            "audit_status": "ok" if not missing else "failed"}

    def _artifact_pointers(self, proof_id: str) -> list[str]:
        return sorted(referenced_artifacts(self.store.read_events(proof_id)))

    def _export(self, proof_id: str, destination: str) -> dict:
        if destination != "default":
            raise ContractError("unknown export alias")
        audit = self._audit(proof_id)
        if audit["audit_status"] != "ok":
            raise ContractError("audit failed")
        payload = {"proof_id": proof_id, "head": self.store.head(proof_id),
            "chain": self.store.read_chain(proof_id),
            "events": self.store.read_events(proof_id),
            "artifacts": {ref: self.artifacts.get(ref).hex() for ref in audit["artifact_pointers"]}}
        content = json.dumps(payload, sort_keys=True).encode()
        check_export(content)
        pointer = self.artifacts.put(content)
        return {**audit, "export_artifact": pointer}
