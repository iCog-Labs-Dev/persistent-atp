"""Trusted operation facade: Omega never sees a Proposal or lease token."""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import uuid
import threading
from pathlib import Path

from commit_gate.gate import CommitGate
from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore, LeaseCandidate
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
from .specification import SPEC_FIELDS, parse_json, validate_spec
from .candidates import formalization_spec

_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
ACTOR = "omega-coordinator"


class OperationService:
    def __init__(self, root: str | Path, *, workspace: str = "reference", artifacts=None,
                 critic_command: tuple[str, ...] | None = None, critic_timeout_s: float = 2.0,
                 profile: str = "reference", artifact_db_url: str | None = None, formal_config=None, intake_config=None):
        if profile not in {"reference", "persistence", "production"}:
            raise ValueError("unknown receiver profile")
        if formal_config is not None:
            from .formal_runtime import FormalWorkflow, FormalProviderConfig
            if not isinstance(formal_config, FormalProviderConfig):
                raise ValueError('formal_config must be a trusted FormalProviderConfig')
            if profile == 'production' and formal_config.mode != 'real':
                raise ValueError('production cannot use a scripted formal provider')
        if intake_config is not None:
            from .intake_runtime import IntakeConfig
            if not isinstance(intake_config, IntakeConfig) or formal_config is None:
                raise ValueError('intake requires a trusted IntakeConfig and configured formal provider')
            intake_config.llm()  # Fail at startup if the selected credentials are absent.
        self.profile = profile
        self.lock = threading.RLock()
        self._executing = {}
        self.receiver_mode = {"reference": "reference-scripted", "persistence": "persistence-scripted",
                              "production": "persistent-storage"}[profile]
        self._owns_artifacts = artifacts is None
        if profile != "reference":
            from artifacts.store import ArtifactStore
            from mork.backend.ffi import MorkSpace
            from mork.backend.view import MorkView
            self.view = MorkView(MorkSpace())
            if artifacts is not None and not isinstance(artifacts, ArtifactStore):
                raise ValueError("persistent profiles require the configured PostgreSQL artifact store")
            artifacts = artifacts if artifacts is not None else ArtifactStore(artifact_db_url)
        else:
            self.view = MemoryView()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.store = JournalStore(str(self.root / "journal.sqlite3"), allow_cross_thread=True)
        self.artifacts = artifacts or ReferenceArtifactStore(self.root / "artifacts")
        self.gate = CommitGate(self.view, self.store, artifacts=self.artifacts)
        self.jobs = JobLedger(self.root / "operations.sqlite3")
        from .bounded_gate import BoundedGate
        self.gate = BoundedGate(self.gate,self)
        if critic_timeout_s <= 0 or critic_timeout_s > 10:
            raise ValueError("reference critic timeout must be within (0, 10] seconds")
        # The child gets a minimal environment, without pytest's source path.
        # This stdlib-only script must also run from an uninstalled checkout.
        self.critic_command = critic_command or (sys.executable,
            str(Path(__file__).resolve().with_name("reference_critic.py")))
        self.critic_timeout_s = critic_timeout_s
        for proof_id in self.store.proof_ids():
            self.catch_up(proof_id)
        self.formal = self.cycles = self.intake = self.research = self.refinement = self.bounded = None
        if formal_config is not None:
            self.formal = FormalWorkflow(self, formal_config)
            from .cycle_runtime import CycleWorkflow
            self.cycles = CycleWorkflow(self)
        if intake_config is not None:
            from .intake_runtime import IntakeWorkflow
            from .research_runtime import ResearchWorkflow
            self.intake = IntakeWorkflow(self, intake_config)
            self.research = ResearchWorkflow(self, intake_config)
            from .refinement_runtime import ParentRefinementWorkflow
            self.refinement = ParentRefinementWorkflow(self)
            from .bounded_runtime import BoundedRefinementWorkflow
            self.bounded = BoundedRefinementWorkflow(self)

    def catch_up(self, proof_id: str) -> None:
        if self.gate.projects:
            self.gate.catch_up(proof_id)
            return
        while self.view.snapshot(proof_id)[0] < self.store.head(proof_id)[0]:
            self.view.apply_journal_event(self.store, proof_id, self.view.snapshot(proof_id)[0] + 1)

    def submit(self, raw: object) -> dict:
        request = validate_request(raw)
        if self.profile == "production" and request["operation"] not in {
                "proof-create", "proof-status", "proof-audit", "proof-export",
                *({"proof-cycle"} if self.cycles is not None else set()),
                *({"lemma-bridge"} if self.research is not None else set()),
                *({"hypothesis-formalize", "hypothesis-revise", "parent-refine",'bounded-refine'} if self.intake is not None else set())}:
            raise ContractError("production executor is unavailable for this action")
        if request["workspace"] != self.workspace:
            raise ContractError("workspace is not registered")
        with self.lock:
            if request["operation"] in {"proof-cycle", "parent-refine"}:
                active = self.jobs.active_cycle(request["arguments"]["proof"], self.workspace,
                    excluding_request_id=request["request_id"])
                if active:
                    return active
            record, created = self.jobs.submit(request)
            if not created and record["phase"] in ("finished", "failed"):
                return record
            operation_id = record["operation_id"]
            # Wait only on another execution of this operation, never while
            # holding the shared journal/projection lock. Count waiters so the
            # entry can be removed safely when the last caller leaves.
            execution = self._executing.setdefault(operation_id, [threading.Lock(), 0])
            execution[1] += 1
        try:
            with execution[0]:
                with self.lock:
                    latest = self.jobs.get(operation_id, self.workspace)
                    if latest["phase"] in ("finished", "failed"):
                        return latest
                return self._run_admitted(request, operation_id)
        finally:
            with self.lock:
                execution[1] -= 1
                if execution[1] == 0:
                    del self._executing[operation_id]

    def _run_admitted(self, request, operation_id):
        # These workflows own short locks around their durable stages. Their
        # provider calls must run without an enclosing service lock.
        staged_workflow = (request["operation"] in {"hypothesis-formalize", "hypothesis-revise", "parent-refine",'bounded-refine'}
            or request["operation"] == "lemma-bridge" and self.research is not None
            or request["operation"] == "proof-cycle" and self.cycles is not None)
        try:
            if staged_workflow:
                result = self._execute(request, operation_id)
            else:
                with self.lock:
                    result = self._execute(request, operation_id)
            response = {"operation_id": operation_id, "phase": "finished", "transport_status": "ok", **result}
        except (ContractError, ValueError, KeyError) as exc:
            response = {"operation_id": operation_id, "phase": "failed", "transport_status": "error", "error_code": type(exc).__name__, "summary": str(exc)[:500]}
        with self.lock:
            self.jobs.update(operation_id, response)
        return response

    def close(self):
        self.jobs.conn.close()
        self.store.close()
        if self._owns_artifacts and hasattr(self.artifacts, "close"):
            self.artifacts.close()

    def _execute(self, request: dict, operation_id: str) -> dict:
        operation = request["operation"]
        args = request["arguments"]
        if operation == "proof-create":
            return self._create(args["spec"], operation_id)
        proof_id = args["proof"]
        if not _ID.fullmatch(proof_id):
            raise ContractError("invalid proof id")
        with self.lock:
            if self.store.head(proof_id)[0] == 0:
                raise ContractError("unknown proof")
            self.catch_up(proof_id)
        if operation == "proof-status":
            return self._status(proof_id)
        if operation == "proof-audit":
            return self._audit(proof_id)
        if operation == "proof-cycle":
            if self.cycles is not None:
                return self.cycles.run(operation_id, proof_id)
            return self._cycle(proof_id, operation_id)
        if operation == 'parent-refine':
            return self.refinement.run(operation_id, proof_id, args['previous_declaration'], args['lemma'],
                self._parse_budget(args['budget']), expected_revision=int(args['expected_revision']),
                expected_theorem_hash=args['expected_theorem_hash'])
        if operation=='bounded-refine':
            from datetime import datetime
            return self.bounded.run(operation_id,proof_id,json.loads(args['policy']),
                expected_revision=int(args['expected_revision']),theorem_hash=args['expected_theorem_hash'],
                deadline=datetime.fromisoformat(args['deadline']).timestamp())
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
        if operation == "hypothesis-formalize":
            return self._formalize_hypothesis(proof_id, args["hypothesis"], operation_id,
                expected_revision=args["expected_revision"])
        if operation == "hypothesis-revise":
            return self._formalize_hypothesis(proof_id, args["hypothesis"], operation_id,
                expected_revision=args["expected_revision"], spec_artifact=args["spec_artifact"],
                previous_claim_id=args["previous_claim"] or None)
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
        if self.research is not None:
            return self.research.run(proof_id, obstruction_id, operation_id)
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

    def _formalize_hypothesis(self, proof_id: str, hypothesis_id: str, operation_id: str,
                              *, expected_revision: str, spec_artifact=None, previous_claim_id=None,
                              cancelled=lambda: False) -> dict:
        """Explicitly admit one speculative candidate, then reuse guarded intake."""
        if cancelled():
            return {'proof_id':proof_id,'lifecycle':'cancelled','formalization_ready':False}
        if self.intake is None:
            raise ContractError("hypothesis formalization requires configured formalization and review workers")
        if not expected_revision.isascii() or not expected_revision.isdecimal():
            raise ContractError("expected_revision must be a nonnegative decimal revision")
        expected = int(expected_revision)
        if not hypothesis_id.startswith(proof_id + "/"):
            raise ContractError("hypothesis is outside the requested proof")

        with self.lock:
            self.catch_up(proof_id)
            hypothesis = self.view.node(hypothesis_id)
            if hypothesis is None or hypothesis.label != "SpeculativeHypothesis":
                raise ContractError("unknown speculative hypothesis")
            errors = validate_schema("speculative-hypothesis", dict(hypothesis.fields))
            if errors:
                raise ContractError("hypothesis does not satisfy its typed schema")

            source_claims = [node_id for node_id in hypothesis.fields["source_ids"]
                if (node := self.view.node(node_id)) is not None and node.label == "Claim"]
            obstructions = [node_id for node_id in hypothesis.fields["source_ids"]
                if (node := self.view.node(node_id)) is not None and node.label == "Obstruction"]
            if len(source_claims) != 1 or len(obstructions) != 1:
                raise ContractError("hypothesis must name exactly one source theorem and obstruction")
            parent_claim_id, obstruction_id = source_claims[0], obstructions[0]
            parent_claim = self.view.node(parent_claim_id)
            obstruction = self.view.node(obstruction_id)
            if (parent_claim is None or parent_claim.fields.get("status") in
                    {"refuted", "retracted", "stale", "tainted", "lean-verified"}):
                raise ContractError("source theorem is inactive or already verified")
            if (obstruction is None or obstruction.fields.get("formal_run_id") is None
                    or not any(edge.dst_id == obstruction_id
                        for edge in self.view.edges_from(obstruction.fields["formal_run_id"], "RAISED_OBSTRUCTION"))):
                raise ContractError("hypothesis obstruction provenance is no longer committed")

            input_digest = content_hash({"hypothesis_id": hypothesis_id, "hypothesis": dict(hypothesis.fields),
                "parent_claim_id": parent_claim_id, "parent_claim": dict(parent_claim.fields),
                "obstruction_id": obstruction_id, "obstruction": dict(obstruction.fields)})
            lemma_spec = None
            if spec_artifact is not None:
                try:
                    lemma_spec = validate_spec(parse_json(self.artifacts.get(spec_artifact).decode('utf-8')))
                except (ValueError, KeyError) as exc:
                    raise ContractError('revised candidate requires an available complete theorem specification') from exc
                if lemma_spec['targets'] != [lemma_spec['theorem']]:
                    raise ContractError('revised candidate must specify exactly one literal target')
                input_digest = content_hash({'source': input_digest, 'spec_artifact': spec_artifact,
                    'previous_claim_id': previous_claim_id})

            promotions = self.view.edges_from(hypothesis_id, "PROMOTED_TO")
            promoted = [self.view.node(edge.dst_id) for edge in promotions]
            for claim in promoted:
                if (claim is None or claim.label != "Claim"
                        or claim.fields.get("source_hypothesis_id") != hypothesis_id
                        or claim.fields.get("parent_claim_id") != parent_claim_id):
                    raise ContractError("existing hypothesis promotion differs from its source")
            matches = [claim for claim in promoted if claim.fields.get('hypothesis_input_digest') == input_digest]
            if not matches and any(claim.fields.get('hypothesis_operation_id') == operation_id for claim in promoted):
                raise ContractError('hypothesis admission inputs changed; reconcile the original operation')
            if len(matches) > 1:
                raise ContractError('hypothesis has conflicting promotion links')
            if matches:
                claim = matches[0]
                claim_id = claim.node_id
                if (claim.fields.get("hypothesis_operation_id") != operation_id
                        or claim.fields.get("hypothesis_admission_revision") != expected):
                    raise ContractError("hypothesis admission belongs to another operation or requires legacy reconciliation")
                if claim.fields.get("hypothesis_input_digest") != input_digest:
                    raise ContractError("hypothesis admission inputs changed; reconcile the original operation")
                admission = self.intake.validate_hypothesis_history(operation_id, proof_id, claim)
                self.store.release_lease(proof_id, admission['lease_id'], fencing_token=admission['fencing_token'])
            else:
                if self.store.head(proof_id)[0] != expected:
                    raise ContractError("proof changed before hypothesis admission; review the current candidate")
                if promoted:
                    previous = next((claim for claim in promoted if claim.node_id == previous_claim_id), None)
                    if (spec_artifact is None or previous is None
                            or previous.fields.get('status') != 'provisional'):
                        raise ContractError('revision must identify a previous provisional claim from this hypothesis')
                    if any(claim.fields.get('previous_claim_id') == previous_claim_id for claim in promoted):
                        raise ContractError('previous claim already has a revision; review the latest attempt')
                    prior = self.jobs.get(previous.fields.get('hypothesis_operation_id'), self.workspace)
                    if prior is None or prior['phase'] not in {'finished', 'failed'}:
                        raise ContractError('previous formalization is still active or requires legacy reconciliation')
                    if prior['phase'] == 'finished' and prior.get('formalization_ready') is not False:
                        raise ContractError('previous formalization is already ready; correction requires a refused or failed attempt')
                elif previous_claim_id is not None:
                    raise ContractError('previous claim does not belong to this hypothesis')
                if lemma_spec is None:
                    kind = hypothesis.fields.get('candidate_kind')
                    code = ('RESEARCH_MOVE_NOT_A_CLAIM' if kind == 'research-move'
                            else 'CANDIDATE_SPECIFICATION_REQUIRED')
                    reason = 'Select a mathematical claim with a complete specification, or explicitly use hypothesis-revise.'
                    if kind == 'mathematical-claim':
                        try:
                            lemma_spec = formalization_spec(dict(hypothesis.fields))
                        except ContractError as exc:
                            reason = str(exc)
                    if lemma_spec is None:
                        return {**self._status(proof_id), 'hypothesis_id': hypothesis_id,
                            'formalization_ready': False, 'formalization_disposition': 'needs-clarification',
                            'error_code': code, 'clarification_reasons': [reason],
                            'component_mode': 'hypothesis-formalization'}
                statement = lemma_spec['theorem']
                statement_ref = self.artifacts.put(statement.encode("utf-8"))
                spec_ref = self.artifacts.put(canonical_json(lemma_spec))
                claim_serial = int(content_hash(["hypothesis-claim", hypothesis_id] if spec_artifact is None
                    else ['hypothesis-revision', hypothesis_id, spec_artifact, previous_claim_id])[7:], 16)
                claim_id = f"{proof_id}/c-{claim_serial}"
                fields = {
                    "status": "provisional",
                    "claim_text": statement,
                    "literal_target_hash": statement_ref,
                    "theorem_spec_artifact": spec_ref,
                    "assumptions": lemma_spec["assumptions"],
                    "source_hypothesis_id": hypothesis_id,
                    "parent_claim_id": parent_claim_id,
                    "obstruction_id": obstruction_id,
                    "research_context_artifact": hypothesis.fields["packet_artifact"],
                    "research_result_artifact": hypothesis.fields["result_artifact"],
                    "hypothesis_operation_id": operation_id,
                    "hypothesis_admission_revision": expected,
                    "hypothesis_input_digest": input_digest,
                }
                if spec_artifact is not None:
                    fields.update(revision_spec_artifact=spec_artifact, previous_claim_id=previous_claim_id)
                lease = self.store.claim_next(proof_id,
                    [LeaseCandidate(hypothesis_id, {}, tuple(i for i in
                        (parent_claim_id, obstruction_id, previous_claim_id) if i))],
                    actor=ACTOR, worker_class="coordinator", base_revision=expected, ttl_seconds=120)
                if lease is None:
                    raise ContractError("hypothesis is already leased by another operation")
                try:
                    result = self.gate.commit(Proposal(proof_id, lease.actor, lease.worker_class,
                        ops=(UpsertNode("Claim", claim_id, fields),
                             AddEdge("PROMOTED_TO", hypothesis_id, claim_id,
                                f"{proof_id}/promote-{content_hash([hypothesis_id, claim_id])[7:]}")),
                        base_revision=lease.base_revision, lease_id=lease.lease_id,
                        fencing_token=lease.fencing_token, selected_move_id=hypothesis_id))
                    if not result.accepted:
                        raise ContractError("gate rejected hypothesis admission: " +
                            ", ".join(item.reason.value for item in result.rejections))
                    self.catch_up(proof_id)
                finally:
                    self.store.release_lease(proof_id, lease.lease_id, fencing_token=lease.fencing_token)

        intake = self.intake.run(operation_id, proof_id, claim_id=claim_id,cancelled=cancelled)
        with self.lock:
            return {**self._status(proof_id), "claim_id": claim_id,
                "parent_claim_id": parent_claim_id, "hypothesis_id": hypothesis_id,
                "formalization_ready": intake.get("ready", False),
                "declaration_id": intake.get("declaration_id"), "alignment_id": intake.get("alignment_id"),
                "report_artifact": intake.get("report_artifact"),
                "formalization_disposition": intake.get('formalization_disposition',
                    'ready' if intake.get('ready') else 'waiting-on-review'),
                "clarification_reasons": intake.get('clarification_reasons', []),
                "error_code": intake.get("error_code"), "component_mode": "hypothesis-formalization"}

    def _create(self, raw_spec: str, operation_id: str) -> dict:
        try:
            spec = parse_json(raw_spec)
        except (ValueError, TypeError) as exc:
            raise ContractError("spec is not JSON") from exc
        complete = isinstance(spec, dict) and set(spec) == SPEC_FIELDS
        if complete:
            try:
                validate_spec(spec)
            except ValueError as exc:
                raise ContractError(str(exc)) from exc
            proof_id = "proof" + operation_id.replace("-", "")[:24]
            target, title = spec["theorem"], "Literal theorem"
        else:
            # Compatibility for the existing trusted reference-service fixtures.
            # The model-facing adapter accepts only the complete contract.
            if not isinstance(spec, dict) or set(spec) != {"proof_id", "literal_target", "title"}:
                raise ContractError("invalid theorem specification")
            proof_id, target, title = spec["proof_id"], spec["literal_target"], spec["title"]
        if not isinstance(proof_id, str) or not _ID.fullmatch(proof_id):
            raise ContractError("invalid proof id")
        if not isinstance(target, str) or not target.strip() or len(target.encode()) > 16_384:
            raise ContractError("invalid literal target")
        if not isinstance(title, str) or not title.strip():
            raise ContractError("invalid title")
        claim_id = f"{proof_id}/c-1"
        if validate_schema("proof-spec", {"proof_id": proof_id, "actor": ACTOR, "title": title, "target_claim_ids": [claim_id]}):
            raise ContractError("proof spec failed schema validation")
        self.catch_up(proof_id)
        revision, head = self.store.head(proof_id)
        if revision:
            node = self.view.node(f"{proof_id}/proof")
            if node and node.fields.get("operation_id") == operation_id:
                return self._status(proof_id)
            raise ContractError("proof id already exists")
        target_ref = self.artifacts.put(target.encode("utf-8"))
        spec_ref = self.artifacts.put(raw_spec.encode("utf-8")) if complete else target_ref
        lease_id = f"{proof_id}/init-{uuid.uuid4().hex}"
        token = self.store.acquire_lease(proof_id, lease_id, actor=ACTOR, worker_class="coordinator", ttl_seconds=30)
        try:
            proposal = Proposal(proof_id=proof_id, actor=ACTOR, worker_class="coordinator",
                ops=(UpsertNode("Proof", f"{proof_id}/proof", {"title": title, "actor": ACTOR, "operation_id": operation_id, "target_artifact": spec_ref}),
                     UpsertNode("Claim", claim_id, {"status": "provisional", "claim_text": target, "literal_target_hash": target_ref,
                         **({"theorem_spec_artifact": spec_ref, "assumptions": spec["assumptions"]} if complete else {})}),
                     AddEdge("HAS_TARGET", f"{proof_id}/proof", claim_id, f"{proof_id}/target-1")),
                base_revision=revision, lease_id=lease_id, fencing_token=token)
            result = self.gate.commit(proposal)
            if not result.accepted:
                raise ContractError("gate rejected bootstrap: " + ", ".join(r.reason.value for r in result.rejections))
            self.catch_up(proof_id)
            return {**self._status(proof_id), "event_hash": result.event_hash}
        finally:
            self.store.release_lease(proof_id, lease_id, fencing_token=token)

    def _status(self, proof_id: str) -> dict:
        self.catch_up(proof_id)
        revision, event_hash = self.store.head(proof_id)
        claim = self.view.node(f"{proof_id}/c-1")
        project = self.view.node(f"{proof_id}/proof")
        status = claim.fields.get("status") if claim else None
        return {"proof_id": proof_id, "revision": revision, "event_hash": event_hash,
            "claim_status": status, "formal_verified": status == "lean-verified",
            "projection_lag": revision - self.view.snapshot(proof_id)[0],
            "literal_target": claim.fields["claim_text"] if claim else None,
            "target_media_type": "application/json" if claim and project and project.fields["target_artifact"] != claim.fields["literal_target_hash"] else "text/plain; charset=utf-8",
            "artifact_pointers": list(dict.fromkeys([project.fields["target_artifact"], claim.fields["literal_target_hash"]])) if claim and project else [],
            "summary": ("Target is lean-verified through independently replayed, committed evidence."
                if status == "lean-verified" else f"claim remains {status}; independent Lean replay not recorded")}

    def _cycle(self, proof_id: str, operation_id: str, *, category: str | None = None,
               record_progress: bool = True) -> dict:
        attempt_id = f"{proof_id}/at-{operation_id.replace('-', '')}"
        self.catch_up(proof_id)
        existing = self.view.node(attempt_id)
        if existing is not None:
            return {**self._status(proof_id), "cycle_disposition": "pending-review",
                "component_mode": "scripted-reference", "packet_ref": existing.fields["packet_artifact"],
                "result_ref": existing.fields["result_artifact"], "attempt_id": attempt_id,
                "selected_move_id": existing.fields["selected_move_id"]}
        scheduler = GlobalScheduler(self.view, self.store, worker_actors={
            "critic": "omega-critic", "formal-atp": "omega-formal-adapter",
            "llm-research": "omega-research-dispatch"})
        lease = scheduler.lease_best_global_move(proof_id, ttl_seconds=15, category=category)
        if lease is None:
            frontier = [item for item in scheduler.build_frontier(proof_id)
                        if category is None or item.category == category]
            disposition = "waiting-on-lease" if frontier and self.store.active_leases(proof_id) else "idle"
            return {**self._status(proof_id), "cycle_disposition": disposition,
                "component_mode": "scripted-reference",
                "viable_move_ids": [candidate.move_id for candidate in frontier]}
        if record_progress:
            self.jobs.update(operation_id, {"operation_id": operation_id, "proof_id": proof_id, "phase": "leased", "selected_move_id": lease.selected_move_id})
        try:
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
        return sorted(referenced_artifacts(self.store.read_events(proof_id), read=self.artifacts.get))

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
