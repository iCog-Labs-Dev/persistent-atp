"""The commit gate's live graph contract backed by the MORK FFI space."""

from __future__ import annotations

import json
import inspect
from functools import wraps
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from commit_gate.state import EdgeRecord, MemoryView, NodeRecord, ReadView
from mork.atoms import (
    PROJECTED_SCHEMA_VERSION,
    derive_state_id,
    edge_atoms,
    encode,
    extract_local_id,
    extract_proof_id,
    field_atom,
    node_atoms,
    projected_atom,
)

from .ffi import MorkError, MorkSpace

if TYPE_CHECKING:
    from commit_gate.store import JournalStore

from .queries import QueryOps

__all__ = ["MorkView", "ProjectionUnavailable", "decode", "encode", "tokens"]


class ProjectionUnavailable(MorkError):
    """A projection failed; journal repair or a host restart is required."""


def decode(token: str) -> Any:
    """Decode one JSON atom slot, preserving hand-authored bare symbols."""
    if token.startswith("(json-chunks ") and token.endswith(")"):
        parts = tokens(token)
        if parts and parts[0] == "json-chunks":
            try:
                serialized = _chunk_text(token)
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


def _chunk_text(token: str) -> str:
    if token.startswith("(json-chunks "):
        return "".join(_chunk_text(part) for part in tokens(token)[1:])
    chunk = json.loads(token)
    if not isinstance(chunk, str):
        raise ValueError("JSON chunk is not text")
    return chunk


class MorkView(QueryOps):
    """Read and write all proofs in the process-wide MORK space.

    Every graph atom is proof-scoped. Node and edge writes also maintain the
    committed-layer atoms and reverse-edge index used by the current rule set.

    One reentrant lock serializes every public read and write so a journal
    event applies atomically against readers: the watermark only advances
    after the event's ops are in the space, and no reader can observe a
    partially applied event.
    """

    def __init__(self, space: MorkSpace):
        self._space = space
        self._lock = space.lock

    def _ensure_valid(self, proof_id: str) -> None:
        if self._space.unavailable:
            raise ProjectionUnavailable("the shared Space requires restart and replay")
        if (
            proof_id in self._space.invalid_proofs
            and getattr(self._space.recovery, "proof", None) != proof_id
        ):
            raise ProjectionUnavailable(f"projection for {proof_id!r} requires journal repair")

    # ---------------------------------------------------------------- reads

    def nodes(
        self, proof_id: str, label: str | None = None
    ) -> tuple[NodeRecord, ...]:
        """All nodes in one proof, optionally restricted by label."""
        with self._lock:
            proof = encode(proof_id)
            if label is None:
                rows = self._space.match(
                    f"(node {proof} $id $label)", "($id $label)"
                )
                identities = (
                    tuple(decode(token) for token in tokens(row)) for row in rows
                )
            else:
                rows = self._space.match(
                    f"(node {proof} $id {encode(label)})", "$id"
                )
                identities = ((decode(row), label) for row in rows)

            records = [
                NodeRecord(
                    f"{proof_id}/{local_id}",
                    node_label,
                    dict(self._read_fields("field", proof_id, local_id)),
                )
                for local_id, node_label in identities
            ]
            return tuple(sorted(records, key=lambda record: record.node_id))

    def edges(
        self, proof_id: str, rel_type: str | None = None
    ) -> tuple[EdgeRecord, ...]:
        """All edges in one proof, optionally restricted by relationship type."""
        with self._lock:
            proof = encode(proof_id)
            if rel_type is None:
                rows = self._space.match(
                    f"(edge {proof} $eid $rel $src $dst)",
                    "($eid $rel $src $dst)",
                )
                identities = (
                    tuple(decode(token) for token in tokens(row)) for row in rows
                )
            else:
                rows = self._space.match(
                    f"(edge {proof} $eid {encode(rel_type)} $src $dst)",
                    "($eid $src $dst)",
                )
                identities = (
                    (decoded[0], rel_type, decoded[1], decoded[2])
                    for row in rows
                    if (decoded := tuple(decode(token) for token in tokens(row)))
                )

            records = [
                self._edge_record(
                    proof_id, local_edge, relation, local_src, local_dst
                )
                for local_edge, relation, local_src, local_dst in identities
            ]
            return tuple(sorted(records, key=lambda record: record.edge_id))

    def list_nodes(
        self, proof_id: str, label: str | None = None
    ) -> tuple[NodeRecord, ...]:
        """The shared `ReadView` enumeration; same records as `nodes()`."""
        return self.nodes(proof_id, label)

    def list_edges(
        self, proof_id: str, rel_type: str | None = None
    ) -> tuple[EdgeRecord, ...]:
        """The shared `ReadView` enumeration; same records as `edges()`."""
        return self.edges(proof_id, rel_type)

    def snapshot(self, proof_id: str) -> tuple[int, ReadView]:
        """Copy one proof at a stable projected revision for validation."""
        with self._lock:
            for _ in range(3):
                revision = self.projected_revision(proof_id)
                clone = MemoryView()

                for node in self.nodes(proof_id):
                    clone.add_node(node.node_id, node.label, node.fields)
                for edge in self.edges(proof_id):
                    clone.add_edge(
                        edge.rel_type,
                        edge.src_id,
                        edge.dst_id,
                        edge.edge_id,
                        edge.fields,
                    )

                if revision == self.projected_revision(proof_id):
                    return revision, clone
        raise RuntimeError("MORK projection changed while taking a snapshot")

    def node(self, node_id: str) -> NodeRecord | None:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            proof_id = extract_proof_id(node_id)
            local_src = extract_local_id(node_id)
            rows = self._space.match(
                f"(out-edge {encode(proof_id)} {encode(local_src)} "
                f"{encode(rel_type)} $dst $eid)",
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
        with self._lock:
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
        """Every canonical graph atom for one proof, sorted.

        The watermark is included so parity checks see exactly what is
        published; comparisons that exclude it must say so explicitly.
        """
        with self._lock:
            proof = encode(proof_id)
            shapes = [
                f"(node {proof} $id $label)",
                f"(field {proof} $id $name $value)",
                f"(edge {proof} $eid $rel $src $dst)",
                f"(rev-edge {proof} $dst $rel $src $eid)",
                f"(out-edge {proof} $src $rel $dst $eid)",
                f"(efield {proof} $eid $name $value)",
                f"(layer {proof} $entity $kind)",
                f"(projected {proof} $revision $schema_version)",
            ]
            return sorted(
                atom
                for shape in shapes
                for atom in self._space.match(shape, shape)
            )

    # --------------------------------------------------------------- writes

    def add_node(
        self, node_id: str, label: str, fields: Mapping[str, Any] | None = None
    ) -> None:
        """Replace one node family and restore its derived state reference."""
        with self._lock:
            proof_id = extract_proof_id(node_id)
            local_id = extract_local_id(node_id)
            self._replace(
                f"(node {encode(proof_id)} {encode(local_id)} $label)"
            )
            self._replace(
                f"(field {encode(proof_id)} {encode(local_id)} $name $value)"
            )
            self._replace(
                f"(layer {encode(proof_id)} {encode(local_id)} $kind)"
            )
            self._space.add(*node_atoms(node_id, label, fields))
            self._sync_state_reference(node_id)

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        with self._lock:
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
        with self._lock:
            previous = self.edge(edge_id)
            self._remove_edge_family(edge_id)
            self._space.add(
                *edge_atoms(rel_type, src_id, dst_id, edge_id, fields)
            )

            if previous is not None:
                affected = self._state_reference_node(previous)
                if affected is not None:
                    self._sync_state_reference(affected)
            affected = self._state_reference_node(
                EdgeRecord(
                    edge_id, rel_type, src_id, dst_id, dict(fields or {})
                )
            )
            if affected is not None:
                self._sync_state_reference(affected)

    def remove_edge(self, edge_id: str) -> None:
        with self._lock:
            previous = self.edge(edge_id)
            self._remove_edge_family(edge_id)
            if previous is not None:
                affected = self._state_reference_node(previous)
                if affected is not None:
                    self._sync_state_reference(affected)

    # ------------------------------------------------------------ watermark

    def projected_revision(self, proof_id: str) -> int:
        with self._lock:
            marks = self._space.match(
                f"(projected {encode(proof_id)} $revision $schema_version)",
                "$revision",
            )
            return max((int(decode(mark)) for mark in marks), default=0)

    def projected_schema_version(self, proof_id: str) -> int:
        """The schema version of the current watermark (0 when none)."""
        with self._lock:
            marks = self._space.match(
                f"(projected {encode(proof_id)} $revision $schema_version)",
                "($revision $schema_version)",
            )
            versions = {
                int(decode(tokens(mark)[0])): int(decode(tokens(mark)[1]))
                for mark in marks
            }
            return versions[max(versions)] if versions else 0

    def record_projected(
        self,
        proof_id: str,
        revision: int,
        schema_version: int = PROJECTED_SCHEMA_VERSION,
    ) -> None:
        """Publish the watermark for `revision` after its writes succeeded.

        Watermarks advance strictly in journal order — the new revision must
        be exactly current+1 — and the schema version must never decrease.
        A violation raises instead of advancing, retreating, or duplicating
        the watermark, so a skipped or replayed event can never be marked.
        """
        with self._lock:
            current = self.projected_revision(proof_id)
            if revision != current + 1:
                raise ValueError(
                    f"out-of-order projection revision for {proof_id!r}: "
                    f"watermark is {current}, got {revision}"
                )
            if schema_version < self.projected_schema_version(proof_id):
                raise ValueError(
                    f"projection schema version regression for {proof_id!r}: "
                    f"have {self.projected_schema_version(proof_id)}, "
                    f"got {schema_version}"
                )
            pattern = f"(projected {encode(proof_id)} $revision $schema_version)"
            previous = self._space.match(pattern, pattern)
            try:
                self._replace(pattern)
                self._space.add(projected_atom(proof_id, revision, schema_version))
            except BaseException:
                self._space.invalid_proofs.add(proof_id)
                try:
                    self._replace(pattern)
                    if previous:
                        self._space.add(*previous)
                except BaseException:
                    type(self._space).unavailable = True
                raise

    # --------------------------------------------------------- gate contract

    def apply_journal_event(
        self, store: JournalStore, proof_id: str, revision: int
    ) -> None:
        """Apply the exact next journal event and publish its revision atomically.

        A failed or out-of-order event leaves the watermark unchanged; the
        gate's recovery replays the event (ops are idempotent) before the mark
        advances. The event's operations come from the journal, not a caller.
        """
        from commit_gate.apply import apply_ops
        from commit_gate.proposal import Proposal

        with self._lock:
            current = self.projected_revision(proof_id)
            if revision != current + 1:
                raise ValueError(
                    f"projection expected revision {current + 1}, got {revision}"
                )
            (payload,) = store.read_events_between(proof_id, current, revision)
            event = Proposal.from_dict(payload)
            if event.proof_id != proof_id or not event.ops:
                raise ValueError(
                    "journal event has the wrong proof or no operations"
                )
            if self._space.unavailable:
                raise ProjectionUnavailable("the shared Space requires restart and replay")
            # Failure retains invalidation even after releasing the lock. Only
            # this journal-driven repair path may inspect partial state.
            self._space.invalid_proofs.add(proof_id)
            self._space.recovery.proof = proof_id
            try:
                apply_ops(self, event.ops)
                self.record_projected(proof_id, revision)
                if revision >= self._space.recovery_targets.get(proof_id, revision):
                    self._space.invalid_proofs.discard(proof_id)
                    self._space.recovery_targets.pop(proof_id, None)
            finally:
                self._space.recovery.proof = None

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
            f"(out-edge {encode(proof_id)} $src $rel $dst {encode(local_id)})"
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
        state_id = derive_state_id(candidates)
        if state_id is not None:
            self._write_field("field", proof_id, local_id, "state_id", state_id)
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


def _guard_projection(method, *, proof_argument: bool):
    """Hold the shared lock for complete queries, including Python traversals."""
    parameters = list(inspect.signature(method).parameters)[1:]
    index = 1 if method.__name__ == "add_edge" else 0
    parameter = parameters[index]
    @wraps(method)
    def guarded(self, *args, **kwargs):
        # All graph operations take a proof or entity identifier first.
        identifier = args[index] if len(args) > index else kwargs[parameter]
        proof = identifier if proof_argument else extract_proof_id(identifier)
        with self._lock:
            self._ensure_valid(proof)
            return method(self, *args, **kwargs)
    return guarded


_PROOF_ARGUMENT = {
    "nodes", "edges", "list_nodes", "list_edges", "snapshot", "atoms",
    "get_claim", "get_pending_replay", "get_projector_watermark", "record_projected", "get_discovery",
}
# Watermarks remain inspectable for recovery diagnostics. Mutation and every
# graph read (including inherited compound queries) refuse invalid projections.
for _name in dir(MorkView):
    if not _name.startswith("_") and _name not in {
        "apply_journal_event", "projected_revision", "projected_schema_version",
    }:
        _method = getattr(MorkView, _name)
        if callable(_method):
            setattr(MorkView, _name, _guard_projection(
                _method, proof_argument=_name in _PROOF_ARGUMENT,
            ))
