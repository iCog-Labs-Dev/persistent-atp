"""Escalations become proposals: the coordinator's only writes (Section 12.1).

The commit gate trusts the coordinator worker class with any label, so the
limit is kept here: the coordinator proposes queued research moves under its
own research state and nothing else. It never writes a claim status, a formal
state, a certificate or an obstruction -- those belong to the workers that
take its moves, and reach the journal through the gate like any other result.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.dispatch import _next_serial
from shared.vocab import WorkerClass

from .policy import Escalation
from .progress import COORDINATOR_ROLE, ProofReading, nodes_of

__all__ = ["COORDINATOR_ACTOR", "COORDINATOR_LABELS", "escalation_ops", "commit_escalations"]

COORDINATOR_ACTOR = "coordinator"
COORDINATOR_LABELS = frozenset({"ResearchState", "ResearchMove"})
COORDINATOR_EDGES = frozenset({"PROPOSES", "MOVE_TARGETS"})


def escalation_ops(
    view: MemoryView,
    proof_id: str,
    escalations: Sequence[Escalation],
    *,
    reading: ProofReading,
    round_number: int,
) -> list:
    """Queued moves for `escalations`, under the coordinator's research state."""
    ops: list = []
    state_id = next(
        (
            state.node_id
            for state in nodes_of(view, proof_id, "ResearchState")
            if state.fields.get("role") == COORDINATOR_ROLE
        ),
        None,
    )
    if state_id is None:
        state_id = f"{proof_id}/rs-{_next_serial(view, 'rs')}"
        ops.append(
            UpsertNode(
                "ResearchState",
                state_id,
                {"status": "open", "role": COORDINATOR_ROLE, "detail": "coordinator escalations"},
            )
        )
    serial = _next_serial(view, "rm")
    for escalation in escalations:
        move_id = f"{proof_id}/rm-{serial}"
        serial += 1
        ops.append(
            UpsertNode(
                "ResearchMove",
                move_id,
                {
                    "status": "queued",
                    "escalation": escalation.rule,
                    "target_id": escalation.target_id,
                    "detail": escalation.detail,
                    "basis": reading.basis,
                    "round": round_number,
                },
            )
        )
        ops.append(AddEdge("PROPOSES", state_id, move_id, f"{move_id}-proposed"))
        if escalation.claim_id is not None:
            ops.append(
                AddEdge("MOVE_TARGETS", move_id, escalation.claim_id, f"{move_id}-targets")
            )
    return ops


def commit_escalations(
    store: JournalStore,
    view: MemoryView,
    proof_id: str,
    escalations: Sequence[Escalation],
    *,
    reading: ProofReading,
    round_number: int,
) -> CommitResult | None:
    """Commit `escalations` as one coordinator proposal, under a proof-wide lease.

    The lease supersedes any dispatch still held on the proof, so the moves are
    proposed against a quiet head; a lost race is a rejection, not a retry.
    """
    if not escalations:
        return None
    ops = escalation_ops(
        view, proof_id, escalations, reading=reading, round_number=round_number
    )
    lease_id = f"{proof_id}/ls-{uuid.uuid4().hex[:12]}"
    fencing_token = store.acquire_lease(
        proof_id,
        lease_id,
        actor=COORDINATOR_ACTOR,
        worker_class=WorkerClass.COORDINATOR.value,
    )
    try:
        return CommitGate(view, store).commit(
            Proposal(
                proof_id=proof_id,
                actor=COORDINATOR_ACTOR,
                worker_class=WorkerClass.COORDINATOR.value,
                ops=tuple(ops),
                base_revision=store.head(proof_id)[0],
                lease_id=lease_id,
                fencing_token=fencing_token,
            )
        )
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=fencing_token)
