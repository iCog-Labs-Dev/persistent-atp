"""Lease and record a critic review through the commit gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from artifacts.hashing import hash_content

from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import JournalOverlayView, ReadView
from commit_gate.vocab import WorkerClass
from mathproof.ids import IdType, parse_local_id
from mathproof.scheduler import GlobalScheduler, Lease
from mathproof.workers import CriticWorker


@dataclass(frozen=True, slots=True)
class CriticCycleResult:
    lease: Lease
    review: Mapping[str, Any]
    commit: CommitResult


class ArtifactReader(Protocol):
    def get(self, artifact_hash: str) -> bytes: ...


def _current_view(gate: CommitGate, proof_id: str) -> ReadView:
    revision, snapshot = gate.view.snapshot(proof_id)
    head = gate.store.head(proof_id)[0]
    if type(revision) is not int or not 0 <= revision <= head:
        raise ValueError("critic read view has an invalid journal revision")
    events = gate.store.read_events_between(proof_id, revision, head)
    ops = tuple(op for event in events for op in Proposal.from_dict(event).ops)
    return JournalOverlayView(snapshot, head, ops)


def _context(view: ReadView, lease: Lease, artifacts: ArtifactReader | None) -> dict[str, Any]:
    claim_id = lease.selected_move_id
    claim = view.node(claim_id)
    if (
        claim is None
        or claim.label != "Claim"
        or not claim_id.startswith(f"{lease.proof_id}/")
        or claim.fields.get("status") != "provisional"
    ):
        raise ValueError("critic lease must target a provisional claim in its proof")

    def related(rel_type: str, incoming: bool = False) -> list[dict[str, Any]]:
        edges = view.edges_to(claim_id, rel_type) if incoming else view.edges_from(claim_id, rel_type)
        records = []
        for edge in edges:
            node_id = edge.src_id if incoming else edge.dst_id
            node = view.node(node_id)
            if node is None or not node_id.startswith(f"{lease.proof_id}/"):
                raise ValueError(f"critic context has a missing or out-of-proof {rel_type} source")
            records.append({"id": node_id, "fields": dict(node.fields)})
        return sorted(records, key=lambda record: record["id"])

    evidence = []
    for field in ("argument", "proof_sketch"):
        value = claim.fields.get(field)
        if isinstance(value, str) and value.strip():
            if len(value) > 32_000:
                raise ValueError(f"critic {field} exceeds the 32,000 character input limit")
            evidence.append({"id": f"{claim_id}#{field}", "text": value.strip()})
    for edge in view.edges_from(claim_id, "HAS_ARTIFACT"):
        artifact = view.node(edge.dst_id)
        if artifact is None or artifact.label != "Artifact":
            raise ValueError("claim evidence artifact is missing from the graph")
        digest = artifact.fields.get("artifact_hash", artifact.node_id)
        if not isinstance(digest, str) or artifacts is None:
            raise ValueError("critic requires an artifact reader for linked evidence")
        raw = artifacts.get(digest)
        if len(raw) > 64_000 or hash_content(raw) != digest:
            raise ValueError("critic evidence artifact exceeds its limit or fails integrity check")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("critic evidence artifact must be UTF-8 text") from exc
        evidence.append({"id": digest, "text": content})
    dependencies = related("DEPENDS_ON")
    established = {"critic-accepted", "formally-closed", "lean-verified"}
    return {
        "claim": {"id": claim_id, "fields": dict(claim.fields)},
        "dependencies": dependencies,
        "dependencies_established": all(
            item["fields"].get("status") in established for item in dependencies
        ),
        "prior_reviews": related("REVIEWS_CLAIM", incoming=True),
        "alignments": related("ALIGNS_CLAIM", incoming=True),
        "evidence": evidence,
    }


def _attempt_id(view: ReadView, proof_id: str) -> str:
    highest = 0
    for node in view.list_nodes(proof_id, "Attempt"):
        try:
            kind, serial = parse_local_id(node.node_id.removeprefix(f"{proof_id}/"))
        except ValueError:
            continue
        if kind == IdType.ATTEMPT:
            highest = max(highest, serial)
    return f"{proof_id}/{IdType.ATTEMPT.value}-{highest + 1}"


def run_critic_cycle(
    proof_id: str,
    *,
    actor: str,
    scheduler: GlobalScheduler,
    worker: CriticWorker,
    gate: CommitGate,
    artifacts: ArtifactReader | None = None,
) -> CriticCycleResult | None:
    """Review one eligible claim, or return None when no critic work is free."""
    lease = scheduler.lease_next(
        proof_id, WorkerClass.CRITIC.value, actor=actor, category="critic-task"
    )
    if lease is None:
        return None
    try:
        view = _current_view(gate, proof_id)
        context = _context(view, lease, artifacts)
        review = worker.run(lease, context)
        attempt_id = _attempt_id(view, proof_id)
        proposal = Proposal(
            proof_id=proof_id,
            actor=actor,
            worker_class=WorkerClass.CRITIC.value,
            base_revision=lease.base_revision,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            selected_move_id=lease.selected_move_id,
            ops=(
                UpsertNode("Attempt", attempt_id, {
                    "actor": actor,
                    "worker_class": WorkerClass.CRITIC.value,
                    "status": review["verdict"],
                    "reasoning": review["reasoning"],
                    "evidence_ids": review["evidence_ids"],
                    "earliest_gap": review["earliest_gap"],
                    "counterexample": review["counterexample"],
                    "repair": review["repair"],
                    "missing_context": review["missing_context"],
                }),
                AddEdge("REVIEWS_CLAIM", attempt_id, lease.selected_move_id, f"{attempt_id}-reviews"),
            ),
        )
        commit = gate.commit(proposal)
        scheduler.update_statistics(commit, "critic-task")
        return CriticCycleResult(lease, review, commit)
    finally:
        scheduler.release(proof_id, lease.lease_id, fencing_token=lease.fencing_token)
