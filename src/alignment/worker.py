"""Alignment Worker for reviewing and proposing statement alignment (§5.4, §7.6)."""

from __future__ import annotations

from typing import Any

from commit_gate.ops import AddEdge, Op, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.vocab import WorkerClass

from .models import AlignmentReviewRequest, AlignmentReviewResult
from .reviewer import AlignmentReviewerProtocol, RuleBasedAlignmentReviewer

__all__ = [
    "AlignmentWorker",
    "build_alignment_proposal",
    "build_worker_result",
]


def build_alignment_proposal(
    *,
    proof_id: str,
    alignment_id: str,
    request: AlignmentReviewRequest,
    result: AlignmentReviewResult,
    actor: str,
    base_revision: int | None = None,
    lease_id: str | None = None,
    fencing_token: int | None = None,
    existing_alignment: bool = False,
) -> Proposal:
    """Build a CommitGate Proposal recording the alignment review outcome."""
    ops: list[Op] = []

    if not existing_alignment:
        # Initial upsert of the Alignment node
        fields: dict[str, Any] = {
            "actor": actor,
            "verdict": result.verdict.value,
            "lifecycle": result.lifecycle.value,
            "statement_hash": request.statement_hash,
            "source_hash": request.source_hash,
        }
        ops.append(UpsertNode(label="Alignment", node_id=alignment_id, fields=fields))
        ops.append(
            AddEdge(
                rel_type="ALIGNS_CLAIM",
                src_id=alignment_id,
                dst_id=request.claim_id,
                edge_id=f"{alignment_id}->{request.claim_id}:ALIGNS_CLAIM",
            )
        )
        ops.append(
            AddEdge(
                rel_type="ALIGNS_DECLARATION",
                src_id=alignment_id,
                dst_id=request.declaration_id,
                edge_id=f"{alignment_id}->{request.declaration_id}:ALIGNS_DECLARATION",
            )
        )
    else:
        # If alignment record was already created as draft/review-needed, update lifecycle
        ops.append(
            SetField(
                label="Alignment",
                node_id=alignment_id,
                field="lifecycle",
                value=result.lifecycle.value,
                prior="review-needed",
            )
        )

    return Proposal(
        proof_id=proof_id,
        actor=actor,
        worker_class=WorkerClass.ALIGNMENT_REVIEWER.value,
        base_revision=base_revision,
        lease_id=lease_id,
        fencing_token=fencing_token,
        ops=tuple(ops),
    )


class AlignmentWorker:
    """Worker executing alignment reviews and generating validated proposals."""

    def __init__(
        self,
        actor: str = "alignment-worker-1",
        reviewer: AlignmentReviewerProtocol | None = None,
    ) -> None:
        self.actor = actor
        self.reviewer = reviewer or RuleBasedAlignmentReviewer(reviewer_id=actor)

    def run_review(
        self,
        request: AlignmentReviewRequest,
        alignment_id: str,
        *,
        base_revision: int | None = None,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        existing_alignment: bool = False,
    ) -> tuple[AlignmentReviewResult, Proposal]:
        """Perform review on request and return both result and ready proposal."""
        result = self.reviewer.review(request)
        proposal = build_alignment_proposal(
            proof_id=request.proof_id,
            alignment_id=alignment_id,
            request=request,
            result=result,
            actor=self.actor,
            base_revision=base_revision,
            lease_id=lease_id,
            fencing_token=fencing_token,
            existing_alignment=existing_alignment,
        )
        return result, proposal

    def create_record(
        self,
        request: AlignmentReviewRequest,
        result: AlignmentReviewResult,
        alignment_id: str,
    ) -> AlignmentRecord:
        """Create a full schema-compliant AlignmentRecord from request and result."""
        from .models import AlignmentRecord

        return AlignmentRecord(
            alignment_id=alignment_id,
            claim_id=request.claim_id,
            declaration_id=request.declaration_id,
            statement_hash=request.statement_hash,
            source_hash=request.source_hash,
            reviewer=self.actor,
            verdict=result.verdict,
            lifecycle=result.lifecycle,
            criteria=result.criteria,
            imports=request.imports,
            namespace=request.namespace,
        )


def build_worker_result(
    *,
    attempt_id: str,
    proof_id: str,
    result: AlignmentReviewResult,
    request: AlignmentReviewRequest,
    base_revision: int | None = None,
    lease_id: str | None = None,
    fencing_token: int | None = None,
) -> dict[str, Any]:
    """Format worker result adhering to schemas/worker-result.schema.json."""
    from commit_gate.vocab import EvidenceKind

    payload: dict[str, Any] = {
        "result_id": attempt_id,
        "worker_class": WorkerClass.ALIGNMENT_REVIEWER.value,
        "proof_id": proof_id,
        "evidence_kind": EvidenceKind.CRITIC_REVIEW.value,
        "summary": result.reasoning or f"Alignment review: {result.verdict.value}",
    }
    if base_revision is not None:
        payload["base_revision"] = base_revision
    if lease_id is not None:
        payload["lease_id"] = lease_id
    if fencing_token is not None:
        payload["fencing_token"] = fencing_token
    if request.source_hash:
        payload["artifact_hashes"] = [request.source_hash]
    return payload

