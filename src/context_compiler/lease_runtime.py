"""Unified scheduler-to-context routing for every integrated packet kind."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from commit_gate.state import ReadView

from .dispatch import CompiledDispatch, LeaseLike
from .errors import ContextValidationError
from .formal_runtime import FormalRuntimeConfig, compile_formal_for_lease
from .obstruction_runtime import (
    ObstructionRuntimeConfig,
    compile_obstruction_for_lease,
)
from .runtime import ResearchRuntimeConfig, compile_research_for_lease
from .snapshot_provider import JournalReader, LeaseSnapshotProvider
from .sources import ArtifactSource

__all__ = ["ContextLeaseCompiler", "SchedulerLike"]


class SchedulerLike(Protocol):
    """Scheduler operations required by ``lease_and_compile``."""

    def lease_next(
        self,
        proof_id: str,
        worker_class: str,
        ttl_seconds: float = 600.0,
        *,
        actor: str,
    ) -> LeaseLike | None: ...

    def release(
        self,
        proof_id: str,
        lease_id: str,
        *,
        fencing_token: int,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ContextLeaseCompiler:
    """Route scheduler leases to the compiler matching their graph object."""

    view: ReadView
    journal: JournalReader
    artifacts: ArtifactSource
    research: ResearchRuntimeConfig
    formal: FormalRuntimeConfig
    obstruction: ObstructionRuntimeConfig

    def __post_init__(self) -> None:
        LeaseSnapshotProvider(self.view, self.journal)
        for method in ("exists", "get"):
            if not callable(getattr(self.artifacts, method, None)):
                raise ContextValidationError(f"artifacts must provide {method}()")
        expected = (
            ("research", self.research, ResearchRuntimeConfig),
            ("formal", self.formal, FormalRuntimeConfig),
            ("obstruction", self.obstruction, ObstructionRuntimeConfig),
        )
        for name, value, kind in expected:
            if not isinstance(value, kind):
                raise ContextValidationError(
                    f"{name} must be a {kind.__name__}"
                )

    def compile(self, lease: LeaseLike) -> CompiledDispatch:
        """Compile a lease according to its selected committed node label."""
        snapshot = LeaseSnapshotProvider(self.view, self.journal).for_lease(lease)
        selected = snapshot.view.node(lease.selected_move_id)
        if selected is None:
            raise ContextValidationError(
                f"selected work {lease.selected_move_id!r} is not committed"
            )
        if selected.label == "ResearchMove":
            return compile_research_for_lease(
                lease, snapshot, self.artifacts, self.research
            )
        if selected.label in {"FormalRun", "FormalDeclaration"}:
            return compile_formal_for_lease(
                lease, snapshot, self.artifacts, self.formal
            )
        if selected.label == "Obstruction":
            return compile_obstruction_for_lease(
                lease, snapshot, self.artifacts, self.obstruction
            )
        raise ContextValidationError(
            f"selected work {lease.selected_move_id!r} has unsupported label "
            f"{selected.label!r}"
        )

    def lease_and_compile(
        self,
        scheduler: SchedulerLike,
        proof_id: str,
        worker_class: str,
        *,
        actor: str,
        ttl_seconds: float = 600.0,
    ) -> CompiledDispatch | None:
        """Acquire the next lease and compile it, releasing failed handoffs.

        Successful leases remain active because the eventual worker proposal
        must carry their identity and fencing token. If compilation fails, the
        lease has not reached a worker and is released so it does not block the
        frontier until its TTL expires.
        """
        for method in ("lease_next", "release"):
            if not callable(getattr(scheduler, method, None)):
                raise ContextValidationError(
                    f"scheduler must provide {method}()"
                )
        lease = scheduler.lease_next(
            proof_id,
            worker_class,
            ttl_seconds,
            actor=actor,
        )
        if lease is None:
            return None
        try:
            return self.compile(lease)
        except Exception as exc:
            try:
                released = scheduler.release(
                    lease.proof_id,
                    lease.lease_id,
                    fencing_token=lease.fencing_token,
                )
                if not released:
                    exc.add_note("failed context handoff lease was not released")
            except Exception as release_error:
                exc.add_note(
                    "failed to release context handoff lease: "
                    f"{release_error!r}"
                )
            raise
