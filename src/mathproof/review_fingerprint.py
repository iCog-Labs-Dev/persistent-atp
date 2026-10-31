"""Stable identity of the claim material a critic is asked to review."""

from __future__ import annotations

from commit_gate.canon import content_hash
from commit_gate.state import ReadView


def critic_input_digest(view: ReadView, claim_id: str) -> str:
    """Ignore prior reviews, which must not make an unchanged claim look new."""
    claim = view.node(claim_id)
    related = {}
    for rel_type in ("DEPENDS_ON", "PROVED_BY", "RESOLVES"):
        records = {
            edge.dst_id: {"label": record.label, "fields": dict(record.fields)}
            for edge in view.edges_from(claim_id, rel_type)
            if (record := view.node(edge.dst_id)) is not None
        }
        related[rel_type] = [(node_id, records[node_id]) for node_id in sorted(records)]
    for rel_type in ("ALIGNS_CLAIM", "CHECKS_CLAIM", "DRAFTS_CLAIM"):
        records = {
            edge.src_id: {"label": record.label, "fields": dict(record.fields)}
            for edge in view.edges_to(claim_id, rel_type)
            if (record := view.node(edge.src_id)) is not None
        }
        related[rel_type] = [(node_id, records[node_id]) for node_id in sorted(records)]
    return content_hash({
        "claim_id": claim_id,
        "claim": dict(claim.fields) if claim is not None else None,
        "related": related,
    })
