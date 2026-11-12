"""Role-specific context packets (Section 9.3 / Context Compilation).

Workers never see the whole workspace: the compiler projects committed state
into the narrow packet a role needs, and stamps every packet with a manifest
digest so what a worker was told stays reproducible.
"""

from __future__ import annotations

from typing import Any, Mapping

from commit_gate.canon import content_hash
from commit_gate.state import ReadView
from commit_gate.vocab import ClaimStatus
from .scheduler import Lease

__all__ = [
    "compile_context",
    "compile_formal_request",
]


def compile_context(lease: Lease, view: ReadView) -> dict[str, Any]:
    """The packet for a non-formal worker class, keyed by its role."""
    if lease.worker_class == "critic":
        return _critic_packet(lease, view)
    return _research_packet(lease, view)


def manifest_digest(packet: Mapping[str, Any]) -> str:
    return content_hash(dict(packet))


# -- research packet (Explorer) ---------------------------------------------


def _research_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    move = view.node(lease.selected_move_id)
    packet: dict[str, Any] = {
        "role": "research",
        "proof_id": lease.proof_id,
        "move": _node_payload(move),
        "parent_state": None,
        "claims": [],
        "obstructions": [],
    }
    if move is not None:
        parent = view.edges_to(move.node_id, "PROPOSES")
        if parent:
            state = view.node(parent[0].src_id)
            packet["parent_state"] = _node_payload(state)
    for node in _all_nodes(view):
        if node["label"] == "Claim" and node["fields"].get("status") in (
            ClaimStatus.CONJECTURAL.value,
            ClaimStatus.PROVISIONAL.value,
            ClaimStatus.CRITIC_ACCEPTED.value,
        ):
            packet["claims"].append(node)
        elif node["label"] == "Obstruction":
            packet["obstructions"].append(node)
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


def _critic_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    claim = view.node(lease.selected_move_id)
    packet: dict[str, Any] = {
        "role": "critic",
        "proof_id": lease.proof_id,
        "claim": _node_payload(claim),
        "dependencies": [],
        "evidence": [],
        "prior_reviews": [],
    }
    if claim is not None:
        for edge in view.edges_from(claim.node_id, "DEPENDS_ON"):
            packet["dependencies"].append(_node_payload(view.node(edge.dst_id)))
        for edge in view.edges_to(claim.node_id, "REVIEWS_CLAIM"):
            packet["prior_reviews"].append(_node_payload(view.node(edge.src_id)))
        for edge_type in ("PROVED_BY", "RESOLVES"):
            for edge in view.edges_from(claim.node_id, edge_type):
                packet["evidence"].append({"rel": edge_type, **_node_payload(view.node(edge.dst_id))})
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


# -- formal request -----------------------------------------------------------


def compile_formal_request(
    lease: Lease,
    view: ReadView,
    *,
    run_id: str | None = None,
    search_policy: str = "gnn-pln-best-first-v1",
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A formal-search-request for the leased declaration or resumable run.

    Reads only committed state: the declaration's Lean text, the pinned
    environment, and the run identity. `run_id` must be provided for a fresh
    declaration; a leased `fr-` move resumes that run as-is.
    """
    target_id = lease.selected_move_id
    target = view.node(target_id)
    if target is None:
        raise ValueError(f"leased formal target {target_id!r} is not committed")

    if target.label == "FormalRun":
        run_id = target_id
        searched = view.edges_from(run_id, "SEARCHES")
        if not searched:
            raise ValueError(f"run {run_id!r} commits no SEARCHES edge")
        declaration_id = searched[0].dst_id
        declaration = view.node(declaration_id)
    elif target.label == "FormalDeclaration":
        if run_id is None:
            raise ValueError("a fresh declaration needs a minted run_id")
        declaration_id = target_id
        declaration = target
    else:
        raise ValueError(
            f"{target_id!r} is a {target.label}, not a formal-run target"
        )

    environment = _pinned_environment(view, declaration_id)

    request: dict[str, Any] = {
        "proof_id": lease.proof_id,
        "claim_id": _aligned_claim(view, declaration_id),
        "formal_declaration_id": declaration_id,
        "run_id": run_id,
        "base_revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "fencing_token": lease.fencing_token,
        "lean_source_artifact": (declaration.fields or {}).get("lean_value", ""),
        "environment_id": environment["environment_id"],
        "environment_hash": environment["environment_hash"],
        "goal_text": (declaration.fields or {}).get("lean_type", ""),
        "search_policy": search_policy,
    }
    if budget is not None:
        request["budget"] = dict(budget)
    return request


def _aligned_claim(view: ReadView, declaration_id: str) -> str | None:
    for align_edge in view.edges_to(declaration_id, "ALIGNS_DECLARATION"):
        alignment = view.node(align_edge.src_id)
        if alignment is None:
            continue
        for claim_edge in view.edges_from(alignment.node_id, "ALIGNS_CLAIM"):
            return claim_edge.dst_id
    return None


def _pinned_environment(view: ReadView, declaration_id: str) -> dict[str, Any]:
    for edge in view.edges_from(declaration_id, "PINNED_ENVIRONMENT"):
        env = view.node(edge.dst_id)
        if env is not None:
            fields = dict(env.fields or {})
            return {
                "environment_id": env.node_id,
                "environment_hash": fields.get(
                    "lake_manifest_hash", ""
                ),
                "toolchain": fields.get("toolchain"),
            }
    return {"environment_id": "", "environment_hash": "", "toolchain": None}


# -- shared helpers -----------------------------------------------------------


def _node_payload(record) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "id": record.node_id,
        "label": record.label,
        "fields": dict(record.fields or {}),
    }


def _all_nodes(view: ReadView) -> list[dict[str, Any]]:
    nodes = getattr(view, "nodes", {})
    payloads = []
    for record in nodes.values():
        payloads.append(_node_payload(record))
    return payloads
