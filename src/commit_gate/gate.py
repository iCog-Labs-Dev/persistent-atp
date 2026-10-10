"""The commit gate.

The gate is the only writer of committed state. A worker submits an inert
`Proposal`; the gate validates it against committed state and, if it holds,
appends it to the journal itself. Nothing else may write to the journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .proposal import Proposal
from .reasons import Reason, Rejection
from .state import JournalOverlayView, ReadView
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

    `view` provides a revisioned snapshot of projected state; `store` is the
    journal. Before validation, the gate overlays journal events the snapshot
    has not yet applied. Append compares the same revision under the write
    lock, rejecting a proposal if the head moved meanwhile.
    """

    def __init__(self, view: ReadView, store: JournalStore):
        self._view = view
        self._store = store

    def validate(self, proposal: Proposal) -> list[Rejection]:
        """Validate against the exact journal revision this proposal names."""
        try:
            snapshot_method = getattr(self._view, "snapshot", None)
        except Exception as exc:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"read view could not provide a snapshot: {exc}",
            )]
        if snapshot_method is None:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                "read view cannot provide a revisioned snapshot",
            )]
        try:
            view_revision, snapshot = snapshot_method(proposal.proof_id)
        except Exception as exc:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"read view could not provide a snapshot: {exc}",
            )]
        head_revision, _ = self._store.head(proposal.proof_id)
        if proposal.base_revision is not None and proposal.base_revision != head_revision:
            return [Rejection(
                Reason.STALE_BASE_REVISION,
                f"proposal is based on revision {proposal.base_revision}, "
                f"head is {head_revision}",
            )]
        if not isinstance(view_revision, int) or not 0 <= view_revision <= head_revision:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"read view revision {view_revision!r} is ahead of journal "
                f"head {head_revision}",
            )]
        try:
            events = self._store.read_events_between(
                proposal.proof_id, view_revision, head_revision
            )
            ops = tuple(
                op for event in events for op in Proposal.from_dict(event).ops
            )
        except ConcurrencyError as exc:
            return [Rejection(exc.reason, exc.detail)]
        except (KeyError, TypeError, ValueError) as exc:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"journal events cannot be replayed for validation: {exc}",
            )]
        try:
            return validate_proposal(
                proposal, JournalOverlayView(snapshot, head_revision, ops)
            )
        except Exception as exc:
            return [Rejection(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"read view failed during validation: {exc}",
            )]

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
