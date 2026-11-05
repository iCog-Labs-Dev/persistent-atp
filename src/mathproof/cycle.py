"""One scheduling cycle: lease -> dispatch -> propose -> commit (Section 10.2).

`run_cycle` is the thin outer loop of the reference algorithm. It owns no
state of its own: leases come from the scheduler, results come from adapters
or the dispatcher seam, and every write goes through the commit gate as an
inert proposal. An empty frontier is not a failure -- it is the coordinator's
cue to audit or expand (Section 10.4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.canon import content_hash
from commit_gate.state import JournalOverlayView, ReadView
from .worker_context import compile_context, compile_formal_request
from .dispatch import Dispatcher, result_to_proposal
from .ids import IdType
from .scheduler import GlobalScheduler, Lease

__all__ = ["CycleDigest", "run_cycle", "category_of"]


@dataclass(frozen=True, slots=True)
class CycleDigest:
    """What one cycle did, for the outer coordinator to react to."""

    lease_issued: bool = False
    selected_move_id: str | None = None
    worker_class: str | None = None
    accepted: bool = False
    revision: int | None = None
    rejections: tuple = field(default=())
    audit_recommended: bool = False
    error_type: str | None = None
    error: str | None = None

    @classmethod
    def empty_frontier(cls) -> "CycleDigest":
        return cls(audit_recommended=True)


def category_of(move_id: str, worker_class: str | None = None) -> str:
    """Infer a category, using worker class where claim/declaration IDs are shared."""
    local = move_id.rsplit("/", 1)[-1]
    if worker_class == "formalizer" and local.startswith("c-"):
        return "formalization-task"
    if worker_class == "alignment-reviewer" and local.startswith("fd-"):
        return "alignment-task"
    if worker_class == "replayer" and local.startswith("cert-"):
        return "replay-task"
    if worker_class == "experiment" and local.startswith(("rm-", "at-")):
        return "statement-check"
    if local.startswith("rm-"):
        return "research-move"
    if local.startswith(("fd-", "fr-")):
        return "formal-run"
    if local.startswith("c-"):
        return "critic-task"
    raise ValueError(f"cannot infer a frontier category from {move_id!r}")


def run_cycle(
    proof_id: str,
    *,
    view: ReadView,
    gate: CommitGate,
    scheduler: GlobalScheduler,
    dispatcher: Dispatcher,
    adapters: Mapping[str, Any] | None = None,
    worker_class: str = "llm-research",
    actor: str | None = None,
    category: str | None = None,
    ttl_seconds: float = 600.0,
    maintenance: Callable[[Any, CommitResult], None] | None = None,
    search_policy: str = "gnn-pln-best-first-v1",
    capture_dispatch_errors: bool = False,
    event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> CycleDigest:
    """Lease the best move, run it under its worker class, commit the result.

    ``actor`` is the worker the lease is issued to; it defaults to the worker
    class when the caller runs one anonymous worker per class.
    """
    replayed = gate.catch_up(proof_id)
    if replayed:
        _emit(event_sink, "projection_caught_up", replayed=replayed.get(proof_id, 0))
    if category is None:
        category = {
            "llm-research": "research-move",
            "critic": "critic-task",
            "experiment": "statement-check",
            "formalizer": "formalization-task",
            "alignment-reviewer": "alignment-task",
            "formal-atp": "formal-run",
            "replayer": "replay-task",
        }.get(worker_class)
    lease = scheduler.lease_next(
        proof_id, worker_class, ttl_seconds=ttl_seconds,
        actor=actor or worker_class, category=category,
    )
    if lease is None:
        _emit(event_sink, "frontier_empty", worker_class=worker_class, category=category)
        return CycleDigest.empty_frontier()
    _emit(
        event_sink,
        "lease_issued",
        lease_id=lease.lease_id,
        move_id=lease.selected_move_id,
        worker_class=lease.worker_class,
        base_revision=lease.base_revision,
    )
    keep_lease = False
    try:
        current = _view_at_lease(lease, view, gate)
        worker_started = time.perf_counter()
        _emit(
            event_sink,
            "worker_started",
            lease_id=lease.lease_id,
            move_id=lease.selected_move_id,
            worker_class=lease.worker_class,
        )
        try:
            result, proposal = _dispatch(
                lease,
                view=current,
                dispatcher=dispatcher,
                adapters=adapters or {},
                search_policy=search_policy,
                event_sink=event_sink,
            )
        except Exception as exc:
            message = " ".join(str(exc).split()) or type(exc).__name__
            _emit(
                event_sink,
                "worker_failed",
                lease_id=lease.lease_id,
                move_id=lease.selected_move_id,
                worker_class=lease.worker_class,
                duration_ms=round((time.perf_counter() - worker_started) * 1000, 3),
                error_type=type(exc).__name__,
                message=message[:1000],
            )
            if not capture_dispatch_errors:
                raise
            return CycleDigest(
                lease_issued=True,
                selected_move_id=lease.selected_move_id,
                worker_class=lease.worker_class,
                error_type=type(exc).__name__,
                error=message[:1000],
            )
        _emit(
            event_sink,
            "worker_completed",
            lease_id=lease.lease_id,
            move_id=lease.selected_move_id,
            worker_class=lease.worker_class,
            duration_ms=round((time.perf_counter() - worker_started) * 1000, 3),
            **_result_summary(lease.worker_class, result),
        )
        commit = gate.commit(proposal)
        proposal_summary = _proposal_summary(proposal)
        if commit.accepted:
            _emit(
                event_sink,
                "proposal_accepted",
                lease_id=lease.lease_id,
                move_id=lease.selected_move_id,
                worker_class=lease.worker_class,
                revision=commit.revision,
                **proposal_summary,
            )
        else:
            _emit(
                event_sink,
                "proposal_rejected",
                lease_id=lease.lease_id,
                move_id=lease.selected_move_id,
                worker_class=lease.worker_class,
                reasons=[rejection.reason.value for rejection in commit.rejections],
                **proposal_summary,
            )
        scheduler.update_statistics(commit, category)
        if commit.accepted:
            if maintenance is not None:
                maintenance(proposal, commit)
            _emit(
                event_sink,
                "projection_updated",
                revision=commit.revision,
                backend=type(view).__name__,
            )
            keep_lease = not result.get("terminal", True)
        return CycleDigest(
            lease_issued=True,
            selected_move_id=lease.selected_move_id,
            worker_class=lease.worker_class,
            accepted=commit.accepted,
            revision=commit.revision,
            rejections=commit.rejections,
        )
    finally:
        if not keep_lease:
            released = scheduler.release(
                proof_id, lease.lease_id, fencing_token=lease.fencing_token
            )
            _emit(
                event_sink,
                "lease_released",
                lease_id=lease.lease_id,
                move_id=lease.selected_move_id,
                worker_class=lease.worker_class,
                released=released,
            )


def _view_at_lease(lease: Lease, view: ReadView, gate: CommitGate) -> ReadView:
    """Give a worker exactly the committed revision its lease names."""
    revision, snapshot = view.snapshot(lease.proof_id)
    head = gate.store.head(lease.proof_id)[0]
    if head != lease.base_revision or revision > head:
        raise ValueError("proof revision changed before worker context compilation")
    events = gate.store.read_events_between(lease.proof_id, revision, head)
    ops = tuple(op for event in events for op in Proposal.from_dict(event).ops)
    return JournalOverlayView(snapshot, head, ops)


def _dispatch(
    lease: Lease,
    *,
    view: ReadView,
    dispatcher: Dispatcher,
    adapters: Mapping[str, Any],
    search_policy: str,
    event_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> tuple[Mapping[str, Any], Any]:
    """Run the worker and bind its output into a gate-ready Proposal."""
    if lease.worker_class == "formal-atp":
        adapter = adapters.get("formal-atp")
        if adapter is None:
            raise ValueError("no formal ATP adapter registered")
        resuming = lease.selected_move_id.rsplit("/", 1)[-1].startswith("fr-")
        request = compile_formal_request(
            lease,
            view,
            search_policy=search_policy,
            run_id=None
            if resuming
            else f"{lease.proof_id}/{IdType.FORMAL_RUN.value}-{_next_run(view, lease.proof_id)}",
        )
        _emit(event_sink, "context_compiled", role="formal-atp")
        result = dict(
            adapter.formal_search_resume(request["run_id"])
            if resuming
            else adapter.formal_search_start(request)
        )
        result["proof_id"] = lease.proof_id
        result["actor"] = lease.actor
        from .soundness import validate_formal_search_result
        violations = (
            validate_formal_search_result(
                result, pinned_environment_hash=request["environment_hash"]
            )
            if request["environment_hash"]
            else ()
        )
        if violations:
            raise ValueError(
                "invalid formal search result: "
                + "; ".join(violation.reason.value for violation in violations)
            )
        result["environment_id"] = request["environment_id"]
        result["claim_id"] = request["claim_id"]
    elif lease.worker_class == "replayer":
        adapter = adapters.get("replayer")
        if adapter is None:
            raise ValueError("no replay adapter registered")
        certificate = view.node(lease.selected_move_id)
        if certificate is None or certificate.label != "Certificate":
            raise ValueError("replay target is not a certificate")
        environment_edges = view.edges_from(certificate.node_id, "CERTIFICATE_ENVIRONMENT")
        if len(environment_edges) != 1:
            raise ValueError("certificate has no unique environment binding")
        environment = view.node(environment_edges[0].dst_id)
        if environment is None or not environment.fields.get("environment_hash"):
            raise ValueError("certificate environment is missing")
        _emit(event_sink, "context_compiled", role="replayer")
        result = dict(adapter.formal_replay(
            {"certificate_id": certificate.node_id, **dict(certificate.fields)},
            environment.fields["environment_hash"],
        ))
    elif lease.worker_class == "experiment":
        checker = adapters.get("lean-statement-checker")
        if checker is None:
            raise ValueError("statement check needs a Lean checker")
        packet = compile_context(lease, view)
        _emit(
            event_sink, "context_compiled", role=packet.get("role"),
            context_digest=packet.get("manifest_digest"),
        )
        declaration = packet["declaration"]
        fields = declaration["fields"]
        checked = checker.check(fields["lean_type"], fields["imports"])
        result = {
            "success": checked.ok,
            "diagnostics": checked.diagnostics,
            "source_hash": content_hash({"lean_type": fields["lean_type"], "imports": fields["imports"]}),
            "context_digest": packet["manifest_digest"],
        }
    elif lease.worker_class == "alignment-reviewer":
        from alignment.models import (
            AlignmentCriteria, AlignmentReviewRequest, AlignmentReviewResult,
        )
        from alignment.worker import build_alignment_proposal
        from alignment.reviewer import RuleBasedAlignmentReviewer
        from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict

        worker = adapters.get("alignment-reviewer")
        checker = adapters.get("lean-statement-checker")
        if worker is None or checker is None:
            raise ValueError("alignment worker and Lean statement checker are required")
        if isinstance(worker.reviewer, RuleBasedAlignmentReviewer):
            raise ValueError("rule-based alignment review cannot approve formalization")
        if worker.actor != lease.actor:
            raise ValueError("alignment worker actor differs from its lease")
        packet = compile_context(lease, view)
        _emit(
            event_sink, "context_compiled", role=packet.get("role"),
            context_digest=packet.get("manifest_digest"),
        )
        claim, declaration = packet["claim"], packet["declaration"]
        fields = declaration["fields"]
        checked = checker.check(fields["lean_type"], fields["imports"])
        request = AlignmentReviewRequest(
            proof_id=lease.proof_id,
            claim_id=claim["id"],
            declaration_id=declaration["id"],
            informal_statement=claim["fields"]["claim_text"],
            formal_statement=fields["statement_source"],
            imports=tuple(fields["imports"]),
            namespace=fields["namespace"],
        )
        if checked.ok:
            review = worker.reviewer.review(request)
        else:
            review = AlignmentReviewResult(
                verdict=AlignmentVerdict.AMBIGUOUS,
                lifecycle=AlignmentLifecycle.REVIEW_NEEDED,
                criteria=AlignmentCriteria(notes="Lean could not elaborate the draft statement"),
                reviewer=worker.actor,
                reasoning="Lean statement elaboration failed; inspect diagnostics.",
            )
        from .dispatch import _next_serial
        alignment_id = f"{lease.proof_id}/al-{_next_serial(view, 'al', lease.proof_id)}"
        promote_claim = claim["fields"].get("status") == "provisional" and any(
            (attempt := view.node(edge.src_id)) is not None
            and attempt.label == "Attempt"
            and attempt.fields.get("worker_class") == "critic"
            and attempt.fields.get("status") in {"supported", "critic-accepted"}
            for edge in view.edges_to(claim["id"], "REVIEWS_CLAIM")
        )
        proposal = build_alignment_proposal(
            proof_id=lease.proof_id, alignment_id=alignment_id,
            request=request, result=review, actor=worker.actor,
            base_revision=lease.base_revision, lease_id=lease.lease_id,
            fencing_token=lease.fencing_token, selected_move_id=lease.selected_move_id,
            statement_checked=checked.ok, lean_diagnostics=checked.diagnostics,
            promote_claim=promote_claim,
        )
        return {"verdict": review.verdict.value, "statement_checked": checked.ok}, proposal
    else:
        packet = compile_context(lease, view)
        _emit(
            event_sink, "context_compiled", role=packet.get("role"),
            context_digest=packet.get("manifest_digest"),
        )
        result = dict(dispatcher.run(lease, packet))

    attempt_serial = _next_attempt(view, lease.proof_id)
    attempt_id = f"{lease.proof_id}/{IdType.ATTEMPT.value}-{attempt_serial}"
    proposal = result_to_proposal(result, lease, view, attempt_id=attempt_id)
    return result, proposal


def _emit(
    sink: Callable[[str, Mapping[str, Any]], None] | None,
    event: str,
    **fields: Any,
) -> None:
    if sink is not None:
        sink(event, fields)


def _result_summary(worker_class: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Safe semantic summary: no prompts, arguments, or model output."""
    if worker_class in {"llm-research", "hyperon"}:
        candidate = result.get("candidate_argument")
        return {
            "result": "candidate_argument" if candidate else "research_moves",
            "proposed_moves": len(result.get("moves", ())),
            "move_status": result.get("move_status"),
        }
    if worker_class == "critic":
        return {
            "result": "claim_review",
            "verdict": result.get("verdict"),
            "reported_verdict": result.get("reported_verdict"),
            "evidence_count": len(result.get("evidence_ids", ())),
        }
    if worker_class == "formalizer":
        return {
            "result": "formal_declaration_drafted",
            "lean_name": result.get("lean_name"),
            "imports": result.get("imports"),
        }
    if worker_class == "alignment-reviewer":
        return {
            "result": "alignment_review",
            "verdict": result.get("verdict"),
            "statement_checked": result.get("statement_checked"),
        }
    if worker_class == "experiment":
        return {
            "result": "statement_check",
            "success": result.get("success"),
        }
    if worker_class == "formal-atp":
        return {
            "result": "formal_search",
            "status": result.get("status") or result.get("disposition"),
        }
    if worker_class == "replayer":
        return {
            "result": "certificate_replay",
            "status": result.get("status"),
            "simulated": bool(result.get("simulated", False)),
        }
    return {"result": "completed"}


def _proposal_summary(proposal: Proposal) -> dict[str, Any]:
    created = [
        f"{op.label}:{op.node_id}"
        for op in proposal.ops
        if isinstance(op, UpsertNode)
    ]
    status_changes = [
        f"{op.label}:{op.node_id}={op.value}"
        for op in proposal.ops
        if isinstance(op, SetField) and op.field == "status"
    ]
    return {
        "created": created,
        "status_changes": status_changes,
        "operation_count": len(proposal.ops),
    }


def _next_attempt(view: ReadView, proof_id: str) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "at", proof_id)


def _next_run(view: ReadView, proof_id: str) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "fr", proof_id)
