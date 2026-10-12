"""The commit gate.

The gate is the only writer of committed state. A worker submits an inert
`Proposal`; the gate validates it and appends it to the journal itself.

Accepting a proposal writes twice, in this order: the journal records what
happened, then the graph is updated to match. The journal is the record of truth
and the graph is a view replay can rebuild, so a crash in between leaves the
graph merely behind. The reverse order would leave a change in the graph that no
journal entry justifies.

No transaction spans both — the journal is SQLite, the graph may be MORK, which
cannot roll back. In its place: one writer at a time, that fixed order, and ops
that survive being applied twice, so an interrupted commit is finished by
replaying it rather than undone. How far the graph has been projected is
recorded in the graph itself (see `WriteView`), and `commit` replays the
difference before it validates anything.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .apply import apply_ops
from .proposal import Proposal
from .reasons import Reason, Rejection
from .state import JournalOverlayView, ProjectionView, ReadView
from .store import ConcurrencyError, JournalStore
from .validate import validate_proposal

__all__ = ["CommitResult", "CommitGate", "ProjectionError"]


class ProjectionError(RuntimeError):
    """The journal accepted a proposal but the graph could not be updated.

    The commit stands, identified by `revision` and `event_hash`; only the graph
    is behind, and replay brings it level. Raised rather than returned as a
    rejection, so a worker never retries a proposal that in fact committed.
    """

    def __init__(self, revision: int, event_hash: str, cause: BaseException):
        super().__init__(
            f"revision {revision} is journalled but not projected: {cause}"
        )
        self.revision = revision
        self.event_hash = event_hash
        self.cause = cause


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
    """Validates proposals, journals the ones that hold, and projects them.

    `view` provides a revisioned snapshot of projected state; `store` is the
    journal. Before validation, the gate overlays journal events the snapshot
    has not yet applied. Append compares the same revision under the write
    lock, rejecting a proposal if the head moved meanwhile.

    A `ProjectionView` such as `MorkView` is also updated after the durable
    journal append. Other read views remain independently projected and are
    brought current for validation by the journal overlay.
    """

    def __init__(self, view: ReadView, store: JournalStore):
        self._view = view
        self._store = store
        self._projection = view if isinstance(view, ProjectionView) else None
        # Reentrant: `commit` calls `validate`, which takes it again.
        self._lock = threading.RLock()

    @property
    def projects(self) -> bool:
        """Whether accepted proposals are written through to the graph."""
        return self._projection is not None

    @property
    def view(self) -> ReadView:
        """The committed state, for inspection. Do not write through this."""
        return self._view

    @property
    def store(self) -> JournalStore:
        """The journal, for inspection. Do not append through this."""
        return self._store

    def catch_up(self, proof_id: str | None = None) -> dict[str, int]:
        """Replay journalled events the graph has not been told about.

        Returns the events replayed per proof, covering every proof when
        `proof_id` is omitted. `commit` does this on demand, but calling it at
        startup surfaces a stalled projection while someone is watching.
        """
        if self._projection is None:
            return {}
        with self._lock:
            proofs = [proof_id] if proof_id is not None else list(self._store.proof_ids())
            return {
                proof: replayed
                for proof in proofs
                if (replayed := self._catch_up_locked(proof))
            }

    def _catch_up_locked(self, proof_id: str) -> int:
        """Bring the graph level with the journal for one proof."""
        if self._projection is None:
            return 0
        mark = self._projection.projected_revision(proof_id)
        if mark == self._store.head(proof_id)[0]:
            return 0

        replayed = 0
        for revision, payload in self._store.read_events_after(proof_id, mark):
            try:
                event = Proposal.from_dict(payload)
                if event.proof_id != proof_id or not event.ops:
                    raise ValueError("journal event has the wrong proof or no operations")
                apply_ops(self._projection, event.ops)
            except Exception as exc:
                raise ProjectionError(revision, "", exc) from exc
            # Marked per event: an interrupted replay resumes rather than restarts.
            self._projection.record_projected(proof_id, revision)
            replayed += 1
        return replayed

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
        """Validate `proposal` and, if it holds, journal and project it.

        A lost race is a rejection, not an exception: the proposer gets a code it
        can act on, nothing has been committed, and the refusal is audited.

        Holds the gate's lock throughout, which the graph depends on: changing a
        field means reading its current value and then replacing it, and a second
        writer in between would leave both values behind.

        Raises `ProjectionError` if the journal accepted the proposal but the
        graph write failed. The commit stands; see that exception.
        """
        with self._lock:
            if self._projection is not None:
                # Two integers in the normal case; does work only after a crash.
                self._catch_up_locked(proposal.proof_id)

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
                rejection = Rejection(exc.reason, exc.detail)
                self._audit(proposal, (rejection,))
                return CommitResult(
                    accepted=False,
                    rejections=(rejection,),
                    event_hash=None,
                    revision=None,
                )

            if self._projection is not None:
                try:
                    (payload,) = self._store.read_events_between(
                        proposal.proof_id, revision - 1, revision
                    )
                    event = Proposal.from_dict(payload)
                    if event.proof_id != proposal.proof_id or not event.ops:
                        raise ValueError(
                            "journal event has the wrong proof or no operations"
                        )
                    apply_ops(self._projection, event.ops)
                except Exception as exc:
                    raise ProjectionError(revision, event_hash, exc) from exc
                self._projection.record_projected(proposal.proof_id, revision)
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
