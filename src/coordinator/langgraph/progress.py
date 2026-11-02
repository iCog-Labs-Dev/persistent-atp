"""What committed state says about a proof's progress (Sections 10.1, 10.4).

Every signal here is recomputed from the view, never remembered: whatever the
coordinator caches between rounds can be thrown away and rebuilt from the
journal. Progress means verified gain only -- a settled claim, a closed
formal state, a replayed declaration. Activity that settles nothing (a new
move, a failed run, a refutation) is not progress, which is what lets the
stagnation policy see a proof that is busy but stuck.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterator

from commit_gate.state import MemoryView, NodeRecord
from shared.vocab import (
    AlignmentVerdict,
    ClaimStatus,
    DeclarationStatus,
    FormalStateStatus,
)

__all__ = [
    "ProofReading",
    "read_proof",
    "nodes_of",
    "run_declaration",
    "COORDINATOR_ROLE",
]

COORDINATOR_ROLE = "coordinator"
"""The `role` field of the ResearchState the coordinator proposes moves from."""

SETTLED_CLAIMS = frozenset(
    {
        ClaimStatus.CRITIC_ACCEPTED.value,
        ClaimStatus.FORMALLY_CLOSED.value,
        ClaimStatus.LEAN_VERIFIED.value,
    }
)
CLOSED_STATES = frozenset(
    {FormalStateStatus.FORMALLY_CLOSED.value, FormalStateStatus.LEAN_VERIFIED.value}
)
PENDING_CERTIFICATES = frozenset({"candidate", "replay-pending"})


@dataclass(frozen=True, slots=True)
class ProofReading:
    """One proof's committed position, as plain counts."""

    revision: int
    settled_claims: int = 0
    closed_states: int = 0
    replayed_declarations: int = 0
    declarations: int = 0
    pending_certificates: int = 0

    @property
    def progress(self) -> tuple[int, int, int]:
        return (self.settled_claims, self.closed_states, self.replayed_declarations)

    @property
    def basis(self) -> str:
        """The progress level an escalation was raised at, as a field value."""
        return "/".join(str(count) for count in self.progress)

    @property
    def solved(self) -> bool:
        return self.declarations > 0 and self.replayed_declarations == self.declarations

    @property
    def awaiting_replay(self) -> bool:
        return not self.solved and self.pending_certificates > 0

    def advanced_from(self, earlier: "ProofReading") -> bool:
        return any(now > then for now, then in zip(self.progress, earlier.progress))


def nodes_of(view: MemoryView, proof_id: str, label: str) -> Iterator[NodeRecord]:
    prefix = f"{proof_id}/"
    for node_id, record in list(view.nodes.items()):
        if node_id.startswith(prefix) and record.label == label:
            yield record


def read_proof(view: MemoryView, proof_id: str, revision: int) -> ProofReading:
    def count(label: str, statuses: frozenset[str]) -> int:
        return sum(
            1 for node in nodes_of(view, proof_id, label)
            if node.fields.get("status") in statuses
        )

    return ProofReading(
        revision=revision,
        settled_claims=count("Claim", SETTLED_CLAIMS),
        closed_states=count("FormalState", CLOSED_STATES),
        replayed_declarations=count(
            "FormalDeclaration", frozenset({DeclarationStatus.REPLAY_ACCEPTED.value})
        ),
        declarations=sum(1 for _ in nodes_of(view, proof_id, "FormalDeclaration")),
        pending_certificates=count("Certificate", PENDING_CERTIFICATES),
    )


def run_declaration(view: MemoryView, run_id: str) -> str | None:
    """The declaration a formal run searches, if it names exactly one."""
    targets = {edge.dst_id for edge in view.edges_from(run_id, "SEARCHES")}
    return next(iter(targets)) if len(targets) == 1 else None


def obstructions_by_run(view: MemoryView, proof_id: str) -> dict[str, list[NodeRecord]]:
    raised: dict[str, list[NodeRecord]] = defaultdict(list)
    for run in nodes_of(view, proof_id, "FormalRun"):
        for edge in view.edges_from(run.node_id, "RAISED_OBSTRUCTION"):
            obstruction = view.node(edge.dst_id)
            if obstruction is not None:
                raised[run.node_id].append(obstruction)
    return raised


def residual_of(view: MemoryView, obstruction_id: str) -> tuple[str, str] | None:
    """The state an obstruction is stuck at: its id and semantic signature."""
    for edge in view.edges_from(obstruction_id, "AT_STATE"):
        state = view.node(edge.dst_id)
        if state is not None:
            return state.node_id, state.fields.get("semantic_signature") or state.node_id
    return None


def misaligned_declarations(view: MemoryView, proof_id: str) -> dict[str, str]:
    """Declarations a reviewed alignment says do not state the claim."""
    unsound = {
        AlignmentVerdict.MISMATCH.value,
        AlignmentVerdict.WEAKER.value,
        AlignmentVerdict.STRONGER.value,
        AlignmentVerdict.AMBIGUOUS.value,
    }
    found: dict[str, str] = {}
    for alignment in nodes_of(view, proof_id, "Alignment"):
        verdict = alignment.fields.get("verdict")
        if alignment.fields.get("lifecycle") != "reviewed" or verdict not in unsound:
            continue
        for edge in view.edges_from(alignment.node_id, "ALIGNS_DECLARATION"):
            found.setdefault(edge.dst_id, verdict)
    return found


def refuted_reviews(view: MemoryView, proof_id: str) -> Iterator[tuple[NodeRecord, str]]:
    """Critic attempts that refuted a claim still waiting on a verdict."""
    for attempt in nodes_of(view, proof_id, "Attempt"):
        if attempt.fields.get("worker_class") != "critic" or attempt.fields.get("status") != "refuted":
            continue
        for edge in view.edges_from(attempt.node_id, "REVIEWS_CLAIM"):
            claim = view.node(edge.dst_id)
            if claim is not None and claim.fields.get("status") == ClaimStatus.PROVISIONAL.value:
                yield attempt, claim.node_id


def coordinator_moves(view: MemoryView, proof_id: str) -> Iterator[NodeRecord]:
    """Every move the coordinator has proposed, worked or not."""
    for state in nodes_of(view, proof_id, "ResearchState"):
        if state.fields.get("role") != COORDINATOR_ROLE:
            continue
        for edge in view.edges_from(state.node_id, "PROPOSES"):
            move = view.node(edge.dst_id)
            if move is not None and move.fields.get("escalation"):
                yield move
