"""Frozen transport DTOs. Mathematical claims remain receiver-owned facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, ClassVar, TypeAlias


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{field_name} must be a bounded nonempty identifier")


def _hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:") or any(c not in "0123456789abcdef" for c in value[7:]):
        raise ValueError(f"{field_name} must be a sha256 content hash")


def _alias(value: str, field_name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is None:
        raise ValueError(f"{field_name} must be a registered alias")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    hash: str
    size_bytes: int | None = None
    media_type: str | None = None

    def __post_init__(self):
        _hash(self.hash, "artifact hash")
        if self.size_bytes is not None and (type(self.size_bytes) is not int or self.size_bytes < 0):
            raise ValueError("size_bytes must be a nonnegative integer")
        if self.media_type is not None and (not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 128):
            raise ValueError("media_type must be a bounded string")


@dataclass(frozen=True, slots=True)
class Budget:
    max_steps: int
    wall_clock_ms: int

    def __post_init__(self):
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 1_000_000:
            raise ValueError("max_steps must be within 1..1000000")
        if type(self.wall_clock_ms) is not int or not 1 <= self.wall_clock_ms <= 86_400_000:
            raise ValueError("wall_clock_ms must be within 1..86400000")


@dataclass(frozen=True, slots=True)
class Preconditions:
    expected_theorem_hash: str | None = None
    expected_revision: int | None = None
    environment_hash: str | None = None
    declaration_hash: str | None = None
    checkpoint_hash: str | None = None

    def __post_init__(self):
        for name in ("expected_theorem_hash", "environment_hash", "declaration_hash", "checkpoint_hash"):
            value = getattr(self, name)
            if value is not None:
                _hash(value, name)
        if self.expected_revision is not None and (type(self.expected_revision) is not int or self.expected_revision < 0):
            raise ValueError("expected_revision must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProofCreate:
    kind: ClassVar[str] = "proof-create"
    spec_artifact: ArtifactRef

    def __post_init__(self):
        if not isinstance(self.spec_artifact, ArtifactRef):
            raise TypeError("spec_artifact must be ArtifactRef")


@dataclass(frozen=True, slots=True)
class ProofCycle:
    kind: ClassVar[str] = "proof-cycle"
    proof_id: str

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")


COORDINATION_CATEGORIES = frozenset({"formal-run", "research-move", "critic-task"})


@dataclass(frozen=True, slots=True)
class SelectedProofCycle:
    """One service-owned cycle restricted to a frontier category."""

    kind: ClassVar[str] = "proof-cycle-selected"
    proof_id: str
    category: str

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        if self.category not in COORDINATION_CATEGORIES:
            raise ValueError("unknown frontier category")


@dataclass(frozen=True, slots=True)
class ResearchGuidance:
    """A speculative move request; it carries no proof authority."""

    move_kind: str
    detail: str
    target_id: str | None = None

    def __post_init__(self):
        _identifier(self.move_kind, "move_kind")
        if not isinstance(self.detail, str) or not self.detail.strip() or len(self.detail.encode("utf-8")) > 4096:
            raise ValueError("guidance detail must be nonempty and at most 4096 bytes")
        if self.target_id is not None:
            _identifier(self.target_id, "target_id")


@dataclass(frozen=True, slots=True)
class QueueResearchMoves:
    """Idempotently queue bounded guidance through the trusted receiver."""

    kind: ClassVar[str] = "queue-research-moves"
    proof_id: str
    intent_key: str
    moves: tuple[ResearchGuidance, ...]

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.intent_key, "intent_key")
        if not isinstance(self.moves, tuple) or not 1 <= len(self.moves) <= 5 or not all(isinstance(move, ResearchGuidance) for move in self.moves):
            raise ValueError("moves must contain one to five typed research guidance items")


COORDINATOR_RULES = ("repair", "five-mechanisms", "diversify-formal", "bridge-lemma", "alignment-review", "global-audit")
STALL_ESCALATION_RULES = frozenset({"five-mechanisms", "global-audit"})


@dataclass(frozen=True, slots=True)
class CoordinatorEscalation:
    """A speculative policy action, with its target and optional claim separate."""

    rule: str
    target_id: str
    detail: str
    claim_id: str | None = None

    def __post_init__(self):
        if self.rule not in COORDINATOR_RULES:
            raise ValueError("unknown coordinator escalation rule")
        _identifier(self.target_id, "target_id")
        if not isinstance(self.detail, str) or not self.detail.strip() or len(self.detail.encode("utf-8")) > 4096:
            raise ValueError("escalation detail must be nonempty and at most 4096 bytes")
        if self.claim_id is not None:
            _identifier(self.claim_id, "claim_id")


@dataclass(frozen=True, slots=True)
class QueueCoordinatorEscalations:
    """One atomic policy batch; the receiver recomputes its committed basis."""

    kind: ClassVar[str] = "queue-coordinator-escalations"
    proof_id: str
    intent_key: str
    basis: tuple[int, int, int]
    round_number: int
    escalations: tuple[CoordinatorEscalation, ...]

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.intent_key, "intent_key")
        if (not isinstance(self.basis, tuple) or len(self.basis) != 3
                or any(type(n) is not int or n < 0 for n in self.basis)):
            raise ValueError("basis must contain three nonnegative integer counts")
        if type(self.round_number) is not int or self.round_number < 0:
            raise ValueError("round_number must be a nonnegative integer")
        if (not isinstance(self.escalations, tuple) or not 1 <= len(self.escalations) <= 100
                or not all(isinstance(e, CoordinatorEscalation) for e in self.escalations)):
            raise ValueError("escalations must contain one to 100 typed items")
        keys = [(e.rule, e.target_id) for e in self.escalations]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate policy identity in escalation batch")
        for item in self.escalations:
            if item.target_id != self.proof_id and not item.target_id.startswith(self.proof_id + "/"):
                raise ValueError("escalation target belongs to another proof")
            if item.claim_id is not None and not item.claim_id.startswith(self.proof_id + "/"):
                raise ValueError("escalation claim belongs to another proof")


@dataclass(frozen=True, slots=True)
class ProofStatus:
    kind: ClassVar[str] = "proof-status"
    proof_id: str
    at_revision: int | None = None

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        if self.at_revision is not None and (type(self.at_revision) is not int or self.at_revision < 0):
            raise ValueError("at_revision must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProofAudit:
    kind: ClassVar[str] = "proof-audit"
    proof_id: str

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")


@dataclass(frozen=True, slots=True)
class ProofExport:
    kind: ClassVar[str] = "proof-export"
    proof_id: str
    destination_alias: str

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _alias(self.destination_alias, "destination_alias")


@dataclass(frozen=True, slots=True)
class FormalSearch:
    kind: ClassVar[str] = "formal-search"
    proof_id: str
    claim_id: str
    budget: Budget

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.claim_id, "claim_id")
        if not isinstance(self.budget, Budget):
            raise TypeError("budget must be Budget")


@dataclass(frozen=True, slots=True)
class FormalResume:
    kind: ClassVar[str] = "formal-resume"
    proof_id: str
    run_id: str
    budget: Budget
    checkpoint_artifact: ArtifactRef | None = None

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.run_id, "run_id")
        if not isinstance(self.budget, Budget):
            raise TypeError("budget must be Budget")
        if self.checkpoint_artifact is not None and not isinstance(self.checkpoint_artifact, ArtifactRef):
            raise TypeError("checkpoint_artifact must be ArtifactRef")


@dataclass(frozen=True, slots=True)
class StatementAlign:
    kind: ClassVar[str] = "statement-align"
    proof_id: str
    claim_id: str
    lean_source_artifact: ArtifactRef

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.claim_id, "claim_id")
        if not isinstance(self.lean_source_artifact, ArtifactRef):
            raise TypeError("lean_source_artifact must be ArtifactRef")


@dataclass(frozen=True, slots=True)
class LemmaBridge:
    kind: ClassVar[str] = "lemma-bridge"
    proof_id: str
    obstruction_id: str

    def __post_init__(self):
        _identifier(self.proof_id, "proof_id")
        _identifier(self.obstruction_id, "obstruction_id")


BASE_ACTION_TYPES = {cls.kind: cls for cls in (ProofCreate, ProofCycle, ProofStatus, ProofAudit, ProofExport, FormalSearch, FormalResume, StatementAlign, LemmaBridge)}
COORDINATION_ACTION_TYPES = {cls.kind: cls for cls in (SelectedProofCycle, QueueResearchMoves)}
COORDINATOR_ACTION_TYPES = {QueueCoordinatorEscalations.kind: QueueCoordinatorEscalations}
ACTION_TYPES = {**BASE_ACTION_TYPES, **COORDINATION_ACTION_TYPES, **COORDINATOR_ACTION_TYPES}
Action: TypeAlias = ProofCreate | ProofCycle | ProofStatus | ProofAudit | ProofExport | FormalSearch | FormalResume | StatementAlign | LemmaBridge | SelectedProofCycle | QueueResearchMoves | QueueCoordinatorEscalations


def action_types_for_contract(version: str):
    if version == "2.0":
        return BASE_ACTION_TYPES
    if version == "2.1":
        return {**BASE_ACTION_TYPES, **COORDINATION_ACTION_TYPES}
    if version == "2.2":
        return ACTION_TYPES
    raise ValueError("unsupported contract version")


@dataclass(frozen=True, slots=True)
class ActionRequest:
    request_id: str
    workspace_id: str
    action: Action
    preconditions: Preconditions = field(default_factory=Preconditions)
    deadline: datetime | None = None
    contract_version: str = "2.0"
    schema_version: int = 1

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        _alias(self.workspace_id, "workspace_id")
        if type(self.action) not in ACTION_TYPES.values():
            raise TypeError("action must be a registered typed action")
        if not isinstance(self.preconditions, Preconditions):
            raise TypeError("preconditions must be Preconditions")
        if (self.action.kind not in action_types_for_contract(self.contract_version)
                or type(self.schema_version) is not int or self.schema_version != 1):
            raise ValueError("unsupported contract or schema version")
        if (self.contract_version == "2.2" and isinstance(self.action, (SelectedProofCycle, QueueCoordinatorEscalations))
                and self.preconditions.expected_revision is None):
            raise ValueError("coordinator mutations require expected_revision")
        if self.deadline is not None and (not isinstance(self.deadline, datetime) or self.deadline.tzinfo is None or self.deadline.utcoffset() is None):
            raise ValueError("deadline must be an aware UTC datetime")
        if self.deadline is not None and self.deadline.utcoffset().total_seconds() != 0:
            raise ValueError("deadline must use UTC")

    def to_dict(self) -> dict[str, Any]:
        from .codecs import to_dict
        return to_dict(self)

    @classmethod
    def from_dict(cls, raw: object) -> "ActionRequest":
        from .codecs import request_from_dict
        return request_from_dict(raw)


@dataclass(frozen=True, slots=True)
class OperationRef:
    workspace_id: str
    operation_id: str

    def __post_init__(self):
        _alias(self.workspace_id, "workspace_id")
        _identifier(self.operation_id, "operation_id")

    def to_dict(self) -> dict[str, str]:
        return {"workspace_id": self.workspace_id, "operation_id": self.operation_id}

    @classmethod
    def from_dict(cls, raw: object) -> "OperationRef":
        if not isinstance(raw, dict) or set(raw) != {"workspace_id", "operation_id"}:
            raise ValueError("invalid OperationRef fields")
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class Admission:
    request_id: str
    fingerprint: str
    accepted: bool
    operation_ref: OperationRef | None = None
    error_code: str | None = None
    coalesced_to: OperationRef | None = None

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        _hash(self.fingerprint, "fingerprint")
        if type(self.accepted) is not bool or (self.accepted and self.operation_ref is None):
            raise ValueError("accepted admission requires an operation reference")
        if not self.accepted and self.operation_ref is not None:
            raise ValueError("rejected admission cannot contain an operation reference")
        for ref in (self.operation_ref, self.coalesced_to):
            if ref is not None and not isinstance(ref, OperationRef):
                raise TypeError("admission references must be OperationRef")
        if (self.operation_ref is not None and self.coalesced_to is not None
                and self.operation_ref.workspace_id != self.coalesced_to.workspace_id):
            raise ValueError("coalesced operation belongs to another workspace")
        if self.error_code is not None:
            _identifier(self.error_code, "error_code")


LIFECYCLES = frozenset(("queued", "running", "waiting-on-review", "waiting-on-dependency", "waiting-on-resource", "cancelling", "succeeded", "failed", "cancelled"))
TERMINAL_LIFECYCLES = frozenset(("succeeded", "failed", "cancelled"))


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    ref: OperationRef
    request_id: str
    observation_version: int
    lifecycle: str
    execution_phase: str | None = None
    mathematical_disposition: str | None = None
    proof_id: str | None = None
    project_revision: int | None = None
    journal_hash: str | None = None
    projection_watermark: int | None = None
    evidence_artifacts: tuple[ArtifactRef, ...] = ()
    result: dict[str, Any] | None = None

    def __post_init__(self):
        if not isinstance(self.ref, OperationRef):
            raise TypeError("ref must be OperationRef")
        _identifier(self.request_id, "request_id")
        if type(self.observation_version) is not int or self.observation_version < 0:
            raise ValueError("observation_version must be nonnegative")
        if self.lifecycle not in LIFECYCLES:
            raise ValueError("unknown operation lifecycle")
        if self.project_revision is not None and (type(self.project_revision) is not int or self.project_revision < 0):
            raise ValueError("project_revision must be nonnegative")
        if self.projection_watermark is not None and (type(self.projection_watermark) is not int or self.projection_watermark < 0):
            raise ValueError("projection_watermark must be nonnegative")
        if self.journal_hash is not None:
            _hash(self.journal_hash, "journal_hash")
        if (self.project_revision is not None and self.projection_watermark is not None
                and self.projection_watermark > self.project_revision):
            raise ValueError("projection watermark exceeds project revision")
        for name in ("proof_id", "execution_phase", "mathematical_disposition"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        if not isinstance(self.evidence_artifacts, tuple) or not all(
                isinstance(ref, ArtifactRef) for ref in self.evidence_artifacts):
            raise TypeError("evidence_artifacts must be typed artifact references")
        if self.result is not None and not isinstance(self.result, dict):
            raise TypeError("result must be an object or null")


@dataclass(frozen=True, slots=True)
class ProjectDigest:
    workspace_id: str
    proof_id: str
    revision: int
    journal_hash: str
    projection_watermark: int
    target_artifact: ArtifactRef
    literal_target: str
    claim_status: str
    formal_verified: bool
    summary: str
    component_mode: str = "reference"

    def __post_init__(self):
        _alias(self.workspace_id, "workspace_id")
        _identifier(self.proof_id, "proof_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be nonnegative")
        _hash(self.journal_hash, "journal_hash")
        if type(self.projection_watermark) is not int or self.projection_watermark < 0 or self.projection_watermark > self.revision:
            raise ValueError("invalid projection watermark")
        if type(self.formal_verified) is not bool:
            raise ValueError("formal_verified must be boolean")
        if not isinstance(self.literal_target, str) or not self.literal_target:
            raise ValueError("literal_target must be exact nonempty text")
        if not isinstance(self.target_artifact, ArtifactRef):
            raise TypeError("target_artifact must be ArtifactRef")
        _identifier(self.claim_status, "claim_status")
        _identifier(self.component_mode, "component_mode")
        if not isinstance(self.summary, str):
            raise TypeError("summary must be text")


@dataclass(frozen=True, slots=True)
class CoordinationItem:
    """One read-only graph or scheduler record from a pinned proof revision."""

    item_type: str
    record_id: str
    label: str
    fields: dict[str, Any]

    def __post_init__(self):
        if self.item_type not in {"node", "edge", "frontier"}:
            raise ValueError("unknown coordination item type")
        _identifier(self.record_id, "record_id")
        _identifier(self.label, "label")
        if not isinstance(self.fields, dict) or not all(isinstance(key, str) for key in self.fields):
            raise ValueError("coordination fields must be a string-keyed mapping")


@dataclass(frozen=True, slots=True)
class CoordinationPage:
    """A complete or explicitly continued proof-scoped coordination read."""

    workspace_id: str
    proof_id: str
    revision: int
    journal_hash: str
    projection_watermark: int
    items: tuple[CoordinationItem, ...]
    total_items: int
    next_cursor: str | None
    component_mode: str

    def __post_init__(self):
        _alias(self.workspace_id, "workspace_id")
        _identifier(self.proof_id, "proof_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be nonnegative")
        _hash(self.journal_hash, "journal_hash")
        if type(self.projection_watermark) is not int or not 0 <= self.projection_watermark <= self.revision:
            raise ValueError("invalid projection watermark")
        if not isinstance(self.items, tuple) or not all(isinstance(item, CoordinationItem) for item in self.items):
            raise ValueError("items must be typed coordination items")
        if type(self.total_items) is not int or self.total_items < len(self.items):
            raise ValueError("invalid coordination item count")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor")
        _identifier(self.component_mode, "component_mode")


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    sequence: int
    cursor: str
    operation_ref: OperationRef
    event_type: str
    observation_version: int

    def __post_init__(self):
        _identifier(self.event_id, "event_id")
        _identifier(self.cursor, "cursor")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be nonnegative")
        if type(self.observation_version) is not int or self.observation_version < 0:
            raise ValueError("observation_version must be nonnegative")
        if not isinstance(self.operation_ref, OperationRef):
            raise TypeError("operation_ref must be OperationRef")
        _identifier(self.event_type, "event_type")


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    contract_version: str
    actions: frozenset[str]
    controls: frozenset[str] = frozenset()
    backend_mode: str = "unavailable"
    independent_replay: bool = False
    receiver_mode: str = "unavailable"

    def __post_init__(self):
        if not isinstance(self.actions, frozenset) or not all(isinstance(action, str) for action in self.actions):
            raise TypeError("actions must be a frozenset of strings")
        if not isinstance(self.controls, frozenset) or not all(isinstance(control, str) for control in self.controls):
            raise TypeError("controls must be a frozenset of strings")
        if type(self.independent_replay) is not bool:
            raise TypeError("independent_replay must be boolean")
        _identifier(self.receiver_mode, "receiver_mode")
        supported = action_types_for_contract(self.contract_version)
        if not self.actions <= supported.keys():
            raise ValueError("unknown advertised action")
        if self.contract_version != "2.2" and self.controls.intersection({"cycle-execution-revision", "atomic-coordinator-escalations"}):
            raise ValueError("coordinator guarantees require contract 2.2")
        if self.backend_mode not in {"real", "scripted-test", "unavailable"}:
            raise ValueError("unknown backend mode")


@dataclass(frozen=True, slots=True)
class CancelRequest:
    request_id: str
    operation_ref: OperationRef
    expected_observation_version: int | None = None

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        if self.expected_observation_version is not None and (type(self.expected_observation_version) is not int or self.expected_observation_version < 0):
            raise ValueError("expected_observation_version must be nonnegative")


@dataclass(frozen=True, slots=True)
class BudgetAmendment:
    request_id: str
    operation_ref: OperationRef
    expected_budget_version: int
    additional_budget: Budget

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        if type(self.expected_budget_version) is not int or self.expected_budget_version < 0:
            raise ValueError("expected_budget_version must be nonnegative")
        if not isinstance(self.additional_budget, Budget):
            raise TypeError("additional_budget must be Budget")


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    request_id: str
    operation_ref: OperationRef
    decision_id: str
    decision_version: int
    option_id: str
    target_hash: str

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        _identifier(self.decision_id, "decision_id")
        _identifier(self.option_id, "option_id")
        _hash(self.target_hash, "target_hash")
        if type(self.decision_version) is not int or self.decision_version < 0:
            raise ValueError("decision_version must be nonnegative")
