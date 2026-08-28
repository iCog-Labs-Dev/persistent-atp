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
                # Upsert is idempotent. It confirms the node exists.
                pass
        elif isinstance(op, SetField):
            existing = view.node(op.node_id)
            if existing is None:
                raise ValueError(f"cannot SetField on unknown node {op.node_id!r}")
            view.set_field(op.node_id, op.field, op.value)
        elif isinstance(op, AddEdge):
            existing_edge = view.edge(op.edge_id)
            if existing_edge is None:
                view.add_edge(op.rel_type, op.src_id, op.dst_id, op.edge_id, op.fields)
        elif isinstance(op, RemoveEdge):
            view.remove_edge(op.edge_id)
