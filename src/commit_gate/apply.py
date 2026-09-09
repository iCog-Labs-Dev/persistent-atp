"""Apply ops to a memory view for replay and local verification.

Ops are idempotent. Applying an already-applied op leaves the view unchanged.
"""

from __future__ import annotations

from typing import Sequence

from .ops import AddEdge, Op, RemoveEdge, SetField, UpsertNode
from .state import MemoryView

__all__ = ["apply_ops"]


def apply_ops(view: MemoryView, ops: Sequence[Op]) -> None:
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
                # Idempotent no-op. The gate's check_references rejects any
                # UpsertNode whose fields differ from the committed node
                # (Reason.UPSERT_FIELD_CONFLICT), so an UpsertNode can only
                # reach apply for an existing node when its fields already
                # match exactly.
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
