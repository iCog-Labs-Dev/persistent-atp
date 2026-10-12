"""The graph contracts the gate works through.

`ReadView` is four questions about committed state: what is this node, what is
this edge, what leaves a node, what enters it. Validators take one and do not
care which backend answers. `WriteView` is the matching contract for changing
state, and `GraphView` is both — what a projector needs, since applying an op
means reading the current state before writing the new one.

`MemoryView` is the in-process implementation, used by tests and by replay.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from .ops import AddEdge, Op, RemoveEdge, SetField, UpsertNode

if TYPE_CHECKING:
    from .store import JournalStore

__all__ = [
    "NodeRecord",
    "EdgeRecord",
    "ReadView",
    "MutableView",
    "WriteView",
    "GraphView",
    "ProjectionView",
    "MemoryView",
    "JournalOverlayView",
]


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """A committed node: its identity, its label, and its current fields."""

    node_id: str
    label: str
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """A committed edge: its identity, its type, and its endpoints."""

    edge_id: str
    rel_type: str
    src_id: str
    dst_id: str
    fields: Mapping[str, Any]


def _node_copy(record: NodeRecord) -> NodeRecord:
    return NodeRecord(
        record.node_id, record.label,
        MappingProxyType(deepcopy(dict(record.fields))),
    )


def _edge_copy(record: EdgeRecord) -> EdgeRecord:
    return EdgeRecord(
        record.edge_id, record.rel_type, record.src_id, record.dst_id,
        MappingProxyType(deepcopy(dict(record.fields))),
    )


@runtime_checkable
class ReadView(Protocol):
    """Read-only access to one proof's committed state."""

    def snapshot(self, proof_id: str) -> tuple[int, ReadView]:
        """An immutable view and the last fully applied journal revision.

        The state and its revision must be read from one atomic checkpoint.
        """
        ...

    def node(self, node_id: str) -> NodeRecord | None:
        """The node, or None if nothing is committed under that id."""
        ...

    def edge(self, edge_id: str) -> EdgeRecord | None:
        """The edge, or None if nothing is committed under that id."""
        ...

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        """Edges of `rel_type` leaving `node_id`."""
        ...

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        """Edges of `rel_type` entering `node_id`."""
        ...


@runtime_checkable
class MutableView(Protocol):
    """The four idempotent mutations used while projecting journal events."""

    def add_node(
        self, node_id: str, label: str, fields: Mapping[str, Any] | None = None
    ) -> None:
        """Record a node under `node_id`, replacing any fields given."""
        ...

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        """Give `name` exactly one value on an existing node."""
        ...

    def add_edge(
        self,
        rel_type: str,
        src_id: str,
        dst_id: str,
        edge_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an edge under `edge_id`."""
        ...

    def remove_edge(self, edge_id: str) -> None:
        """Retract the edge under `edge_id`. A no-op if it is not there."""
        ...


@runtime_checkable
class WriteView(MutableView, Protocol):
    """A live mutable projection that stores its own journal checkpoint.

    The mark—how far this graph has been brought level with the journal—is
    kept here rather than with the journal so it cannot outlive the state it
    describes. An in-memory graph is empty after a restart; a mark that survived
    in the journal's database would claim that empty graph was level with
    revision N, and every event up to N would be skipped forever.
    """

    def projected_revision(self, proof_id: str) -> int:
        """The last journal revision this graph was brought up to date with."""
        ...

    def record_projected(self, proof_id: str, revision: int) -> None:
        """Note that this graph now reflects the journal up to `revision`.

        Called only after the writes for that revision have succeeded, so the
        mark is never ahead of the state. Must not move backwards.
        """
        ...


@runtime_checkable
class GraphView(ReadView, MutableView, Protocol):
    """A backend that can be both read and written — what a projector takes.

    Applying an op needs both halves: a field is changed by reading what is
    committed and then replacing it.
    """


@runtime_checkable
class ProjectionView(GraphView, WriteView, Protocol):
    """A readable graph that the gate keeps level with its journal."""


@dataclass(slots=True)
class MemoryView:
    """A `GraphView` held in dictionaries: fixtures, replay, and tests."""

    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    edges: dict[str, EdgeRecord] = field(default_factory=dict)
    _out: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    _in: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    _revisions: dict[str, int] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def snapshot(self, proof_id: str) -> tuple[int, ReadView]:
        """Copy the view so later projection cannot change validation reads."""
        with self._lock:
            clone = MemoryView(
                nodes={key: _node_copy(record) for key, record in self.nodes.items()},
                edges={key: _edge_copy(record) for key, record in self.edges.items()},
                _out=defaultdict(list, {key: list(ids) for key, ids in self._out.items()}),
                _in=defaultdict(list, {key: list(ids) for key, ids in self._in.items()}),
                _revisions=dict(self._revisions),
            )
            return self._revisions.get(proof_id, 0), clone

    def apply_journal_event(
        self, store: JournalStore, proof_id: str, revision: int
    ) -> None:
        """Apply the exact next journal event and publish its revision atomically.

        A failed or out-of-order event leaves both the state and the checkpoint
        unchanged. The event's operations come from the journal, not a caller.
        """
        from .apply import apply_ops
        from .proposal import Proposal

        with self._lock:
            current = self._revisions.get(proof_id, 0)
            if revision != current + 1:
                raise ValueError(
                    f"projection expected revision {current + 1}, got {revision}"
                )
            (payload,) = store.read_events_between(proof_id, current, revision)
            event = Proposal.from_dict(payload)
            if event.proof_id != proof_id or not event.ops:
                raise ValueError("journal event has the wrong proof or no operations")
            _, staged = self.snapshot(proof_id)
            apply_ops(staged, event.ops)
            self.nodes = staged.nodes
            self.edges = staged.edges
            self._out = staged._out
            self._in = staged._in
            self._revisions[proof_id] = revision

    def add_node(self, node_id: str, label: str, fields: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self.nodes[node_id] = NodeRecord(node_id, label, deepcopy(dict(fields or {})))
            self._sync_state_reference(node_id)

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        with self._lock:
            current = self.nodes[node_id]
            merged = {**current.fields, name: deepcopy(value)}
            self.nodes[node_id] = NodeRecord(node_id, current.label, merged)

    def add_edge(
        self,
        rel_type: str,
        src_id: str,
        dst_id: str,
        edge_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if edge_id in self.edges:
                self.remove_edge(edge_id)
            self.edges[edge_id] = EdgeRecord(
                edge_id,
                rel_type,
                src_id,
                dst_id,
                deepcopy(dict(fields or {})),
            )
            self._out[(src_id, rel_type)].append(edge_id)
            self._in[(dst_id, rel_type)].append(edge_id)
            affected = self._state_reference_node(self.edges[edge_id])
            if affected is not None:
                self._sync_state_reference(affected)

    def remove_edge(self, edge_id: str) -> None:
        with self._lock:
            record = self.edges.pop(edge_id, None)
            if record is None:
                return
            self._out[(record.src_id, record.rel_type)].remove(edge_id)
            self._in[(record.dst_id, record.rel_type)].remove(edge_id)
            affected = self._state_reference_node(record)
            if affected is not None:
                self._sync_state_reference(affected)

    def node(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            record = self.nodes.get(node_id)
            return _node_copy(record) if record is not None else None

    def edge(self, edge_id: str) -> EdgeRecord | None:
        with self._lock:
            record = self.edges.get(edge_id)
            return _edge_copy(record) if record is not None else None

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        with self._lock:
            return tuple(_edge_copy(self.edges[e]) for e in self._out.get((node_id, rel_type), ()))

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        with self._lock:
            return tuple(_edge_copy(self.edges[e]) for e in self._in.get((node_id, rel_type), ()))

    def _sync_state_reference(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        if node.label == "Move":
            candidates = {
                edge.src_id.split("/", 1)[-1]
                for edge in self.edges_to(node_id, "PROPOSES")
            }
        elif node.label == "Attempt":
            candidates = {
                edge.dst_id.split("/", 1)[-1]
                for edge in self.edges_from(node_id, "ON_STATE")
            }
        else:
            return

        fields = dict(node.fields)
        if len(candidates) == 1:
            fields["state_id"] = next(iter(candidates))
        else:
            fields.pop("state_id", None)
        self.nodes[node_id] = NodeRecord(node_id, node.label, fields)

    @staticmethod
    def _state_reference_node(edge: EdgeRecord) -> str | None:
        if edge.rel_type == "PROPOSES":
            return edge.dst_id
        if edge.rel_type == "ON_STATE":
            return edge.src_id
        return None


class JournalOverlayView:
    """Read-only view of a projection plus journal events it has not applied."""

    def __init__(self, base: ReadView, revision: int, ops: Sequence[Op]):
        self._base = base
        self._revision = revision
        self._node_ops: dict[str, list[Op]] = defaultdict(list)
        self._edge_ops: dict[str, list[Op]] = defaultdict(list)
        self._added_from: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._added_to: dict[tuple[str, str], set[str]] = defaultdict(set)
        for op in ops:
            if isinstance(op, (UpsertNode, SetField)):
                self._node_ops[op.node_id].append(op)
            elif isinstance(op, (AddEdge, RemoveEdge)):
                self._edge_ops[op.edge_id].append(op)
                if isinstance(op, AddEdge):
                    self._added_from[(op.src_id, op.rel_type)].add(op.edge_id)
                    self._added_to[(op.dst_id, op.rel_type)].add(op.edge_id)

    def snapshot(self, proof_id: str) -> tuple[int, ReadView]:
        return self._revision, self

    def node(self, node_id: str) -> NodeRecord | None:
        record = self._base.node(node_id)
        for op in self._node_ops.get(node_id, ()):
            if isinstance(op, UpsertNode) and record is None:
                record = NodeRecord(node_id, op.label, dict(op.fields))
            elif isinstance(op, SetField):
                if record is None:
                    raise ValueError(f"journal sets a field on unknown node {node_id!r}")
                record = NodeRecord(node_id, record.label, {**record.fields, op.field: op.value})
        return record

    def edge(self, edge_id: str) -> EdgeRecord | None:
        record = self._base.edge(edge_id)
        for op in self._edge_ops.get(edge_id, ()):
            if isinstance(op, AddEdge) and record is None:
                record = EdgeRecord(edge_id, op.rel_type, op.src_id, op.dst_id, dict(op.fields or {}))
            elif isinstance(op, RemoveEdge):
                record = None
        return record

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        ids = {edge.edge_id for edge in self._base.edges_from(node_id, rel_type)}
        ids.update(self._added_from.get((node_id, rel_type), ()))
        return tuple(
            edge for edge_id in sorted(ids)
            if (edge := self.edge(edge_id)) is not None
            and edge.src_id == node_id and edge.rel_type == rel_type
        )

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        ids = {edge.edge_id for edge in self._base.edges_to(node_id, rel_type)}
        ids.update(self._added_to.get((node_id, rel_type), ()))
        return tuple(
            edge for edge_id in sorted(ids)
            if (edge := self.edge(edge_id)) is not None
            and edge.dst_id == node_id and edge.rel_type == rel_type
        )
