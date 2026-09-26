"""Shared contracts and finalization for context compilation."""

from .contracts import (
    CompileRequest,
    CompiledResult,
    ContextPacket,
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

__all__ = [
    "CompileRequest",
    "CompiledResult",
    "ContextPacket",
    "FormalExecutionLimits",
    "ObstructionData",
    "PacketKind",
    "SelectionDecision",
    "SourceBundle",
    "SourceRef",
    "TextBudget",
    "TheoremKernel",
    "finalize_packet",
]