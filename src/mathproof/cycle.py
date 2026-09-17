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
from commit_gate.state import ReadView
from .context_compiler import compile_context, compile_formal_request
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


def category_of(move_id: str) -> str:
    """Which frontier category a selected move belongs to, by its ID type."""
    local = move_id.rsplit("/", 1)[-1]
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
    ttl_seconds: float = 600.0,
    maintenance: Callable[[Any, CommitResult], None] | None = None,
    search_policy: str = "gnn-pln-best-first-v1",
) -> CycleDigest:
    """Lease the best move, run it under its worker class, commit the result."""
    lease = scheduler.lease_next(proof_id, worker_class, ttl_seconds=ttl_seconds)
    if lease is None:
        return CycleDigest.empty_frontier()

    result, proposal = _dispatch(
        lease,
        view=view,
        dispatcher=dispatcher,
        adapters=adapters or {},
        search_policy=search_policy,
    )
    commit = gate.commit(proposal)
    scheduler.update_statistics(commit, category_of(lease.selected_move_id))

    if commit.accepted:
        if maintenance is not None:
            maintenance(proposal, commit)
        if result.get("terminal", True):
            scheduler.release(proof_id, lease.lease_id)

    return CycleDigest(
        lease_issued=True,
        selected_move_id=lease.selected_move_id,
        worker_class=lease.worker_class,
        accepted=commit.accepted,
        revision=commit.revision,
        rejections=commit.rejections,
    )


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
            else f"{lease.proof_id}/{IdType.FORMAL_RUN.value}-{_next_run(view)}",
        )
        result = (
            adapter.formal_search_resume(request["run_id"])
            if resuming
            else adapter.formal_search_start(request)
        )
        result["proof_id"] = lease.proof_id
    else:
        packet = compile_context(lease, view)
        result = dict(dispatcher.run(lease, packet))

    attempt_serial = _next_attempt(view)
    attempt_id = f"{lease.proof_id}/{IdType.ATTEMPT.value}-{attempt_serial}"
    proposal = result_to_proposal(result, lease, view, attempt_id=attempt_id)
    return result, proposal


def _next_attempt(view: ReadView) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "at")


def _next_run(view: ReadView) -> int:
    from .dispatch import _next_serial

    return _next_serial(view, "fr")
