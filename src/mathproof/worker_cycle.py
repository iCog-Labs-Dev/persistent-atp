"""Lease one worker task and submit its result through the commit gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.vocab import ResearchMoveStatus, WorkerClass
from mathproof.ids import IdType, parse_local_id
from mathproof.scheduler import GlobalScheduler, Lease
from mathproof.workers import WorkerDispatcher


WORKER_CATEGORIES = {
    "research-move": WorkerClass.LLM_RESEARCH.value,
    "critic-task": WorkerClass.CRITIC.value,
}


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    lease: Lease
    result: Mapping[str, Any]
    commit: CommitResult


def _current_snapshot(gate: CommitGate, proof_id: str) -> MemoryView:
    revision, snapshot = gate.view.snapshot(proof_id)
    if not isinstance(snapshot, MemoryView):
        raise TypeError("agent dispatch needs a materialized MemoryView snapshot")
    head = gate.store.head(proof_id)[0]
    if revision > head:
        raise ValueError("read view is ahead of the journal")
    for event in gate.store.read_events_between(proof_id, revision, head):
        apply_ops(snapshot, Proposal.from_dict(event).ops)
    return snapshot


def _next_id(view: MemoryView, proof_id: str, id_type: IdType) -> str:
    highest = 0
    prefix = f"{proof_id}/"
    for node_id in view.nodes:
        if not node_id.startswith(prefix):
            continue
        try:
            kind, serial = parse_local_id(node_id[len(prefix):])
        except ValueError:
            continue
        if kind == id_type:
            highest = max(highest, serial)
    return f"{proof_id}/{id_type.value}-{highest + 1}"


def _context(view: MemoryView, lease: Lease) -> dict[str, Any]:
    target = view.node(lease.selected_move_id)
    expected = "ResearchMove" if lease.worker_class == WorkerClass.LLM_RESEARCH.value else "Claim"
    if target is None or target.label != expected or not target.node_id.startswith(f"{lease.proof_id}/"):
        raise ValueError("leased task is missing or has the wrong label")
    context: dict[str, Any] = {"task": {"id": target.node_id, "fields": dict(target.fields)}}

    def related(rel_type: str, *, incoming: bool = False) -> list[dict[str, Any]]:
        edges = (
            view.edges_to(target.node_id, rel_type)
            if incoming else view.edges_from(target.node_id, rel_type)
        )
        records = []
        for edge in edges:
            node_id = edge.src_id if incoming else edge.dst_id
            node = view.node(node_id)
            if node is not None and node_id.startswith(f"{lease.proof_id}/"):
                records.append({"id": node_id, "fields": dict(node.fields)})
        return sorted(records, key=lambda record: record["id"])

    if expected == "ResearchMove":
        parents = [edge.src_id for edge in view.edges_to(target.node_id, "PROPOSES")]
        parent = view.node(parents[0]) if len(parents) == 1 else None
        if parent is None or parent.label != "ResearchState" or not parent.node_id.startswith(f"{lease.proof_id}/"):
            raise ValueError("research move needs exactly one parent research state")
        context["parent_state_id"] = parents[0]
        context["parent_state"] = dict(parent.fields)
        context["target_claims"] = related("MOVE_TARGETS")
    else:
        context["dependencies"] = related("DEPENDS_ON")
        context["prior_reviews"] = related("REVIEWS_CLAIM", incoming=True)
        context["alignments"] = related("ALIGNS_CLAIM", incoming=True)
    return context


def _proposal(
    lease: Lease, result: Mapping[str, Any], context: Mapping[str, Any], view: MemoryView
) -> Proposal:
    target_id = lease.selected_move_id
    if lease.worker_class == WorkerClass.LLM_RESEARCH.value:
        target = view.node(target_id)
        next_move = result["next_move"]
        status = (
            ResearchMoveStatus.CLOSED.value if next_move
            else ResearchMoveStatus.EXHAUSTED.value
        )
        ops: list[Any] = [
            SetField("ResearchMove", target_id, "reasoning", result["reasoning"],
                     prior=target.fields.get("reasoning")),
            SetField("ResearchMove", target_id, "status", status,
                     prior=target.fields["status"]),
        ]
        if next_move:
            move_id = _next_id(view, lease.proof_id, IdType.RESEARCH_MOVE)
            ops.extend((
                UpsertNode("ResearchMove", move_id, {"status": "queued", "detail": next_move}),
                AddEdge("PROPOSES", context["parent_state_id"], move_id,
                        f"{move_id}-proposed"),
            ))
    elif lease.worker_class == WorkerClass.CRITIC.value:
        attempt_id = _next_id(view, lease.proof_id, IdType.ATTEMPT)
        ops = [
            UpsertNode("Attempt", attempt_id, {
                "actor": lease.actor,
                "worker_class": lease.worker_class,
                "status": result["verdict"],
                "reasoning": result["reasoning"],
            }),
            AddEdge("REVIEWS_CLAIM", attempt_id, target_id, f"{attempt_id}-reviews"),
        ]
    else:
        raise ValueError(f"unsupported agent class {lease.worker_class!r}")
    return Proposal(
        proof_id=lease.proof_id,
        actor=lease.actor,
        worker_class=lease.worker_class,
        base_revision=lease.base_revision,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        selected_move_id=target_id,
        ops=tuple(ops),
    )


def run_worker_cycle(
    proof_id: str,
    category: str,
    *,
    actor: str,
    scheduler: GlobalScheduler,
    dispatcher: WorkerDispatcher,
    gate: CommitGate,
) -> WorkerCycleResult | None:
    """Run at most one task of the requested category; idle returns None."""
    try:
        worker_class = WORKER_CATEGORIES[category]
    except KeyError as exc:
        raise ValueError(f"unsupported worker category {category!r}") from exc
    lease = scheduler.lease_next(
        proof_id, worker_class, actor=actor, category=category
    )
    if lease is None:
        return None
    try:
        view = _current_snapshot(gate, proof_id)
        context = _context(view, lease)
        result = dispatcher.run(lease, context)
        commit = gate.commit(_proposal(lease, result, context, view))
        scheduler.update_statistics(commit, category)
        return WorkerCycleResult(lease, result, commit)
    finally:
        scheduler.release(proof_id, lease.lease_id, fencing_token=lease.fencing_token)
