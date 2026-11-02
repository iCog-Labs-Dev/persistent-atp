"""Apply ops to a graph view for projection and local verification.

Ops are idempotent. Applying an already-applied op leaves the view unchanged,
so a projection interrupted partway through is repaired by replaying it.
"""

from __future__ import annotations

from typing import Sequence

from .ops import AddEdge, Op, RemoveEdge, SetField, UpsertNode
from .state import GraphView

__all__ = ["apply_ops"]


def apply_ops(view: GraphView, ops: Sequence[Op]) -> None:
    """Mutate `view` by applying each op in order.

    Raises `ValueError` if a SetField targets a node that does not exist in the
    view. (The gate rejects such proposals before they reach the journal).
    """
    for op in ops:
        if isinstance(op, UpsertNode):
            existing = view.node(op.node_id)
            if existing is None:
                view.add_node(op.node_id, op.label, op.fields)
            else:
                if existing.label != op.label:
                    raise ValueError(
                        f"node {op.node_id!r} is {existing.label!r}, not {op.label!r}"
                    )
                # Repair a projection interrupted after writing the base node
                # but before all of its fields. Existing unrelated fields are
                # retained because re-upsert is confirmation, not replacement.
                for name, value in op.fields.items():
                    if name not in existing.fields or existing.fields[name] != value:
                        view.set_field(op.node_id, name, value)
        elif isinstance(op, SetField):
            existing = view.node(op.node_id)
            if existing is None:
                raise ValueError(f"cannot SetField on unknown node {op.node_id!r}")
            view.set_field(op.node_id, op.field, op.value)
        elif isinstance(op, AddEdge):
            existing_edge = view.edge(op.edge_id)
            if existing_edge is not None and (
                existing_edge.rel_type != op.rel_type
                or existing_edge.src_id != op.src_id
                or existing_edge.dst_id != op.dst_id
            ):
                raise ValueError(f"edge {op.edge_id!r} conflicts with committed state")
            # Re-ensuring an existing edge repairs a missing reverse index,
            # layer atom, or edge field after an interrupted FFI write.
            view.add_edge(op.rel_type, op.src_id, op.dst_id, op.edge_id, op.fields)
        elif isinstance(op, RemoveEdge):
            view.remove_edge(op.edge_id)
