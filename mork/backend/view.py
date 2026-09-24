"""The commit gate's live graph contract backed by the MORK FFI space."""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping

from commit_gate.state import EdgeRecord, NodeRecord
from mork.atoms import (
    edge_atoms,
    encode,
    extract_local_id,
    extract_proof_id,
    field_atom,
    node_atoms,
    projected_atom,
)

from .ffi import MorkSpace

__all__ = ["MorkView", "decode", "encode", "tokens"]


def decode(token: str) -> Any:
    """Decode one JSON atom slot, preserving hand-authored bare symbols."""
    if token.startswith("(json-chunks ") and token.endswith(")"):
        parts = tokens(token)
        if parts and parts[0] == "json-chunks":
            try:
                serialized = "".join(json.loads(part) for part in parts[1:])
                return json.loads(serialized)
            except (json.JSONDecodeError, TypeError):
                return token
    try:
        return json.loads(token)
    except json.JSONDecodeError:
        return token


def tokens(sexpr: str) -> list[str]:
    """Split the top-level slots of one s-expression, respecting quoting."""
    text = sexpr.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]

    found: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    escaped = False

    for character in text:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif in_string:
            current.append(character)
            if character == '"':
                in_string = False
        elif character == '"':
            current.append(character)
            in_string = True
        elif character == "(":
            depth += 1
            current.append(character)
        elif character == ")":
            depth -= 1
            current.append(character)
        elif character.isspace() and depth == 0:
            if current:
                found.append("".join(current))
                current = []
        else:
            current.append(character)

    if current:
        found.append("".join(current))
    return found


class MorkView:
    """Read and write all proofs in the process-wide MORK space.

    Every graph atom is proof-scoped. Node and edge writes also maintain the
    committed-layer atoms and reverse-edge index used by the current rule set.
    """

    def __init__(self, space: MorkSpace):
        self._space = space

    # ---------------------------------------------------------------- reads

    def node(self, node_id: str) -> NodeRecord | None:
        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        labels = self._space.match(
            f"(node {encode(proof_id)} {encode(local_id)} $label)", "$label"
        )
        if not labels:
            return None
        return NodeRecord(
            node_id,
            decode(labels[0]),
            dict(self._read_fields("field", proof_id, local_id)),
        )

    def edge(self, edge_id: str) -> EdgeRecord | None:
        proof_id = extract_proof_id(edge_id)
        local_id = extract_local_id(edge_id)
        rows = self._space.match(
            f"(edge {encode(proof_id)} {encode(local_id)} $rel $src $dst)",
            "($rel $src $dst)",
        )
        if not rows:
            return None
        rel_type, src, dst = (decode(token) for token in tokens(rows[0]))
        return self._edge_record(proof_id, local_id, rel_type, src, dst)

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        proof_id = extract_proof_id(node_id)
        local_src = extract_local_id(node_id)
        rows = self._space.match(
            f"(edge {encode(proof_id)} $eid {encode(rel_type)} "
            f"{encode(local_src)} $dst)",
            "($eid $dst)",
        )
        records = []
        for row in rows:
            edge_token, dst_token = tokens(row)
            records.append(
                self._edge_record(
                    proof_id,
                    decode(edge_token),
                    rel_type,
                    local_src,
                    decode(dst_token),
                )
            )
        return tuple(records)

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        """Read incoming edges through the materialized reverse-edge index."""
        proof_id = extract_proof_id(node_id)
        local_dst = extract_local_id(node_id)
        rows = self._space.match(
            f"(rev-edge {encode(proof_id)} {encode(local_dst)} "
            f"{encode(rel_type)} $src $eid)",
            "($eid $src)",
        )
        records = []
        for row in rows:
            edge_token, src_token = tokens(row)
            records.append(
                self._edge_record(
                    proof_id,
                    decode(edge_token),
                    rel_type,
                    decode(src_token),
                    local_dst,
                )
            )
        return tuple(records)

    def atoms(self, proof_id: str) -> list[str]:
        """Every canonical graph atom for one proof, sorted."""
        proof = encode(proof_id)
        shapes = [
            f"(node {proof} $id $label)",
            f"(field {proof} $id $name $value)",
            f"(edge {proof} $eid $rel $src $dst)",
            f"(rev-edge {proof} $dst $rel $src $eid)",
            f"(efield {proof} $eid $name $value)",
            f"(layer {proof} $entity $kind)",
            f"(projected {proof} $revision)",
        ]
        return sorted(
            atom for shape in shapes for atom in self._space.match(shape, shape)
        )

    # --------------------------------------------------------------- writes

    def add_node(
        self, node_id: str, label: str, fields: Mapping[str, Any] | None = None
    ) -> None:
        """Replace one node family and restore its derived state reference."""
        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        self._replace(f"(node {encode(proof_id)} {encode(local_id)} $label)")
        self._replace(f"(field {encode(proof_id)} {encode(local_id)} $name $value)")
        self._replace(
            f"(layer {encode(proof_id)} {encode(local_id)} $kind)"
        )
        self._space.add(*node_atoms(node_id, label, fields))
        self._sync_state_reference(node_id)

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        self._write_field("field", proof_id, local_id, name, value)

    def add_edge(
        self,
        rel_type: str,
        src_id: str,
        dst_id: str,
        edge_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Replace an edge and all generated atoms as one logical family."""
        previous = self.edge(edge_id)
        self._remove_edge_family(edge_id)
        self._space.add(*edge_atoms(rel_type, src_id, dst_id, edge_id, fields))

        if previous is not None:
            affected = self._state_reference_node(previous)
            if affected is not None:
                self._sync_state_reference(affected)
        affected = self._state_reference_node(
            EdgeRecord(edge_id, rel_type, src_id, dst_id, dict(fields or {}))
        )
        if affected is not None:
            self._sync_state_reference(affected)

    def remove_edge(self, edge_id: str) -> None:
        previous = self.edge(edge_id)
        self._remove_edge_family(edge_id)
        if previous is not None:
            affected = self._state_reference_node(previous)
            if affected is not None:
                self._sync_state_reference(affected)

    # ----------------------------------------------------------------- mark

    def projected_revision(self, proof_id: str) -> int:
        marks = self._space.match(
            f"(projected {encode(proof_id)} $revision)", "$revision"
        )
        return max((int(decode(mark)) for mark in marks), default=0)

    def record_projected(self, proof_id: str, revision: int) -> None:
        if revision <= self.projected_revision(proof_id):
            return
        self._replace(f"(projected {encode(proof_id)} $revision)")
        self._space.add(projected_atom(proof_id, revision))

    # -------------------------------------------------------------- helpers

    def _read_fields(
        self, kind: str, proof_id: str, local_id: str
    ) -> Iterator[tuple[str, Any]]:
        rows = self._space.match(
            f"({kind} {encode(proof_id)} {encode(local_id)} $name $value)",
            "($name $value)",
        )
        for row in rows:
            name, value = tokens(row)
            yield decode(name), decode(value)

    def _write_field(
        self, kind: str, proof_id: str, local_id: str, name: str, value: Any
    ) -> None:
        self._clear_field(kind, proof_id, local_id, name)
        self._space.add(field_atom(kind, proof_id, local_id, name, value))

    def _clear_field(self, kind: str, proof_id: str, local_id: str, name: str) -> None:
        self._replace(
            f"({kind} {encode(proof_id)} {encode(local_id)} {encode(name)} $value)"
        )

    def _remove_edge_family(self, edge_id: str) -> None:
        proof_id = extract_proof_id(edge_id)
        local_id = extract_local_id(edge_id)
        self._replace(
            f"(edge {encode(proof_id)} {encode(local_id)} $rel $src $dst)"
        )
        self._replace(
            f"(rev-edge {encode(proof_id)} $dst $rel $src {encode(local_id)})"
        )
        self._replace(
            f"(efield {encode(proof_id)} {encode(local_id)} $name $value)"
        )
        self._replace(
            f"(layer {encode(proof_id)} {encode(f'edge:{local_id}')} $kind)"
        )

    def _replace(self, pattern: str) -> None:
        for atom in self._space.match(pattern, pattern):
            self._space.remove(atom)

    def _sync_state_reference(self, node_id: str) -> None:
        node = self.node(node_id)
        if node is None:
            return
        if node.label == "Move":
            candidates = {
                extract_local_id(edge.src_id)
                for edge in self.edges_to(node_id, "PROPOSES")
            }
        elif node.label == "Attempt":
            candidates = {
                extract_local_id(edge.dst_id)
                for edge in self.edges_from(node_id, "ON_STATE")
            }
        else:
            return

        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        if len(candidates) == 1:
            self._write_field(
                "field", proof_id, local_id, "state_id", next(iter(candidates))
            )
        else:
            self._clear_field("field", proof_id, local_id, "state_id")

    @staticmethod
    def _state_reference_node(edge: EdgeRecord) -> str | None:
        if edge.rel_type == "PROPOSES":
            return edge.dst_id
        if edge.rel_type == "ON_STATE":
            return edge.src_id
        return None

    def _edge_record(
        self,
        proof_id: str,
        local_edge_id: str,
        rel_type: str,
        local_src: str,
        local_dst: str,
    ) -> EdgeRecord:
        return EdgeRecord(
            f"{proof_id}/{local_edge_id}",
            rel_type,
            f"{proof_id}/{local_src}",
            f"{proof_id}/{local_dst}",
            dict(self._read_fields("efield", proof_id, local_edge_id)),
        )
