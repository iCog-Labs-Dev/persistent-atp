"""One scheduling cycle: lease -> dispatch -> propose -> commit (Section 10.2).

`run_cycle` is the thin outer loop of the reference algorithm. It owns no
state of its own: leases come from the scheduler, results come from adapters
or the dispatcher seam, and every write goes through the commit gate as an
inert proposal. An empty frontier is not a failure -- it is the coordinator's
cue to audit or expand (Section 10.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.gate import CommitGate, CommitResult
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
) -> CycleDigest:
    """Lease the best move, run it under its worker class, commit the result.

    ``actor`` is the worker the lease is issued to; it defaults to the worker
    class when the caller runs one anonymous worker per class.
    """
    gate.catch_up(proof_id)
    if category is None:
        category = {
            "llm-research": "research-move",
            "critic": "critic-task",
            "experiment": "statement-check",
            "formalizer": "formalization-task",
            "alignment-reviewer": "alignment-task",
            "formal-atp": "formal-run",
        }.get(worker_class)
    lease = scheduler.lease_next(
        proof_id, worker_class, ttl_seconds=ttl_seconds,
        actor=actor or worker_class, category=category,
    )
    if lease is None:
        return CycleDigest.empty_frontier()
    keep_lease = False
    try:
        current = _view_at_lease(lease, view, gate)
        result, proposal = _dispatch(
            lease,
            view=current,
            dispatcher=dispatcher,
            adapters=adapters or {},
            search_policy=search_policy,
        )
        commit = gate.commit(proposal)
        scheduler.update_statistics(commit, category)
        if commit.accepted:
            if maintenance is not None:
                maintenance(proposal, commit)
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
            scheduler.release(proof_id, lease.lease_id, fencing_token=lease.fencing_token)


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
        result = (
            adapter.formal_search_resume(request["run_id"])
            if resuming
            else adapter.formal_search_start(request)
        )
        result["proof_id"] = lease.proof_id
    elif lease.worker_class == "experiment":
        checker = adapters.get("lean-statement-checker")
        if checker is None:
            raise ValueError("statement check needs a Lean checker")
        packet = compile_context(lease, view)
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
        proposal = build_alignment_proposal(
            proof_id=lease.proof_id, alignment_id=alignment_id,
            request=request, result=review, actor=worker.actor,
            base_revision=lease.base_revision, lease_id=lease.lease_id,
            fencing_token=lease.fencing_token, selected_move_id=lease.selected_move_id,
            statement_checked=checked.ok, lean_diagnostics=checked.diagnostics,
        )
        return {"verdict": review.verdict.value, "statement_checked": checked.ok}, proposal
    else:
        packet = compile_context(lease, view)
        result = dict(dispatcher.run(lease, packet))

    attempt_serial = _next_attempt(view, lease.proof_id)
    attempt_id = f"{lease.proof_id}/{IdType.ATTEMPT.value}-{attempt_serial}"
    proposal = result_to_proposal(result, lease, view, attempt_id=attempt_id)
    return result, proposal


def _next_attempt(view: ReadView, proof_id: str) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "at", proof_id)


def _next_run(view: ReadView, proof_id: str) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "fr", proof_id)
