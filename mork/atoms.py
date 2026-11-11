"""Canonical proof-graph atoms shared by file and live MORK projection."""

from __future__ import annotations

import json
from typing import Any, Collection, Mapping

__all__ = [
    "encode",
    "extract_local_id",
    "extract_proof_id",
    "node_atoms",
    "edge_atoms",
    "field_atom",
    "projected_atom",
    "derive_state_id",
    "PROJECTED_SCHEMA_VERSION",
    "to_add_command",
]

_MAX_TOKEN_BYTES = 56
_MAX_CHUNK_BYTES = 48

#: Bumped whenever the projected atom set changes shape; a viewer must refuse
#: to treat a space published under a newer schema as valid (NFR-008).
PROJECTED_SCHEMA_VERSION = 1


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
    # MORK encodes expression arity in six bits. Keep the marker plus its
    # children below 64 even for multi-kilobyte values; flat lists corrupt
    # the parser's representation once they contain 63+ chunks.
    parts = [json.dumps(chunk, ensure_ascii=False) for chunk in chunks]
    while len(parts) > 62:
        parts = ["(json-chunks " + " ".join(parts[i:i + 62]) + ")"
                 for i in range(0, len(parts), 62)]
    return "(json-chunks " + " ".join(parts) + ")"


def extract_proof_id(identity: str) -> str:
    """Return the proof prefix from ``<proof>/<local>``."""
    return identity.split("/", 1)[0]


def extract_local_id(identity: str) -> str:
    """Return the local portion of ``<proof>/<local>``."""
    return identity.split("/", 1)[1] if "/" in identity else identity


def derive_state_id(candidates: Collection[str]) -> str | None:
    """Projected ``state_id`` from candidate exact-state references.

    Exactly one candidate resolves to that state id; zero or several are
    ambiguous and resolve to nothing. Shared by the live MORK view, the
    in-memory view, and the batch projector so all three derive identical
    fields for identical graphs.
    """
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


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
        # Selective outgoing index: `edges_from` matches on the fixed
        # (proof, src, rel) prefix; the variable-early `edge` pattern
        # scans every edge of the proof (bench E2: linear growth).
        f"(out-edge {encode(proof_id)} {encode(local_src)} "
        f"{encode(rel_type)} {encode(local_dst)} {encode(local_edge_id)})",
        f'(layer {encode(proof_id)} {encode(f"edge:{local_edge_id}")} "committed")',
    ]
    atoms.extend(
        field_atom("efield", proof_id, local_edge_id, name, value)
        for name, value in sorted(dict(fields or {}).items())
    )
    return tuple(atoms)


def projected_atom(
    proof_id: str, revision: int, schema_version: int = PROJECTED_SCHEMA_VERSION
) -> str:
    """Build the live projection watermark atom."""
    return (
        f"(projected {encode(proof_id)} {encode(revision)} "
        f"{encode(schema_version)})"
    )


def to_add_command(atom: str) -> str:
    """Wrap a raw atom for a generated MeTTa projection file."""
    return f"!(add-atom &mork {atom})"
