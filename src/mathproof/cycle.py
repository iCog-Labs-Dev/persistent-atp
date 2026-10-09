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
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView
from commit_gate.store import JournalStore
from commit_gate.vocab import (
    FormalStateStatus,
    ReplayStatus,
    RunDisposition,
    WorkerClass,
)
from .context_compiler import compile_context, compile_formal_request
from .dispatch import Dispatcher, _next_serial, result_to_proposal
from .ids import IdType
from .routing import RoutingDecision, evaluate_run
from .scheduler import GlobalScheduler, Lease

__all__ = ["CycleDigest", "run_cycle", "category_of", "handle_replay_and_closure"]


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
    routing: RoutingDecision | None = None

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
    auto_replay: bool = False,
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

    routing = _route_after_commit(
        result,
        view=view,
        gate=gate,
        store=scheduler._store,
        proof_id=proof_id,
        maintenance=maintenance,
    )

    if auto_replay and routing is not None and routing.action == "hold-for-replay":
        formal_adapter = (adapters or {}).get("formal-atp")
        if formal_adapter is not None:
            handle_replay_and_closure(
                routing.run_id,
                proof_id,
                view,
                gate,
                formal_adapter,
                scheduler._store,
                maintenance=maintenance,
            )

    return CycleDigest(
        lease_issued=True,
        selected_move_id=lease.selected_move_id,
        worker_class=lease.worker_class,
        accepted=commit.accepted,
        revision=commit.revision,
        rejections=commit.rejections,
        routing=routing,
    )


def _route_after_commit(
    result: Mapping[str, Any],
    *,
    view: ReadView,
    gate: CommitGate,
    store,
    proof_id: str,
    maintenance: Callable[[Any, CommitResult], None] | None = None,
) -> RoutingDecision | None:
    """Evaluate the Section 8.7 trigger table over the run just committed.

    A bridge-lemma convergence fires a follow-up research-move proposal
    through the gate; every other decision rides the digest back to the
    coordinator to fold into the next cycle (e.g. widened retrieval).
    """
    run_id = result.get("run_id")
    if not run_id or view.node(run_id) is None:
        return None
    decision = evaluate_run(view, run_id)
    if decision is None or decision.action != "propose-bridge-lemma":
        return decision

    at_state = decision.params.get("at_state")
    if at_state is None or view.node(at_state) is None:
        return decision

    from commit_gate.ops import AddEdge, UpsertNode
    from .dispatch import _next_serial

    # An obstruction converges into a fresh research state whose first move
    # is the bridge-lemma request; PROPOSES hangs off ResearchState only.
    state_id = f"{proof_id}/rs-{_next_serial(view, 'rs')}"
    move_id = f"{proof_id}/rm-{_next_serial(view, 'rm')}"
    detail = (
        f"bridge lemma for {decision.params.get('obstruction_kind')} "
        f"converging at {at_state}"
    )
    ops = (
        UpsertNode(
            "ResearchState",
            state_id,
            {"status": "open", "origin_state": at_state},
        ),
        UpsertNode("ResearchMove", move_id, {"status": "queued", "detail": detail}),
        AddEdge("PROPOSES", state_id, move_id, f"{move_id}-proposed"),
    )
    proposal = Proposal(
        proof_id=proof_id,
        actor="scheduler-routing",
        worker_class="llm-research",
        ops=ops,
        base_revision=store.head(proof_id)[0],
    )
    follow_up = gate.commit(proposal)
    if follow_up.accepted and maintenance is not None:
        maintenance(proposal, follow_up)
    return decision


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


def handle_replay_and_closure(
    run_id: str,
    proof_id: str,
    view: ReadView,
    gate: CommitGate,
    adapter: Any,
    store: JournalStore,
    *,
    maintenance: Callable[[Any, CommitResult], None] | None = None,
) -> CommitResult | None:
    """If run_id has disposition proved-pending-replay:
    1. Verify certificate via adapter.formal_replay(cert, env).
    2. Build proposal:
       - UpsertNode("LeanReplay", ...)
       - AddEdge("REPLAYED_BY", ...)
       - SetField("FormalState", root_state_id, "status", "formally-closed", prior=root_status)
    3. Commit through gate under an administrative lease.
    """
    run = view.node(run_id)
    if run is None or run.label != "FormalRun":
        return None
    if run.fields.get("status") != RunDisposition.PROVED_PENDING_REPLAY.value:
        return None

    cert_edges = list(view.edges_from(run_id, "PRODUCED_CERTIFICATE"))
    if not cert_edges:
        return None
    cert_id = cert_edges[0].dst_id
    cert_node = view.node(cert_id)
    if cert_node is None:
        return None

    cert_payload = dict(cert_node.fields)
    cert_payload.setdefault("certificate_id", cert_id)

    env_hash = cert_payload.get("environment_hash")
    if not env_hash:
        env_hash = run.fields.get("environment_hash")
    if not env_hash:
        env_hash = "sha256:" + "00" * 32

    replay_result = adapter.formal_replay(cert_payload, env_hash)
    if (
        replay_result.get("status") != ReplayStatus.VERIFIED.value
        or replay_result.get("sorry_detected", False)
    ):
        return None

    root_edges = list(view.edges_from(run_id, "HAS_ROOT"))
    if not root_edges:
        return None
    root_state_id = root_edges[0].dst_id
    root_state = view.node(root_state_id)
    if root_state is None:
        return None
    root_status = root_state.fields.get("status", "open")

    serial = _next_serial(view, IdType.LEAN_REPLAY.value)
    replay_id = f"{proof_id}/{IdType.LEAN_REPLAY.value}-{serial}"

    ops = [
        UpsertNode(
            "LeanReplay",
            replay_id,
            {
                "actor": "replayer",
                "status": ReplayStatus.VERIFIED.value,
                "sorry_detected": False,
            },
        ),
        AddEdge(
            "REPLAYED_BY",
            cert_id,
            replay_id,
            f"{cert_id}-replayed-{serial}",
        ),
        SetField(
            "FormalState",
            root_state_id,
            "status",
            FormalStateStatus.FORMALLY_CLOSED.value,
            prior=root_status,
        ),
    ]

    lease_row = store.issue_lease(
        proof_id,
        None,
        worker_class=WorkerClass.COORDINATOR.value,
        selected_move_id=run_id,
        ttl_seconds=300.0,
    )
    if lease_row is None:
        return None

    proposal = Proposal(
        proof_id=proof_id,
        actor=WorkerClass.COORDINATOR.value,
        worker_class=WorkerClass.COORDINATOR.value,
        ops=tuple(ops),
        base_revision=lease_row.base_revision,
        lease_id=lease_row.lease_id,
        fencing_token=lease_row.fencing_token,
    )

    try:
        commit_result = gate.commit(proposal)
        if commit_result.accepted and maintenance is not None:
            maintenance(proposal, commit_result)
        return commit_result
    finally:
        store.release_lease(proof_id, lease_row.lease_id)
