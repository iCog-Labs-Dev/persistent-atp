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
from .ops import ops_from_dicts
from .proposal import Proposal
from .reasons import Rejection
from .state import ReadView, WriteView
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

    `view` answers questions about committed state; `store` is the journal. The
    gate holds no snapshot of the head — `append` reads it under the write lock.

    If `view` can also be written — a `GraphView` such as `MorkView` — accepted
    proposals are projected into it. Reads and writes then go through one object,
    so the gate cannot validate against one picture of state and write another.
    """

    def __init__(self, view: ReadView, store: JournalStore):
        self._view = view
        self._store = store
        self._projects = isinstance(view, WriteView)
        # Reentrant: `commit` calls `validate`, which takes it again.
        self._lock = threading.RLock()

    @property
    def projects(self) -> bool:
        """Whether accepted proposals are written through to the graph."""
        return self._projects

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
        if not self._projects:
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
        mark = self._view.projected_revision(proof_id)
        if mark == self._store.head(proof_id)[0]:
            return 0

        replayed = 0
        for revision, payload in self._store.read_events_after(proof_id, mark):
            try:
                apply_ops(self._view, ops_from_dicts(payload.get("ops") or ()))
            except Exception as exc:
                raise ProjectionError(revision, "", exc) from exc
            # Marked per event: an interrupted replay resumes rather than restarts.
            self._view.record_projected(proof_id, revision)
            replayed += 1
        return replayed

    def validate(self, proposal: Proposal) -> list[Rejection]:
        """Every rule violation in `proposal`, or an empty list."""
        with self._lock:
            return validate_proposal(proposal, self._view)

    def commit(self, proposal: Proposal) -> CommitResult:
        """Validate `proposal` and, if it holds, journal and project it.

        A lost race is a rejection, not an exception: the proposer gets a code it
        can act on, and nothing has been written.

        Holds the gate's lock throughout, which the graph depends on: changing a
        field means reading its current value and then replacing it, and a second
        writer in between would leave both values behind.

        Raises `ProjectionError` if the journal accepted the proposal but the
        graph write failed. The commit stands; see that exception.
        """
        with self._lock:
            if self._projects:
                # Two integers in the normal case; does work only after a crash.
                self._catch_up_locked(proposal.proof_id)

            rejections = validate_proposal(proposal, self._view)
            if rejections:
                return CommitResult(
                    accepted=False,
                    rejections=tuple(rejections),
                    event_hash=None,
                    revision=None,
                )

            try:
                revision, event_hash = self._store.append(proposal.to_dict())
            except ConcurrencyError as exc:
                return CommitResult(
                    accepted=False,
                    rejections=(Rejection(exc.reason, exc.detail),),
                    event_hash=None,
                    revision=None,
                )

            if self._projects:
                try:
                    apply_ops(self._view, proposal.ops)
                except Exception as exc:
                    raise ProjectionError(revision, event_hash, exc) from exc
                self._view.record_projected(proposal.proof_id, revision)
            return CommitResult(
                accepted=True,
                rejections=(),
                event_hash=event_hash,
                revision=revision,
            )
