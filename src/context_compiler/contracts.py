import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from shared.vocab import (
    AlignmentLifecycle,
    AlignmentVerdict,
    AttemptStatus,
    CertificateStatus,
    ClaimStatus,
    DeclarationStatus,
    ExecutorResult,
    FormalStateStatus,
    ObstructionKind,
    ReplayStatus,
    ResearchMoveStatus,
    ResearchStateStatus,
    RunDisposition,
    TacticStatus,
    WorkerClass,
    values,
)

from .errors import ContextValidationError


class PacketKind(StrEnum):
    RESEARCH = "research"
    FORMAL = "formal"
    OBSTRUCTION = "obstruction"


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{name} must be a non-empty string")


def _require_non_negative_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextValidationError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextValidationError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class TextBudget:
    max_tokens: int

    def __post_init__(self) -> None:
        _require_positive_integer("max_tokens", self.max_tokens)


@dataclass(frozen=True)
class FormalExecutionLimits:
    max_steps: int
    wall_clock_ms: int

    def __post_init__(self) -> None:
        _require_positive_integer("max_steps", self.max_steps)
        _require_positive_integer("wall_clock_ms", self.wall_clock_ms)


@dataclass(frozen=True)
class CompileRequest:
    task_id: str
    proof_id: str
    base_revision: int
    packet_kind: PacketKind
    worker_class: WorkerClass
    text_budget: TextBudget | None = None
    formal_limits: FormalExecutionLimits | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "proof_id"):
            _require_non_empty_string(name, getattr(self, name))

        _require_non_negative_integer("base_revision", self.base_revision)

        if not isinstance(self.packet_kind, PacketKind):
            raise ContextValidationError("packet_kind must be a PacketKind")

        if not isinstance(self.worker_class, WorkerClass):
            raise ContextValidationError(
                "worker_class must be a WorkerClass"
            )
        if self.text_budget is not None and not isinstance(
            self.text_budget, TextBudget
        ):
            raise ContextValidationError("text_budget must be a TextBudget")
        if self.formal_limits is not None and not isinstance(
            self.formal_limits, FormalExecutionLimits
        ):
            raise ContextValidationError(
                "formal_limits must be FormalExecutionLimits"
            )


@dataclass(frozen=True)
class TheoremKernel:
    kernel_id: str
    statement: str
    assumptions: tuple[str, ...] = ()
    status: ClaimStatus = ClaimStatus.CONJECTURAL

    def __post_init__(self) -> None:
        _require_non_empty_string("kernel_id", self.kernel_id)
        _require_non_empty_string("statement", self.statement)
        if not isinstance(self.assumptions, tuple):
            raise ContextValidationError("assumptions must be a tuple")
        for assumption in self.assumptions:
            _require_non_empty_string("assumption", assumption)
        if not isinstance(self.status, ClaimStatus):
            raise ContextValidationError("status must be a ClaimStatus")


@dataclass(frozen=True)
class SourceBundle:
    bundle_id: str
    sources: tuple["SourceRef", ...]

    def __post_init__(self) -> None:
        _require_non_empty_string("bundle_id", self.bundle_id)
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ContextValidationError(
                "sources must be a non-empty tuple"
            )
        if not all(isinstance(source, SourceRef) for source in self.sources):
            raise ContextValidationError("sources must contain SourceRef values")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ContextValidationError("sources must have unique source_id values")


@dataclass(frozen=True)
class SelectionDecision:
    source_id: str
    source_version: str
    status: str
    included: bool
    reason: str

    def __post_init__(self) -> None:
        for name in ("source_id", "source_version", "status", "reason"):
            _require_non_empty_string(name, getattr(self, name))
        if not isinstance(self.included, bool):
            raise ContextValidationError("included must be a boolean")
        if self.status not in (
            values(ClaimStatus)
            | values(FormalStateStatus)
            | values(ResearchMoveStatus)
            | values(ResearchStateStatus)
            | values(AttemptStatus)
            | values(DeclarationStatus)
            | values(CertificateStatus)
            | values(AlignmentLifecycle)
            | values(AlignmentVerdict)
            | values(RunDisposition)
            | values(ExecutorResult)
            | values(ReplayStatus)
            | values(TacticStatus)
        ):
            raise ContextValidationError("status must be a shared vocabulary value")


@dataclass(frozen=True)
class ObstructionData:
    obstruction_id: str
    kind: ObstructionKind
    summary: str
    source_ids: tuple[str, ...] = ()
    suggested_escalation: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string("obstruction_id", self.obstruction_id)
        _require_non_empty_string("summary", self.summary)
        if not isinstance(self.kind, ObstructionKind):
            raise ContextValidationError(
                "kind must be an ObstructionKind"
            )
        if not isinstance(self.source_ids, tuple):
            raise ContextValidationError("source_ids must be a tuple")
        for source_id in self.source_ids:
            _require_non_empty_string("source_id", source_id)
        if self.suggested_escalation is not None:
            _require_non_empty_string(
                "suggested_escalation", self.suggested_escalation
            )


@dataclass(frozen=True)
class ContextPacket:
    task_id: str
    proof_id: str
    base_revision: int
    packet_kind: PacketKind
    content: str
    text_budget: TextBudget | None = None
    formal_limits: FormalExecutionLimits | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "proof_id", "content"):
            _require_non_empty_string(name, getattr(self, name))

        _require_non_negative_integer("base_revision", self.base_revision)

        if not isinstance(self.packet_kind, PacketKind):
            raise ContextValidationError("packet_kind must be a PacketKind")
        if self.text_budget is not None and not isinstance(
            self.text_budget, TextBudget
        ):
            raise ContextValidationError("text_budget must be a TextBudget")
        if self.formal_limits is not None and not isinstance(
            self.formal_limits, FormalExecutionLimits
        ):
            raise ContextValidationError(
                "formal_limits must be FormalExecutionLimits"
            )


@dataclass(frozen=True)
class CompiledResult:
    packet: ContextPacket
    selection_decisions: tuple[SelectionDecision, ...]
    manifest: Mapping[str, object]
    packet_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.packet, ContextPacket):
            raise ContextValidationError("packet must be a ContextPacket")
        if not isinstance(self.selection_decisions, tuple):
            raise ContextValidationError(
                "selection_decisions must be a tuple"
            )
        if not all(
            isinstance(decision, SelectionDecision)
            for decision in self.selection_decisions
        ):
            raise ContextValidationError(
                "selection_decisions must contain SelectionDecision values"
            )
        if not isinstance(self.manifest, Mapping):
            raise ContextValidationError("manifest must be a mapping")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.packet_digest) is None:
            raise ContextValidationError(
                "packet_digest must be sha256: followed by "
                "64 lowercase hexadecimal characters"
            )


@dataclass(frozen=True)
class SourceRef:
    source_type: str
    source_id: str
    source_version: str
    content_hash: str

    def __post_init__(self) -> None:
        field_names = (
            "source_type",
            "source_id",
            "source_version",
            "content_hash",
        )

        for name in field_names:
            value = getattr(self, name)

            if not isinstance(value, str) or not value.strip():
                raise ContextValidationError(
                    f"{name} must be a non-empty string"
                )

        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash) is None:
            raise ContextValidationError(
                "content_hash must be sha256: followed by "
                "64 lowercase hexadecimal characters"
            )
