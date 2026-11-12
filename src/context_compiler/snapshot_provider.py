"""Build immutable proof snapshots for the exact revision a lease names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from commit_gate.proposal import Proposal
from commit_gate.state import JournalOverlayView, ReadView

from .dispatch import LeaseLike
from .errors import ContextValidationError
from .snapshot import ProofSnapshot, StaleSnapshotError

__all__ = ["JournalReader", "LeaseSnapshotProvider"]


class JournalReader(Protocol):
    """Read-only journal operations needed to reconstruct a lease snapshot."""

    def head(self, proof_id: str) -> tuple[int, str]: ...

    def read_events_between(
        self, proof_id: str, after_revision: int, through_revision: int
    ) -> Sequence[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class LeaseSnapshotProvider:
    """Combine a projection snapshot with unapplied journal events.

    The provider fails when the journal has advanced beyond the lease.  That
    avoids spending worker time on context which is already known to be stale.
    """

    view: ReadView
    journal: JournalReader

    def __post_init__(self) -> None:
        if not isinstance(self.view, ReadView):
            raise ContextValidationError("view must implement ReadView")
        for method in ("head", "read_events_between"):
            if not callable(getattr(self.journal, method, None)):
                raise ContextValidationError(f"journal must provide {method}()")

    def for_lease(self, lease: LeaseLike) -> ProofSnapshot:
        """Return an immutable view of ``lease.proof_id@base_revision``."""
        if not isinstance(lease, LeaseLike):
            raise ContextValidationError("lease does not provide the dispatch lease fields")
        if not isinstance(lease.proof_id, str) or not lease.proof_id.strip():
            raise ContextValidationError("lease proof_id must be a non-empty string")
        if (
            isinstance(lease.base_revision, bool)
            or not isinstance(lease.base_revision, int)
            or lease.base_revision < 0
        ):
            raise ContextValidationError(
                "lease base_revision must be a non-negative integer"
            )

        self._require_current_head(lease)
        projected_revision, projected_view = self.view.snapshot(lease.proof_id)
        if (
            isinstance(projected_revision, bool)
            or not isinstance(projected_revision, int)
            or projected_revision < 0
        ):
            raise ContextValidationError(
                "projection snapshot revision must be a non-negative integer"
            )
        if projected_revision > lease.base_revision:
            raise StaleSnapshotError(
                f"projection is at revision {projected_revision}, ahead of lease "
                f"revision {lease.base_revision}"
            )

        events = self.journal.read_events_between(
            lease.proof_id, projected_revision, lease.base_revision
        )
        operations = []
        for raw_event in events:
            proposal = Proposal.from_dict(raw_event)
            if proposal.proof_id != lease.proof_id:
                raise ContextValidationError(
                    f"journal event for {proposal.proof_id!r} cannot build snapshot "
                    f"for {lease.proof_id!r}"
                )
            operations.extend(proposal.ops)

        snapshot_view: ReadView
        if operations:
            snapshot_view = JournalOverlayView(
                projected_view, lease.base_revision, tuple(operations)
            )
        else:
            snapshot_view = projected_view

        # Detect a commit which raced with reconstruction.  A later commit can
        # still happen after this check; the commit gate remains the final
        # authority and will reject that worker result as stale.
        self._require_current_head(lease)
        return ProofSnapshot(lease.proof_id, lease.base_revision, snapshot_view)

    def _require_current_head(self, lease: LeaseLike) -> None:
        head_revision = self.journal.head(lease.proof_id)[0]
        if head_revision != lease.base_revision:
            raise StaleSnapshotError(
                f"lease expects revision {lease.base_revision}, journal head is "
                f"{head_revision}"
            )


