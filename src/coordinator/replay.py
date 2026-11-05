"""Promote evidence after an independently committed replay verdict."""

from __future__ import annotations

from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import SetField
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView
from commit_gate.store import JournalStore

__all__ = ["finalize_accepted_replay"]


def finalize_accepted_replay(
    proof_id: str, *, view: ReadView, store: JournalStore
) -> CommitResult | None:
    """Advance one declaration and claim backed by accepted replay evidence."""
    for certificate in view.list_nodes(proof_id, "Certificate"):
        if certificate.fields.get("status") != "replay-accepted":
            continue
        declarations = view.edges_from(certificate.node_id, "CERTIFIES")
        replays = [
            view.node(edge.dst_id)
            for edge in view.edges_from(certificate.node_id, "REPLAYED_BY")
        ]
        verified = [
            replay for replay in replays
            if replay is not None
            and replay.fields.get("status") == "verified"
            and replay.fields.get("sorry_detected") is False
        ]
        if len(declarations) != 1 or not verified:
            continue
        declaration = view.node(declarations[0].dst_id)
        if declaration is None or declaration.fields.get("status") == "replay-accepted":
            continue

        claims = {
            claim_edge.dst_id
            for alignment_edge in view.edges_to(declaration.node_id, "ALIGNS_DECLARATION")
            for claim_edge in view.edges_from(alignment_edge.src_id, "ALIGNS_CLAIM")
        }
        proved_claims = {
            edge.src_id for edge in view.edges_to(certificate.node_id, "PROVED_BY")
        }
        claim_ids = sorted(claims & proved_claims)
        if len(claim_ids) != 1:
            continue
        claim = view.node(claim_ids[0])
        if claim is None or claim.fields.get("status") not in {
            "critic-accepted", "formally-closed", "lean-verified"
        }:
            continue

        ops = []
        declaration_status = declaration.fields.get("status")
        if declaration_status == "certificate-produced":
            ops.append(SetField(
                "FormalDeclaration", declaration.node_id, "status",
                "replay-pending", prior="certificate-produced",
            ))
            declaration_status = "replay-pending"
        if declaration_status == "replay-pending":
            ops.append(SetField(
                "FormalDeclaration", declaration.node_id, "status",
                "replay-accepted", prior="replay-pending",
            ))

        run_id = certificate.fields.get("producer_run_id")
        if isinstance(run_id, str):
            for edge in view.edges_from(run_id, "HAS_ROOT"):
                root = view.node(edge.dst_id)
                if root is not None and root.fields.get("status") == "formally-closed":
                    ops.append(SetField(
                        "FormalState", root.node_id, "status",
                        "lean-verified", prior="formally-closed",
                    ))

        claim_status = claim.fields.get("status")
        if claim_status == "critic-accepted":
            ops.append(SetField(
                "Claim", claim.node_id, "status",
                "formally-closed", prior="critic-accepted",
            ))
            claim_status = "formally-closed"
        if claim_status == "formally-closed":
            ops.append(SetField(
                "Claim", claim.node_id, "status",
                "lean-verified", prior="formally-closed",
            ))
        if not ops:
            continue

        revision = store.head(proof_id)[0]
        lease_id = f"{proof_id}/replay-finalize-{revision}"
        token = store.acquire_lease(
            proof_id, lease_id, actor="coordinator", worker_class="coordinator"
        )
        try:
            return CommitGate(view, store).commit(Proposal(
                proof_id=proof_id,
                actor="coordinator",
                worker_class="coordinator",
                base_revision=revision,
                lease_id=lease_id,
                fencing_token=token,
                ops=tuple(ops),
            ))
        finally:
            store.release_lease(proof_id, lease_id, fencing_token=token)
    return None
