"""A fixed view of one proof at one revision.

The shared source reader takes everything it collects from a single
``ProofSnapshot``, so a context packet is never assembled from a graph that
changed halfway through the read. The snapshot only *pins* the revision it is
given: the caller is responsible for supplying a ``ReadView`` that reflects
that revision.
"""

from __future__ import annotations

from dataclasses import dataclass

from commit_gate.state import ReadView

from .contracts import CompileRequest
from .errors import ContextValidationError

__all__ = ["ProofSnapshot", "StaleSnapshotError"]

_READ_VIEW_METHODS = ("node", "edge", "edges_from", "edges_to")


class StaleSnapshotError(ContextValidationError):
    """The snapshot is not the one the compilation request asked for."""


@dataclass(frozen=True)
class ProofSnapshot:
    proof_id: str
    revision: int
    view: ReadView

    def __post_init__(self) -> None:
        if not isinstance(self.proof_id, str) or not self.proof_id.strip():
            raise ContextValidationError("proof_id must be a non-empty string")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ContextValidationError("revision must be a non-negative integer")
        for method in _READ_VIEW_METHODS:
            if not callable(getattr(self.view, method, None)):
                raise ContextValidationError(
                    f"view must provide {method}() (a ReadView)"
                )

    def require_matches(self, request: CompileRequest) -> None:
        """Refuse to serve a request that was made against a different proof
        or a different revision than this snapshot."""
        if request.proof_id != self.proof_id:
            raise StaleSnapshotError(
                f"request is for proof {request.proof_id!r}, "
                f"snapshot is for {self.proof_id!r}"
            )
        if request.base_revision != self.revision:
            raise StaleSnapshotError(
                f"request expects revision {request.base_revision}, "
                f"snapshot is at revision {self.revision}"
            )   