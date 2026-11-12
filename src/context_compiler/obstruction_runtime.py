"""Scheduler-lease runtime integration for formal obstruction context."""

from __future__ import annotations

from dataclasses import dataclass

from commit_gate.state import ReadView
from shared.vocab import RunDisposition, WorkerClass

from .contracts import CompileRequest, PacketKind, TextBudget
from .dispatch import CompiledDispatch, LeaseLike
from .errors import ContextValidationError
from .obstruction import ObstructionTask, compile_obstruction
from .resolvers import resolve_obstruction_task
from .snapshot import ProofSnapshot, StaleSnapshotError
from .snapshot_provider import JournalReader, LeaseSnapshotProvider
from .sources import ArtifactSource, RetrievalPlan, collect_sources

__all__ = [
    "ObstructionLeaseCompiler",
    "ObstructionRuntimeConfig",
    "compile_obstruction_for_lease",
]


@dataclass(frozen=True, slots=True)
class ObstructionRuntimeConfig:
    """Coordinator-owned routing and retrieval policy for formal failures."""

    consumer_worker_class: WorkerClass = WorkerClass.COORDINATOR
    eligible_dispositions: frozenset[str] = frozenset(
        {
            RunDisposition.STAGNATED.value,
            RunDisposition.COUNTEREXAMPLE.value,
        }
    )
    max_tokens: int = 2_000
    max_failed_states: int = 3
    max_depth: int = 4
    max_nodes: int = 300
    kernel_id: str | None = None
    require_complete_retrieval: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.consumer_worker_class, WorkerClass):
            raise ContextValidationError(
                "consumer_worker_class must be a WorkerClass"
            )
        if (
            not isinstance(self.eligible_dispositions, frozenset)
            or not self.eligible_dispositions
        ):
            raise ContextValidationError(
                "eligible_dispositions must be a non-empty frozenset"
            )
        for disposition in self.eligible_dispositions:
            try:
                parsed = RunDisposition(disposition)
            except (TypeError, ValueError) as exc:
                raise ContextValidationError(
                    "eligible_dispositions contains an unknown disposition"
                ) from exc
            if parsed in {
                RunDisposition.SEARCHING,
                RunDisposition.PROVED_PENDING_REPLAY,
            }:
                raise ContextValidationError(
                    "eligible_dispositions must contain only unsuccessful "
                    "terminal dispositions"
                )
        for name in ("max_tokens", "max_failed_states", "max_nodes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContextValidationError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or self.max_depth < 0
        ):
            raise ContextValidationError("max_depth must be a non-negative integer")
        if self.kernel_id is not None and (
            not isinstance(self.kernel_id, str) or not self.kernel_id.strip()
        ):
            raise ContextValidationError("kernel_id must be a non-empty string")
        if not isinstance(self.require_complete_retrieval, bool):
            raise ContextValidationError(
                "require_complete_retrieval must be a boolean"
            )


@dataclass(frozen=True, slots=True)
class ObstructionLeaseCompiler:
    """One-call coordinator boundary for scheduler-issued obstruction leases."""

    view: ReadView
    journal: JournalReader
    artifacts: ArtifactSource
    config: ObstructionRuntimeConfig

    def __post_init__(self) -> None:
        LeaseSnapshotProvider(self.view, self.journal)
        for method in ("exists", "get"):
            if not callable(getattr(self.artifacts, method, None)):
                raise ContextValidationError(f"artifacts must provide {method}()")
        if not isinstance(self.config, ObstructionRuntimeConfig):
            raise ContextValidationError(
                "config must be an ObstructionRuntimeConfig"
            )

    def compile(self, lease: LeaseLike) -> CompiledDispatch:
        snapshot = LeaseSnapshotProvider(self.view, self.journal).for_lease(lease)
        return compile_obstruction_for_lease(
            lease, snapshot, self.artifacts, self.config
        )


def compile_obstruction_for_lease(
    lease: LeaseLike,
    snapshot: ProofSnapshot,
    artifacts: ArtifactSource,
    config: ObstructionRuntimeConfig,
) -> CompiledDispatch:
    """Compile one committed formal failure into an obstruction dispatch."""
    if not isinstance(lease, LeaseLike):
        raise ContextValidationError("lease does not provide the dispatch lease fields")
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    if not isinstance(config, ObstructionRuntimeConfig):
        raise ContextValidationError("config must be an ObstructionRuntimeConfig")
    for method in ("exists", "get"):
        if not callable(getattr(artifacts, method, None)):
            raise ContextValidationError(f"artifacts must provide {method}()")
    if lease.proof_id != snapshot.proof_id:
        raise StaleSnapshotError(
            f"lease is for proof {lease.proof_id!r}, snapshot is for "
            f"{snapshot.proof_id!r}"
        )
    if lease.base_revision != snapshot.revision:
        raise StaleSnapshotError(
            f"lease expects revision {lease.base_revision}, snapshot is at "
            f"revision {snapshot.revision}"
        )
    try:
        worker_class = WorkerClass(lease.worker_class)
    except (TypeError, ValueError) as exc:
        raise ContextValidationError("lease worker_class is not recognized") from exc
    if worker_class is not config.consumer_worker_class:
        raise ContextValidationError(
            "obstruction lease worker_class does not match consumer_worker_class"
        )

    resolved = resolve_obstruction_task(
        snapshot,
        lease.selected_move_id,
        eligible_dispositions=config.eligible_dispositions,
        kernel_id=config.kernel_id,
    )
    task_id = f"{lease.lease_id}@{lease.fencing_token}"
    request = CompileRequest(
        task_id=task_id,
        proof_id=lease.proof_id,
        base_revision=lease.base_revision,
        packet_kind=PacketKind.OBSTRUCTION,
        worker_class=worker_class,
        text_budget=TextBudget(config.max_tokens),
    )
    collection = collect_sources(
        request=request,
        snapshot=snapshot,
        artifacts=artifacts,
        plan=RetrievalPlan(
            kernel_id=resolved.kernel_id,
            seed_ids=resolved.seed_ids,
            max_depth=config.max_depth,
            max_nodes=config.max_nodes,
        ),
    )
    if collection.kernel is None:
        raise ContextValidationError(
            "obstruction retrieval did not produce a theorem kernel"
        )
    if collection.bundle is None:
        raise ContextValidationError(
            "obstruction retrieval did not produce a source bundle"
        )
    if config.require_complete_retrieval and not collection.is_complete:
        missing = [f"{item.source_id}:{item.reason}" for item in collection.missing]
        truncated = [
            f"{item.reason}:{','.join(item.unexpanded)}"
            for item in collection.truncations
        ]
        detail = "; ".join((*missing, *truncated)) or "unknown incompleteness"
        raise ContextValidationError(
            f"obstruction retrieval is incomplete: {detail}"
        )

    compilation = compile_obstruction(
        request,
        collection.bundle,
        collection,
        ObstructionTask(collection.kernel, str(snapshot.revision)),
        max_failed_states=config.max_failed_states,
        formal_run_id=resolved.formal_run_id,
        environment_hash=resolved.environment_hash,
    )
    return CompiledDispatch.from_compilation(
        lease=lease,
        compilation=compilation,
        worker_input=compilation.payload.to_schema_dict(),
    )
