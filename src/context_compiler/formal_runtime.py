"""Scheduler-lease runtime integration for the formal context compiler."""

from __future__ import annotations

from dataclasses import dataclass

from commit_gate.state import ReadView
from shared.vocab import WorkerClass

from .contracts import (
    CompileRequest,
    FormalContextLimits,
    FormalExecutionLimits,
    PacketKind,
)
from .dispatch import CompiledDispatch, LeaseLike
from .errors import ContextValidationError
from .formal import (
    CheckpointPolicy,
    CorpusManifest,
    FormalTask,
    ModelBundle,
    PremiseAccessibility,
    compile_formal,
)
from .resolvers import resolve_formal_task
from .snapshot import ProofSnapshot, StaleSnapshotError
from .snapshot_provider import JournalReader, LeaseSnapshotProvider
from .sources import ArtifactSource, RetrievalPlan, collect_sources

__all__ = [
    "FormalLeaseCompiler",
    "FormalRuntimeConfig",
    "compile_formal_for_lease",
]


@dataclass(frozen=True, slots=True)
class FormalRuntimeConfig:
    """Coordinator-owned policy and ATP capabilities for formal dispatch."""

    execution_limits: FormalExecutionLimits
    accessibility: PremiseAccessibility
    checkpoint_policy: CheckpointPolicy
    search_policy: str
    context_limits: FormalContextLimits | None = None
    premise_ids: tuple[str, ...] = ()
    kernel_id: str | None = None
    max_tactic_steps: int | None = None
    models: ModelBundle | None = None
    corpus: CorpusManifest | None = None
    max_depth: int = 4
    max_nodes: int = 300
    require_complete_retrieval: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.execution_limits, FormalExecutionLimits):
            raise ContextValidationError(
                "execution_limits must be FormalExecutionLimits"
            )
        if not isinstance(self.accessibility, PremiseAccessibility):
            raise ContextValidationError(
                "accessibility must be PremiseAccessibility"
            )
        if not isinstance(self.checkpoint_policy, CheckpointPolicy):
            raise ContextValidationError(
                "checkpoint_policy must be CheckpointPolicy"
            )
        if not isinstance(self.search_policy, str) or not self.search_policy.strip():
            raise ContextValidationError("search_policy must be a non-empty string")
        if self.context_limits is not None and not isinstance(
            self.context_limits, FormalContextLimits
        ):
            raise ContextValidationError(
                "context_limits must be FormalContextLimits"
            )
        if not isinstance(self.premise_ids, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.premise_ids
        ):
            raise ContextValidationError(
                "premise_ids must be a tuple of non-empty strings"
            )
        if self.kernel_id is not None and (
            not isinstance(self.kernel_id, str) or not self.kernel_id.strip()
        ):
            raise ContextValidationError("kernel_id must be a non-empty string")
        if self.max_tactic_steps is not None and (
            isinstance(self.max_tactic_steps, bool)
            or not isinstance(self.max_tactic_steps, int)
            or self.max_tactic_steps <= 0
        ):
            raise ContextValidationError(
                "max_tactic_steps must be a positive integer"
            )
        if self.models is not None and not isinstance(self.models, ModelBundle):
            raise ContextValidationError("models must be a ModelBundle")
        if self.corpus is not None and not isinstance(self.corpus, CorpusManifest):
            raise ContextValidationError("corpus must be a CorpusManifest")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or self.max_depth < 0
        ):
            raise ContextValidationError("max_depth must be a non-negative integer")
        if (
            isinstance(self.max_nodes, bool)
            or not isinstance(self.max_nodes, int)
            or self.max_nodes <= 0
        ):
            raise ContextValidationError("max_nodes must be a positive integer")
        if not isinstance(self.require_complete_retrieval, bool):
            raise ContextValidationError(
                "require_complete_retrieval must be a boolean"
            )


@dataclass(frozen=True, slots=True)
class FormalLeaseCompiler:
    """One-call coordinator boundary for scheduler-issued formal leases."""

    view: ReadView
    journal: JournalReader
    artifacts: ArtifactSource
    config: FormalRuntimeConfig

    def __post_init__(self) -> None:
        LeaseSnapshotProvider(self.view, self.journal)
        for method in ("exists", "get"):
            if not callable(getattr(self.artifacts, method, None)):
                raise ContextValidationError(f"artifacts must provide {method}()")
        if not isinstance(self.config, FormalRuntimeConfig):
            raise ContextValidationError("config must be a FormalRuntimeConfig")

    def compile(self, lease: LeaseLike) -> CompiledDispatch:
        snapshot = LeaseSnapshotProvider(self.view, self.journal).for_lease(lease)
        return compile_formal_for_lease(
            lease, snapshot, self.artifacts, self.config
        )


def compile_formal_for_lease(
    lease: LeaseLike,
    snapshot: ProofSnapshot,
    artifacts: ArtifactSource,
    config: FormalRuntimeConfig,
) -> CompiledDispatch:
    """Compile one existing formal-run lease into an ATP wire request."""
    if not isinstance(lease, LeaseLike):
        raise ContextValidationError("lease does not provide the dispatch lease fields")
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    if not isinstance(config, FormalRuntimeConfig):
        raise ContextValidationError("config must be a FormalRuntimeConfig")
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
    if worker_class is not WorkerClass.FORMAL_ATP:
        raise ContextValidationError(
            "formal compilation requires a formal-atp lease"
        )

    resolved = resolve_formal_task(
        snapshot,
        lease.selected_move_id,
        kernel_id=config.kernel_id,
        premise_ids=config.premise_ids,
    )
    task_id = f"{lease.lease_id}@{lease.fencing_token}"
    request = CompileRequest(
        task_id=task_id,
        proof_id=lease.proof_id,
        base_revision=lease.base_revision,
        packet_kind=PacketKind.FORMAL,
        worker_class=worker_class,
        formal_limits=config.execution_limits,
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
        raise ContextValidationError("formal retrieval did not produce a theorem kernel")
    if collection.bundle is None:
        raise ContextValidationError("formal retrieval did not produce a source bundle")
    if config.require_complete_retrieval and not collection.is_complete:
        missing = [f"{item.source_id}:{item.reason}" for item in collection.missing]
        truncated = [
            f"{item.reason}:{','.join(item.unexpanded)}"
            for item in collection.truncations
        ]
        detail = "; ".join((*missing, *truncated)) or "unknown incompleteness"
        raise ContextValidationError(f"formal retrieval is incomplete: {detail}")

    task = FormalTask(
        kernel=collection.kernel,
        kernel_version=str(snapshot.revision),
        declaration_id=resolved.declaration_id,
        root_state_id=resolved.root_state_id,
        run_id=resolved.run_id,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        search_policy=config.search_policy,
        checkpoint_policy=config.checkpoint_policy,
        claim_id=resolved.claim_id,
        max_tactic_steps=config.max_tactic_steps,
        models=config.models,
        corpus=config.corpus,
    )
    compilation = compile_formal(
        request,
        collection.bundle,
        collection,
        task,
        config.accessibility,
        context_limits=config.context_limits,
    )
    return CompiledDispatch.from_compilation(
        lease=lease,
        compilation=compilation,
        worker_input=compilation.payload.to_schema_dict(),
    )
