"""A worker's proposed change to committed proof state.

A proposal is inert: it carries the ops a worker wants applied and the
identity under which it wants them applied. Only the gate may act on one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ops import Op, ops_from_dicts

__all__ = ["Proposal"]


@dataclass(frozen=True, slots=True)
class Proposal:
    """Ops offered for commit, with the proposer's identity and expectations.

    `base_revision` and `lease_id` are the concurrency expectations checked
    against the journal; the proposal-only validators ignore them.
    """

    proof_id: str
    actor: str
    worker_class: str
    ops: tuple[Op, ...] = ()
    base_revision: int | None = None
    lease_id: str | None = None
    fencing_token: int | None = None
    selected_move_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "proof_id": self.proof_id,
            "actor": self.actor,
            "worker_class": str(self.worker_class),
            "base_revision": self.base_revision,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "ops": [op.to_dict() for op in self.ops],
        }
        if self.selected_move_id is not None:
            payload["selected_move_id"] = self.selected_move_id
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Proposal":
        """Rebuild a proposal from its journalled form."""
        return cls(
            proof_id=raw["proof_id"],
            actor=raw["actor"],
            worker_class=raw["worker_class"],
            ops=tuple(ops_from_dicts(raw.get("ops") or ())),
            base_revision=raw.get("base_revision"),
            lease_id=raw.get("lease_id"),
            fencing_token=raw.get("fencing_token"),
            selected_move_id=raw.get("selected_move_id"),
        )
