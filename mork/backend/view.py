"""A `GraphView` backed by the MORK space.

Atom vocabulary, shared with the exporter in `mork/projector/`:

    (node   <proof> <id>  <label>)
    (field  <proof> <id>  <name>  <value>)
    (edge   <proof> <eid> <rel>   <src> <dst>)
    (efield <proof> <eid> <name>  <value>)

Node ids arrive as `<proof>/<local>` and split across the first two slots, using
`projector.core`'s helpers so both paths split them the same way. Every atom then
carries its proof, and one proof's atoms can be matched alone. Edge endpoints are
stored as local ids: an edge does not span proofs.

Every slot is JSON, so a field survives the round trip as the type it was
written with — which compare-and-set depends on, since `prior="0"` must not
match a committed `0`.

Fields are single-valued, and that is the one invariant this module enforces
itself. Because MORK's remove reports success whether or not it matched anything
(see `MorkSpace.remove`), a field is changed by matching the committed atom,
removing exactly the text MORK returned, and only then adding the new one.
Nothing here removes an atom it has not first read back.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping

from commit_gate.state import EdgeRecord, NodeRecord
from mork.projector.core import extract_local_id, extract_proof_id

from .ffi import MorkSpace

__all__ = ["MorkView"]


def encode(value: Any) -> str:
    """One atom slot, as JSON."""
    return json.dumps(value, ensure_ascii=False)


def decode(token: str) -> Any:
    """The Python value an atom slot holds.

    Falls back to the raw token, so a hand-authored atom is surfaced rather than
    crashing a read.
    """
    try:
        return json.loads(token)
    except json.JSONDecodeError:
        return token


def tokens(sexpr: str) -> list[str]:
    """The top-level slots of one s-expression, quoting respected.

    `'(field "p" "s1" "note" "a b")'` gives four tokens, the last still quoted.
    """
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
    """Read and write one MORK space through the gate's graph contract.

    Satisfies `GraphView`, so `apply_ops` projects a journal into MORK by taking
    one of these instead of a `MemoryView`. The underlying space is process-wide,
    so two `MorkView`s share their atoms.
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
        return self._edges(node_id, rel_type, endpoint="src")

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return self._edges(node_id, rel_type, endpoint="dst")

    def atoms(self, proof_id: str) -> list[str]:
        """Every atom under one proof, sorted, mark included. For inspection.

        The space holds every proof at once; this is how one is looked at alone.
        """
        proof = encode(proof_id)
        shapes = [
            f"(node {proof} $id $label)",
            f"(field {proof} $id $name $value)",
            f"(edge {proof} $eid $rel $src $dst)",
            f"(efield {proof} $eid $name $value)",
            f"(projected {proof} $revision)",
        ]
        return sorted(atom for shape in shapes for atom in self._space.match(shape, shape))

    # --------------------------------------------------------------- writes

    def add_node(
        self, node_id: str, label: str, fields: Mapping[str, Any] | None = None
    ) -> None:
        """Record a node, replacing any label and fields already committed.

        Replaces rather than merges, to stay interchangeable with
        `MemoryView.add_node`.
        """
        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        self._replace(f"(node {encode(proof_id)} {encode(local_id)} $label)")
        self._space.add(
            f"(node {encode(proof_id)} {encode(local_id)} {encode(label)})"
        )
        for name, _ in self._read_fields("field", proof_id, local_id):
            if name not in (fields or {}):
                self._clear_field("field", proof_id, local_id, name)
        for name, value in (fields or {}).items():
            self._write_field("field", proof_id, local_id, name, value)

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        """Leave `name` holding exactly `value`, whatever it held before."""
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
        proof_id = extract_proof_id(edge_id)
        local_edge_id = extract_local_id(edge_id)
        local_src = extract_local_id(src_id)
        local_dst = extract_local_id(dst_id)

        self._replace(
            f"(edge {encode(proof_id)} {encode(local_edge_id)} $rel $src $dst)"
        )
        self._space.add(
            f"(edge {encode(proof_id)} {encode(local_edge_id)} "
            f"{encode(rel_type)} {encode(local_src)} {encode(local_dst)})"
        )
        for name, value in (fields or {}).items():
            self._write_field("efield", proof_id, local_edge_id, name, value)

    def remove_edge(self, edge_id: str) -> None:
        """Retract an edge and its fields. A no-op if it is not committed."""
        proof_id = extract_proof_id(edge_id)
        local_edge_id = extract_local_id(edge_id)
        self._replace(
            f"(edge {encode(proof_id)} {encode(local_edge_id)} $rel $src $dst)"
        )
        for name, _ in self._read_fields("efield", proof_id, local_edge_id):
            self._clear_field("efield", proof_id, local_edge_id, name)

    # ----------------------------------------------------------------- mark

    def projected_revision(self, proof_id: str) -> int:
        """How far the journal has been projected here, 0 if never.

        An atom, so it is exactly as durable as the atoms it describes; see
        `WriteView` for why that matters.
        """
        marks = self._space.match(
            f"(projected {encode(proof_id)} $revision)", "$revision"
        )
        return max((int(decode(mark)) for mark in marks), default=0)

    def record_projected(self, proof_id: str, revision: int) -> None:
        if revision <= self.projected_revision(proof_id):
            return
        self._replace(f"(projected {encode(proof_id)} $revision)")
        self._space.add(f"(projected {encode(proof_id)} {encode(revision)})")

    # -------------------------------------------------------------- helpers

    def _read_fields(
        self, kind: str, proof_id: str, local_id: str
    ) -> Iterator[tuple[str, Any]]:
        """Every `(name, value)` under one node's or edge's fields."""
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
        """Replace whatever this slot holds with one value."""
        self._clear_field(kind, proof_id, local_id, name)
        self._space.add(
            f"({kind} {encode(proof_id)} {encode(local_id)} "
            f"{encode(name)} {encode(value)})"
        )

    def _clear_field(self, kind: str, proof_id: str, local_id: str, name: str) -> None:
        self._replace(
            f"({kind} {encode(proof_id)} {encode(local_id)} {encode(name)} $value)"
        )

    def _replace(self, pattern: str) -> None:
        """Remove every atom matching `pattern`, by the text MORK returns.

        Passing the pattern as its own template hands back each atom in full, so
        the removal names bytes MORK has confirmed are present. Reconstructing an
        atom from an expected value would, whenever the guess was wrong, report
        success and leave the stale atom beside its replacement.
        """
        for atom in self._space.match(pattern, pattern):
            self._space.remove(atom)

    def _edges(
        self, node_id: str, rel_type: str, *, endpoint: str
    ) -> tuple[EdgeRecord, ...]:
        """Edges of `rel_type` whose `src` or `dst` is `node_id`."""
        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)
        src = encode(local_id) if endpoint == "src" else "$src"
        dst = encode(local_id) if endpoint == "dst" else "$dst"
        rows = self._space.match(
            f"(edge {encode(proof_id)} $eid {encode(rel_type)} {src} {dst})",
            f"($eid {src} {dst})",
        )

        records = []
        for row in rows:
            edge_token, src_token, dst_token = tokens(row)
            records.append(
                self._edge_record(
                    proof_id,
                    decode(edge_token),
                    rel_type,
                    decode(src_token),
                    decode(dst_token),
                )
            )
        return tuple(records)

    def _edge_record(
        self,
        proof_id: str,
        local_edge_id: str,
        rel_type: str,
        local_src: str,
        local_dst: str,
    ) -> EdgeRecord:
        """One `EdgeRecord`, with local ids restored to full ones."""
        return EdgeRecord(
            f"{proof_id}/{local_edge_id}",
            rel_type,
            f"{proof_id}/{local_src}",
            f"{proof_id}/{local_dst}",
            dict(self._read_fields("efield", proof_id, local_edge_id)),
        )
