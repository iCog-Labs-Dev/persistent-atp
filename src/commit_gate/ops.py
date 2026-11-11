"""The operation algebra a committed event carries.

Four operations describe every mutation the gate can apply: node upsert,
field overwrite, edge add, edge remove. Each is frozen, idempotent under
replay, and canonically serializable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, Sequence

__all__ = [
    "Op",
    "UpsertNode",
    "SetField",
    "AddEdge",
    "RemoveEdge",
    "OpClass",
    "UNSET",
    "op_from_dict",
    "ops_from_dicts",
]


class _Unset:
    """Type of the `UNSET` sentinel."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()
"""Absence of a compare-and-set expectation, as distinct from expecting null."""

OpClass = Literal["structural", "status", "annotation"]
"""Classification of an op.

`annotation` ops carry heuristic scores, `status` ops change a status or
verdict field, `structural` ops change shape. An annotation op may never
justify a status op.
"""


@dataclass(frozen=True, slots=True)
class Op:
    """Base class for graph mutations. Not instantiated directly."""

    kind: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def op_class(self) -> OpClass:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class UpsertNode(Op):
    """Create a node, or confirm it exists with these immutable fields.

    `fields` carries only values fixed at creation. Anything mutable belongs
    in `SetField`, where its transition can be validated.
    """

    kind: ClassVar[str] = "upsert_node"

    label: str
    node_id: str
    fields: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.kind,
            "label": self.label,
            "id": self.node_id,
            "fields": dict(self.fields),
        }

    @property
    def op_class(self) -> OpClass:
        return "structural"


@dataclass(frozen=True, slots=True)
class SetField(Op):
    """Overwrite one mutable field on an existing node.

    `prior` is the value the proposer believed was in place, used by the gate
    for a field-level compare-and-set. `None` expects the field to be null;
    `UNSET` expects nothing and is accepted only for annotation-class fields.
    """

    kind: ClassVar[str] = "set_field"

    label: str
    node_id: str
    field: str
    value: Any
    prior: Any = UNSET

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "op": self.kind,
            "label": self.label,
            "id": self.node_id,
            "field": self.field,
            "value": self.value,
        }
        if self.prior is not UNSET:
            payload["prior"] = self.prior
        return payload

    @property
    def op_class(self) -> OpClass:
        from .vocab import ANNOTATION_FIELDS

        if self.field in ANNOTATION_FIELDS:
            return "annotation"
        if self.field.endswith("status") or self.field.endswith("verdict"):
            return "status"
        return "structural"


@dataclass(frozen=True, slots=True)
class AddEdge(Op):
    """Add a typed edge. `edge_id` makes the op idempotent under replay."""

    kind: ClassVar[str] = "add_edge"

    rel_type: str
    src_id: str
    dst_id: str
    edge_id: str
    fields: Mapping[str, Any] = ()  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.kind,
            "rel": self.rel_type,
            "src": self.src_id,
            "dst": self.dst_id,
            "edge_id": self.edge_id,
            "fields": dict(self.fields or {}),
        }

    @property
    def op_class(self) -> OpClass:
        return "structural"


@dataclass(frozen=True, slots=True)
class RemoveEdge(Op):
    """Remove an edge by its stable ID. Derived and superseded edges only."""

    kind: ClassVar[str] = "remove_edge"

    rel_type: str
    edge_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.kind, "rel": self.rel_type, "edge_id": self.edge_id}

    @property
    def op_class(self) -> OpClass:
        return "structural"


_OP_TYPES: dict[str, type[Op]] = {
    UpsertNode.kind: UpsertNode,
    SetField.kind: SetField,
    AddEdge.kind: AddEdge,
    RemoveEdge.kind: RemoveEdge,
}


def op_from_dict(raw: Mapping[str, Any]) -> Op:
    """Rebuild an op from its journalled form."""
    kind = raw.get("op")
    match kind:
        case UpsertNode.kind:
            return UpsertNode(raw["label"], raw["id"], raw.get("fields") or {})
        case SetField.kind:
            return SetField(
                raw["label"],
                raw["id"],
                raw["field"],
                raw["value"],
                raw["prior"] if "prior" in raw else UNSET,
            )
        case AddEdge.kind:
            return AddEdge(
                raw["rel"],
                raw["src"],
                raw["dst"],
                raw["edge_id"],
                raw.get("fields") or {},
            )
        case RemoveEdge.kind:
            return RemoveEdge(raw["rel"], raw["edge_id"])
        case _:
            raise ValueError(f"unknown op kind: {kind!r}")


def ops_from_dicts(raws: Sequence[Mapping[str, Any]]) -> list[Op]:
    """Rebuild a sequence of ops from their journalled forms."""
    return [op_from_dict(raw) for raw in raws]
