"""Canonical proof-graph atoms shared by file and live MORK projection."""

from __future__ import annotations

import json
from typing import Any, Mapping

__all__ = [
    "encode",
    "extract_local_id",
    "extract_proof_id",
    "node_atoms",
    "edge_atoms",
    "field_atom",
    "projected_atom",
    "to_add_command",
]

_MAX_TOKEN_BYTES = 56
_MAX_CHUNK_BYTES = 48


def encode(value: Any) -> str:
    """Encode one atom slot without losing type or long values.

    MORK truncates an individual textual token once its encoded form approaches
    64 bytes. Longer JSON is therefore represented as one nested expression of
    safely-sized string chunks. It remains a single matchable atom slot.
    """
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) <= _MAX_TOKEN_BYTES:
        return serialized

    chunks: list[str] = []
    current = ""
    for character in serialized:
        candidate = current + character
        encoded = json.dumps(candidate, ensure_ascii=False)
        if current and len(encoded.encode("utf-8")) > _MAX_CHUNK_BYTES:
            chunks.append(current)
            current = character
        else:
            current = candidate
    if current:
        chunks.append(current)
    return "(json-chunks " + " ".join(
        json.dumps(chunk, ensure_ascii=False) for chunk in chunks
    ) + ")"


def extract_proof_id(identity: str) -> str:
    """Return the proof prefix from ``<proof>/<local>``."""
    return identity.split("/", 1)[0]


def extract_local_id(identity: str) -> str:
    """Return the local portion of ``<proof>/<local>``."""
    return identity.split("/", 1)[1] if "/" in identity else identity


def field_atom(
    kind: str, proof_id: str, local_id: str, name: str, value: Any
) -> str:
    """Build a node or edge field atom."""
    return (
        f"({kind} {encode(proof_id)} {encode(local_id)} "
        f"{encode(name)} {encode(value)})"
    )


def node_atoms(
    node_id: str,
    label: str,
    fields: Mapping[str, Any] | None = None,
    *,
    derived_state_id: str | None = None,
) -> tuple[str, ...]:
    """Canonical atoms for one committed node.

    ``state_id`` is projection-derived from active ``PROPOSES`` or ``ON_STATE``
    edges. When supplied it deliberately replaces an explicit value so both
    projection paths expose one unambiguous field atom.
    """
    proof_id = extract_proof_id(node_id)
    local_id = extract_local_id(node_id)
    effective_fields = dict(fields or {})
    if derived_state_id is not None:
        effective_fields["state_id"] = derived_state_id

    atoms = [
        f"(node {encode(proof_id)} {encode(local_id)} {encode(label)})",
        f'(layer {encode(proof_id)} {encode(local_id)} "committed")',
    ]
    atoms.extend(
        field_atom("field", proof_id, local_id, name, effective_fields[name])
        for name in sorted(effective_fields)
    )
    return tuple(atoms)


def edge_atoms(
    rel_type: str,
    src_id: str,
    dst_id: str,
    edge_id: str,
    fields: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Canonical forward, reverse-index, layer, and field atoms for an edge."""
    proof_id = extract_proof_id(edge_id)
    local_edge_id = extract_local_id(edge_id)
    local_src = extract_local_id(src_id)
    local_dst = extract_local_id(dst_id)

    atoms = [
        f"(edge {encode(proof_id)} {encode(local_edge_id)} "
        f"{encode(rel_type)} {encode(local_src)} {encode(local_dst)})",
        f"(rev-edge {encode(proof_id)} {encode(local_dst)} "
        f"{encode(rel_type)} {encode(local_src)} {encode(local_edge_id)})",
        f'(layer {encode(proof_id)} {encode(f"edge:{local_edge_id}")} "committed")',
    ]
    atoms.extend(
        field_atom("efield", proof_id, local_edge_id, name, value)
        for name, value in sorted(dict(fields or {}).items())
    )
    return tuple(atoms)


def projected_atom(proof_id: str, revision: int) -> str:
    """Build the live projection watermark atom."""
    return f"(projected {encode(proof_id)} {encode(revision)})"


def to_add_command(atom: str) -> str:
    """Wrap a raw atom for a generated MeTTa projection file."""
    return f"!(add-atom &mork {atom})"
