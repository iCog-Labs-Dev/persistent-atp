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
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["NodeRecord", "EdgeRecord", "ReadView", "WriteView", "GraphView", "MemoryView"]


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


@runtime_checkable
class ReadView(Protocol):
    """Read-only access to one proof's committed state."""

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
class WriteView(Protocol):
    """The four mutations a projector performs, and its projection mark.

    The mutations mirror the four ops in `ops.py`; only the gate's projector
    calls them.

    The mark — how far this graph has been brought level with the journal — is
    kept here rather than with the journal so it cannot outlive the state it
    describes. An in-memory graph is empty after a restart; a mark that survived
    in the journal's database would claim that empty graph was level with
    revision N, and every event up to N would be skipped forever.
    """

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
class GraphView(ReadView, WriteView, Protocol):
    """A backend that can be both read and written — what a projector takes.

    Applying an op needs both halves: a field is changed by reading what is
    committed and then replacing it.
    """


@dataclass(slots=True)
class MemoryView:
    """A `GraphView` held in dictionaries: fixtures, replay, and tests."""

    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    edges: dict[str, EdgeRecord] = field(default_factory=dict)
    _out: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    _in: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    _projected: dict[str, int] = field(default_factory=dict)

    def projected_revision(self, proof_id: str) -> int:
        return self._projected.get(proof_id, 0)

    def record_projected(self, proof_id: str, revision: int) -> None:
        self._projected[proof_id] = max(revision, self._projected.get(proof_id, 0))

    def add_node(self, node_id: str, label: str, fields: Mapping[str, Any] | None = None) -> None:
        self.nodes[node_id] = NodeRecord(node_id, label, dict(fields or {}))

    def set_field(self, node_id: str, name: str, value: Any) -> None:
        current = self.nodes[node_id]
        merged = {**current.fields, name: value}
        self.nodes[node_id] = NodeRecord(node_id, current.label, merged)

    def add_edge(
        self,
        rel_type: str,
        src_id: str,
        dst_id: str,
        edge_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        self.edges[edge_id] = EdgeRecord(edge_id, rel_type, src_id, dst_id, dict(fields or {}))
        self._out[(src_id, rel_type)].append(edge_id)
        self._in[(dst_id, rel_type)].append(edge_id)

    def remove_edge(self, edge_id: str) -> None:
        record = self.edges.pop(edge_id, None)
        if record is None:
            return
        self._out[(record.src_id, record.rel_type)].remove(edge_id)
        self._in[(record.dst_id, record.rel_type)].remove(edge_id)

    def node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    def edge(self, edge_id: str) -> EdgeRecord | None:
        return self.edges.get(edge_id)

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return tuple(self.edges[e] for e in self._out.get((node_id, rel_type), ()))

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return tuple(self.edges[e] for e in self._in.get((node_id, rel_type), ()))
