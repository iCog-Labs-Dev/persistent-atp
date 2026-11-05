"""Shared contracts and finalization for context compilation."""

from .contracts import (
    CompileRequest,
    CompiledResult,
    ContextPacket,
    FormalContextLimits,
    FormalExecutionLimits,
    ObstructionData,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from .finalize import finalize_packet
from .dispatch import CompiledDispatch
from .errors import FormalRunPreparationRequired
from .formal_runtime import (
    FormalLeaseCompiler,
    FormalRuntimeConfig,
    compile_formal_for_lease,
)
from .lease_runtime import ContextLeaseCompiler, SchedulerLike
from .obstruction import (
    ArtifactReader,
    AttemptEvidence,
    CharHeuristicTokenCounter,
    DiagnosticEvidence,
    FailedState,
    MappingArtifactReader,
    ObstructionCompilation,
    ObstructionPayload,
    ObstructionTask,
    ObservedFailure,
    SuspectedCause,
    TokenCounter,
    compile_obstruction,
)
from .obstruction_runtime import (
    ObstructionLeaseCompiler,
    ObstructionRuntimeConfig,
    compile_obstruction_for_lease,
)
from .snapshot_provider import LeaseSnapshotProvider
from .resolvers import (
    ResolvedFormalTask,
    ResolvedObstructionTask,
    ResolvedResearchTask,
    resolve_formal_task,
    resolve_obstruction_task,
    resolve_research_task,
)
from .runtime import (
    ResearchLeaseCompiler,
    ResearchRuntimeConfig,
    compile_research_for_lease,
)

__all__ = [
    "ArtifactReader",
    "AttemptEvidence",
    "CharHeuristicTokenCounter",
    "CompileRequest",
    "CompiledDispatch",
    "CompiledResult",
    "ContextPacket",
    "ContextLeaseCompiler",
    "DiagnosticEvidence",
    "FailedState",
    "FormalContextLimits",
    "FormalExecutionLimits",
    "FormalLeaseCompiler",
    "FormalRunPreparationRequired",
    "FormalRuntimeConfig",
    "MappingArtifactReader",
    "LeaseSnapshotProvider",
    "ObstructionCompilation",
    "ObstructionData",
    "ObstructionLeaseCompiler",
    "ObstructionPayload",
    "ObstructionRuntimeConfig",
    "ObstructionTask",
    "ObservedFailure",
    "PacketKind",
    "ResearchLeaseCompiler",
    "ResearchRuntimeConfig",
    "ResolvedFormalTask",
    "ResolvedObstructionTask",
    "ResolvedResearchTask",
    "SelectionDecision",
    "SchedulerLike",
    "SourceBundle",
    "SourceRef",
    "SuspectedCause",
    "TextBudget",
    "TheoremKernel",
    "TokenCounter",
    "compile_obstruction",
    "compile_obstruction_for_lease",
    "compile_formal_for_lease",
    "compile_research_for_lease",
    "finalize_packet",
    "resolve_formal_task",
    "resolve_obstruction_task",
    "resolve_research_task",
]
