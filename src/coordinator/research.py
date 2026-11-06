"""Journal-backed reconciliation of decomposed research alternatives."""

from __future__ import annotations

import uuid

from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView, belongs_to_proof
from commit_gate.store import JournalStore
from mathproof.dispatch import _next_serial
from shared.vocab import WorkerClass

__all__ = ["reconcile_research", "research_reconciliation_ops"]


_ACCEPTED_SUBGOAL_STATUSES = frozenset(
    {"critic-accepted", "formally-closed", "lean-verified"}
)


def research_reconciliation_ops(view: ReadView, proof_id: str) -> list:
    """Create synthesis work, or disable alternatives with refuted obligations."""
    moves = view.list_nodes(proof_id, "ResearchMove")
    synthesis_by_parent = {
        move.fields.get("synthesis_of")
        for move in moves
        if isinstance(move.fields.get("synthesis_of"), str)
    }
    serial = _next_serial(view, "rm", proof_id)
    ops: list = []
    for parent in moves:
        required = [
            edge.dst_id
            for edge in view.edges_from(parent.node_id, "REQUIRES_SUBGOAL")
            if belongs_to_proof(edge.dst_id, proof_id)
        ]
        if not required or parent.fields.get("alternative_status") == "unusable":
            continue
        claims = []
        valid = True
        for child_move_id in required:
            targets = [
                edge.dst_id
                for edge in view.edges_from(child_move_id, "MOVE_TARGETS")
                if belongs_to_proof(edge.dst_id, proof_id)
            ]
            claim = view.node(targets[0]) if len(targets) == 1 else None
            if claim is None or claim.label != "Claim":
                valid = False
                break
            claims.append(claim)
        if not valid:
            continue

        refuted = sorted(
            claim.node_id for claim in claims
            if claim.fields.get("status") == "refuted"
        )
        if refuted:
            ops.extend((
                SetField(
                    "ResearchMove", parent.node_id, "alternative_status", "unusable",
                    prior=parent.fields.get("alternative_status"),
                ),
                SetField(
                    "ResearchMove", parent.node_id, "refuted_subgoal_ids", refuted,
                    prior=parent.fields.get("refuted_subgoal_ids"),
                ),
            ))
            continue

        if (
            parent.node_id in synthesis_by_parent
            or not all(
                claim.fields.get("status") in _ACCEPTED_SUBGOAL_STATUSES
                for claim in claims
            )
        ):
            continue
        targets = [
            edge.dst_id
            for edge in view.edges_from(parent.node_id, "MOVE_TARGETS")
            if belongs_to_proof(edge.dst_id, proof_id)
        ]
        state_id = parent.fields.get("state_id")
        state = view.node(state_id) if isinstance(state_id, str) else None
        if len(targets) != 1 or state is None or state.label != "ResearchState":
            continue

        synthesis_id = f"{proof_id}/rm-{serial}"
        serial += 1
        prerequisites = sorted({claim.node_id for claim in claims})
        ops.extend((
            UpsertNode("ResearchMove", synthesis_id, {
                "status": "queued",
                "state_id": state_id,
                "synthesis_of": parent.node_id,
                "detail": "Synthesize the accepted subgoals into a direct argument for the parent claim",
                "prerequisite_claim_ids": prerequisites,
            }),
            AddEdge("PROPOSES", state_id, synthesis_id, f"{synthesis_id}-proposed"),
            AddEdge("MOVE_TARGETS", synthesis_id, targets[0], f"{synthesis_id}-target-0"),
            SetField(
                "ResearchMove", parent.node_id, "alternative_status", "synthesis-queued",
                prior=parent.fields.get("alternative_status"),
            ),
            SetField(
                "ResearchMove", parent.node_id, "synthesis_move_id", synthesis_id,
                prior=parent.fields.get("synthesis_move_id"),
            ),
        ))
        synthesis_by_parent.add(parent.node_id)
    return ops


def reconcile_research(
    store: JournalStore, view: ReadView, proof_id: str
) -> CommitResult | None:
    """Commit deterministic reconciliation through the authoritative gate."""
    ops = research_reconciliation_ops(view, proof_id)
    if not ops:
        return None
    lease_id = f"{proof_id}/ls-research-{uuid.uuid4().hex[:12]}"
    token = store.acquire_lease(
        proof_id, lease_id, actor="research-reconciler",
        worker_class=WorkerClass.MAINTENANCE.value,
    )
    try:
        return CommitGate(view, store).commit(Proposal(
            proof_id=proof_id,
            actor="research-reconciler",
            worker_class=WorkerClass.MAINTENANCE.value,
            ops=tuple(ops),
            base_revision=store.head(proof_id)[0],
            lease_id=lease_id,
            fencing_token=token,
        ))
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=token)
