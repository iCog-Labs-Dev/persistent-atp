"""The commit gate.

The gate is the only writer of committed state. A worker submits an inert
`Proposal`; the gate validates it against committed state and, if it holds,
appends it to the journal itself. Nothing else may write to the journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .proposal import Proposal
from .reasons import Rejection
from .state import ReadView
from .store import ConcurrencyError, JournalStore
from .validate import validate_proposal

__all__ = ["CommitResult", "CommitGate"]


@dataclass(frozen=True, slots=True)
class CommitResult:
    """The outcome of submitting a proposal to the gate."""

    accepted: bool
    rejections: tuple[Rejection, ...]
    event_hash: str | None
    revision: int | None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "accepted": self.accepted,
            "rejections": [r.to_dict() for r in self.rejections],
        }
        if self.event_hash is not None:
            payload["event_hash"] = self.event_hash
        if self.revision is not None:
            payload["revision"] = self.revision
        return payload


class CommitGate:
    """Validates proposals and journals the ones that hold.

    `view` answers questions about committed state; `store` is the journal.
    The gate holds no snapshot of the head — the append reads it under the
    write lock, so there is nothing here that can go stale.
    """

    def __init__(self, view: ReadView, store: JournalStore):
        self._view = view
        self._store = store

    def validate(self, proposal: Proposal) -> list[Rejection]:
        """Every rule violation in `proposal`, or an empty list."""
        return validate_proposal(proposal, self._view)

    def commit(self, proposal: Proposal) -> CommitResult:
        """Validate `proposal` and, if it holds, append it to the journal.
        
        A lost race is reported as a rejection rather than raised: the proposer
        gets a code it can act on, and nothing has been written. Refused
        proposals are recorded in the rejection audit log (best-effort: if the
        journal is too busy even for that, the rejection still reaches the
        proposer -- only the audit copy is lost).
        """
        rejections = self.validate(proposal)
        if rejections:
            self._audit(proposal, rejections)
            return CommitResult(
                accepted=False,
                rejections=tuple(rejections),
                event_hash=None,
                revision=None,
            )

        try:
            revision, event_hash = self._store.append(proposal.to_dict())
        except ConcurrencyError as exc:
            self._audit(
                proposal, (Rejection(exc.reason, exc.detail),)
            )
            return CommitResult(
                accepted=False,
                rejections=(Rejection(exc.reason, exc.detail),),
                event_hash=None,
                revision=None,
            )

        return CommitResult(
            accepted=True,
            rejections=(),
            event_hash=event_hash,
            revision=revision,
        )

    def _audit(self, proposal: Proposal, rejections) -> None:
        """Copy refused proposals into the rejection log; never blocks the answer."""
        for rejection in rejections:
            try:
                self._store.record_rejection(
                    proposal.proof_id,
                    str(rejection.reason),
                    rejection.detail,
                    payload=proposal.to_dict(),
                )
            except ConcurrencyError:
                return
