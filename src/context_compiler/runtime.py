"""Lease-to-context runtime integration.

This module is intentionally independent of LangGraph and worker execution.
It turns a scheduler lease plus its exact snapshot into an auditable,
worker-ready ``CompiledDispatch``.
"""

from __future__ import annotations

from dataclasses import dataclass

from commit_gate.state import ReadView
from shared.vocab import WorkerClass

from .contracts import CompileRequest, PacketKind, TextBudget
from .dispatch import CompiledDispatch, LeaseLike
from .errors import ContextValidationError
from .research import ResearchTask, compile_research
from .resolvers import resolve_research_task
from .snapshot import ProofSnapshot, StaleSnapshotError
from .snapshot_provider import JournalReader, LeaseSnapshotProvider
from .sources import ArtifactSource, RetrievalPlan, collect_sources

__all__ = [
    "ResearchLeaseCompiler",
    "ResearchRuntimeConfig",
    "compile_research_for_lease",
]


@dataclass(frozen=True, slots=True)
class ResearchRuntimeConfig:
    """Coordinator-owned instructions and retrieval limits for research work."""

    instructions: str
    output_schema: str
    tool_policy: str
    max_tokens: int = 4_000
    max_depth: int = 3
    max_nodes: int = 200
    kernel_id: str | None = None
    require_complete_retrieval: bool = True

    def __post_init__(self) -> None:
        for name in ("instructions", "output_schema", "tool_policy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContextValidationError(f"{name} must be a non-empty string")
        for name in ("max_tokens", "max_nodes"):
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
            raise ContextValidationError("require_complete_retrieval must be a boolean")


@dataclass(frozen=True, slots=True)
class ResearchLeaseCompiler:
    """Compile scheduler research leases through one coordinator-facing API.

    The service owns the dependencies needed to reconstruct the exact journal
    revision named by a lease.  Keeping snapshot construction inside this
    boundary prevents callers from accidentally compiling newer projected
    state for older scheduled work.
    """

    view: ReadView
    journal: JournalReader
    artifacts: ArtifactSource
    config: ResearchRuntimeConfig

    def __post_init__(self) -> None:
        # Constructing the provider performs the shared ReadView and journal
        # capability validation once, before the first lease is dispatched.
        LeaseSnapshotProvider(self.view, self.journal)
        for method in ("exists", "get"):
            if not callable(getattr(self.artifacts, method, None)):
                raise ContextValidationError(f"artifacts must provide {method}()")
        if not isinstance(self.config, ResearchRuntimeConfig):
            raise ContextValidationError("config must be a ResearchRuntimeConfig")

    def compile(self, lease: LeaseLike) -> CompiledDispatch:
        """Return the lease-bound context dispatch ready for a research worker."""
        snapshot = LeaseSnapshotProvider(self.view, self.journal).for_lease(lease)
        return compile_research_for_lease(
            lease,
            snapshot,
            self.artifacts,
            self.config,
        )


def compile_research_for_lease(
    lease: LeaseLike,
    snapshot: ProofSnapshot,
    artifacts: ArtifactSource,
    config: ResearchRuntimeConfig,
) -> CompiledDispatch:
    """Compile one leased research move into a worker-ready dispatch."""
    if not isinstance(lease, LeaseLike):
        raise ContextValidationError("lease does not provide the dispatch lease fields")
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    if not isinstance(config, ResearchRuntimeConfig):
        raise ContextValidationError("config must be a ResearchRuntimeConfig")
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
    if worker_class is not WorkerClass.LLM_RESEARCH:
        raise ContextValidationError(
            "research compilation requires an llm-research lease"
        )

    resolved = resolve_research_task(
        snapshot, lease.selected_move_id, kernel_id=config.kernel_id
    )
    task_id = f"{lease.lease_id}@{lease.fencing_token}"
    request = CompileRequest(
        task_id=task_id,
        proof_id=lease.proof_id,
        base_revision=lease.base_revision,
        packet_kind=PacketKind.RESEARCH,
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
        raise ContextValidationError("research retrieval did not produce a theorem kernel")
    if collection.bundle is None:
        raise ContextValidationError("research retrieval did not produce a source bundle")
    if config.require_complete_retrieval and not collection.is_complete:
        missing = [f"{item.source_id}:{item.reason}" for item in collection.missing]
        truncated = [
            f"{item.reason}:{','.join(item.unexpanded)}"
            for item in collection.truncations
        ]
        detail = "; ".join((*missing, *truncated)) or "unknown incompleteness"
        raise ContextValidationError(f"research retrieval is incomplete: {detail}")

    task = ResearchTask(
        kernel=collection.kernel,
        kernel_version=str(snapshot.revision),
        active_state_id=resolved.active_state_id,
        selected_move_id=resolved.selected_move_id,
        instructions=config.instructions,
        output_schema=config.output_schema,
        tool_policy=config.tool_policy,
    )
    compilation = compile_research(
        request,
        collection.bundle,
        collection,
        task,
    )
    return CompiledDispatch.from_compilation(
        lease=lease,
        compilation=compilation,
        worker_input=compilation.packet.content,
    )
