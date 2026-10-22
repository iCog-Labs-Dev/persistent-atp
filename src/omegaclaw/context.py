"""Reference theorem kernel and dispatch packet bound to a journal snapshot."""
from __future__ import annotations

import json

from artifacts.hashing import hash_content
from commit_gate.canon import canonical_json

COMPILER_VERSION = "reference-kernel-v2"


def compile_packet(proof_id: str, lease, view, artifacts) -> bytes:
    revision, snapshot = view.snapshot(proof_id)
    if revision != lease.base_revision:
        raise ValueError("snapshot revision changed before dispatch")
    proof = snapshot.node(f"{proof_id}/proof")
    claim = snapshot.node(f"{proof_id}/c-1")
    if proof is None or claim is None:
        raise ValueError("theorem kernel is incomplete")
    pointer = proof.fields["target_artifact"]
    literal = artifacts.get(pointer).decode("utf-8")
    if literal != claim.fields["claim_text"] or pointer != claim.fields["literal_target_hash"]:
        raise ValueError("literal target artifact differs from committed claim")
    selected = snapshot.node(lease.selected_move_id)
    if selected is None:
        raise ValueError("selected move is absent from the committed snapshot")
    strategy = ""
    alternatives: list[str] = []
    if selected.label == "ResearchMove":
        strategy = str(selected.fields.get("strategy", ""))
        parent_ids = {edge.src_id for edge in snapshot.edges_to(selected.node_id, "PROPOSES")}
        alternatives = sorted({edge.dst_id for parent_id in parent_ids
            for edge in snapshot.edges_from(parent_id, "PROPOSES")
            if edge.dst_id != selected.node_id
            and (other := snapshot.node(edge.dst_id)) is not None
            and other.label == "ResearchMove"
            and other.fields.get("status") in ("open", "queued")})
    packet = {"schema_version": 1, "compiler_version": COMPILER_VERSION,
        "proof_id": proof_id, "claim_id": claim.node_id,
        "literal_target": literal, "theorem_hash": pointer,
        "assumptions": [], "permitted_results": [], "speculative_dependencies": [],
        "selected_move_id": lease.selected_move_id,
        "worker_class": lease.worker_class, "actor": lease.actor,
        "lease_id": lease.lease_id, "snapshot_revision": revision,
        "selected_entity_label": selected.label,
        "selected_entity_status": selected.fields.get("status", ""),
        "selected_strategy": strategy,
        "viable_alternative_ids": alternatives,
        "retrieval_channels": [], "inclusion_reasons": ["registered-literal-target", "selected-committed-move"]}
    packet["packet_digest"] = hash_content(canonical_json(packet))
    return canonical_json(packet)


def verify_packet(content: bytes, *, lease, view, artifacts) -> dict:
    packet = json.loads(content)
    digest = packet.pop("packet_digest", None)
    if digest != hash_content(canonical_json(packet)):
        raise ValueError("packet digest mismatch")
    expected = json.loads(compile_packet(lease.proof_id, lease, view, artifacts))
    if {**packet, "packet_digest": digest} != expected:
        raise ValueError("packet differs from committed theorem or trusted dispatch")
    return expected
