"""Formal obstruction packet compiler.

Turns recorded formal-search failures into a focused packet for a research
worker: where search became stuck, with traceable evidence for a repair.

The compiler accepts a :class:`~context_compiler.contracts.CompileRequest`
(``packet_kind = obstruction``), a :class:`~context_compiler.contracts.SourceBundle`
of complete source artifacts, and a read-only
:class:`ArtifactReader` for their bodies. An :class:`ObstructionTask` carries
the mandatory theorem kernel separately from failure evidence. The compiler
returns a reusable structured
:class:`ObstructionPayload`, the rendered :class:`ContextPacket`, and the
manifest produced by :func:`context_compiler.finalize.finalize_packet`.

Two properties are load bearing:

* **Observed vs. suspected.**  ``payload.observed`` holds only recorded facts
  (exact hypotheses, targets, attempts, diagnostics); ``payload.suspected``
  holds unverified hypotheses, each carrying the :class:`EvidenceKind` it
  rests on.  The rendered packet keeps them under separate headings.
* **Traceability.**  Every selected state, attempt and diagnostic must be
  backed by a ``SourceRef`` in the bundle.  The theorem kernel and every
  bundle source get a ``SelectionDecision`` recording whether they reached
  the packet and why.

Text budgeting and the optional-context policy are shared with the other
compilers (:mod:`context_compiler.budgeting`, :mod:`context_compiler.selection`).
Mandatory sections (theorem kernel, identity, observed failures, suspected
causes, research questions) are never dropped or truncated: if they alone exceed the text
budget, compilation raises ``ContextValidationError``.  Optional-section
retention priority is attempts, then diagnostics, then the source listing;
as the budget shrinks, they therefore drop in the reverse order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from shared.vocab import (
    AttemptStatus,
    EvidenceKind,
    ExecutorResult,
    FormalStateStatus,
    ObstructionKind,
    RunDisposition,
    values,
)

from .contracts import (
    CompileRequest,
    CompiledResult,
    ContextPacket,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from .budgeting import Section, fit_sections
from .errors import ContextValidationError
from .finalize import _json_value, finalize_packet
from .selection import (
    REASON_OVER_FAILED_STATE_LIMIT,
    REASON_OVER_TEXT_BUDGET,
    duplicate_reason,
)

__all__ = [
    "ArtifactReader",
    "AttemptEvidence",
    "CharHeuristicTokenCounter",
    "DiagnosticEvidence",
    "FailedState",
    "MappingArtifactReader",
    "ObstructionCompilation",
    "ObstructionPayload",
    "ObstructionTask",
    "ObservedFailure",
    "SOURCE_TYPES",
    "SourceBundle",
    "SourceRef",
    "SuspectedCause",
    "TokenCounter",
    "compile_obstruction",
]

FORMAL_STATE_TYPE = "formal-state"
ATTEMPT_TYPE = "attempt"
DIAGNOSTIC_TYPE = "diagnostic"
OBSTRUCTION_TYPE = "obstruction"
FORMAL_RUN_TYPE = "formal-run"

SOURCE_TYPES = frozenset(
    {
        FORMAL_STATE_TYPE,
        ATTEMPT_TYPE,
        DIAGNOSTIC_TYPE,
        OBSTRUCTION_TYPE,
        FORMAL_RUN_TYPE,
    }
)

SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
STATE_ID_RE = re.compile(r"^[^/]+/fs-[1-9][0-9]*$")
OBSTRUCTION_ID_RE = re.compile(r"^[^/]+/obs-[1-9][0-9]*$")
RUN_ID_RE = re.compile(r"^[^/]+/fr-[1-9][0-9]*$")

ESCALATIONS = frozenset(
    {
        "bridge-lemma",
        "decomposition",
        "representation-change",
        "generalize-statement",
        "restrict-statement",
        "invariant",
        "premise-expansion",
        "counterexample-search",
    }
)

_ESCALATION_BY_KIND: Mapping[ObstructionKind, str] = {
    ObstructionKind.MISSING_LEMMA: "bridge-lemma",
    ObstructionKind.MISSING_PREMISE: "premise-expansion",
    ObstructionKind.REPRESENTATION_MISMATCH: "representation-change",
    ObstructionKind.STATEMENT_TOO_STRONG: "restrict-statement",
    ObstructionKind.STATEMENT_TOO_WEAK: "generalize-statement",
    ObstructionKind.LIBRARY_GAP: "premise-expansion",
    ObstructionKind.ELABORATION: "representation-change",
    ObstructionKind.TYPECLASS: "premise-expansion",
    ObstructionKind.COERCION: "representation-change",
    ObstructionKind.RESOURCE: "decomposition",
    ObstructionKind.SEARCH_POLICY: "decomposition",
    ObstructionKind.LIKELY_FALSE: "counterexample-search",
    ObstructionKind.UNKNOWN: "decomposition",
}

_QUESTION_BY_KIND: Mapping[ObstructionKind, str] = {
    ObstructionKind.MISSING_LEMMA: (
        "Which bridge lemma would discharge the target of {state_id}?"
    ),
    ObstructionKind.MISSING_PREMISE: (
        "Which premise is missing for the target of {state_id}?"
    ),
    ObstructionKind.REPRESENTATION_MISMATCH: (
        "Which alternative representation of the target of {state_id} makes "
        "the existing lemmas apply?"
    ),
    ObstructionKind.STATEMENT_TOO_STRONG: (
        "Is the target of {state_id} stronger than the informal claim it "
        "came from?"
    ),
    ObstructionKind.STATEMENT_TOO_WEAK: (
        "Is the target of {state_id} weaker than the informal claim it came "
        "from?"
    ),
    ObstructionKind.LIBRARY_GAP: (
        "Which library lemma or import would close the target of {state_id}?"
    ),
    ObstructionKind.ELABORATION: (
        "Where does elaboration fail on the target of {state_id}?"
    ),
    ObstructionKind.TYPECLASS: (
        "Which typeclass instance is missing for the target of {state_id}?"
    ),
    ObstructionKind.COERCION: (
        "Which coercion is unavailable on the target of {state_id}?"
    ),
    ObstructionKind.RESOURCE: (
        "What resource bound stops progress on the target of {state_id}?"
    ),
    ObstructionKind.SEARCH_POLICY: (
        "Which search policy change would avoid re-exploring {state_id}?"
    ),
    ObstructionKind.LIKELY_FALSE: (
        "Is the target of {state_id} actually false?"
    ),
    ObstructionKind.UNKNOWN: "What blocks progress at {state_id}?",
}

_OBSERVED_TITLE = "OBSERVED FAILURES (recorded, exact)"
_ATTEMPTS_TITLE = "ATTEMPTED TACTICS AND PREMISES (recorded)"
_DIAGNOSTICS_TITLE = "RECURRING DIAGNOSTICS (recorded)"
_SUSPECTED_TITLE = "SUSPECTED CAUSES (unverified, not observed)"
_QUESTIONS_TITLE = "RESEARCH QUESTIONS"
_HEADER_TITLE = "OBSTRUCTION"
_KERNEL_TITLE = "THEOREM KERNEL (canonical, mandatory)"
_SOURCES_TITLE = "SOURCE ARTIFACTS (complete records)"

_MAX_DERIVED_SUSPECTED = 2


def _require_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{name} must be a non-empty string")
    return value


def _scoped_reference(owner_id: str, related_id: str) -> str:
    """Qualify a projection-derived local ID with its owner's proof scope."""
    if "/" in related_id:
        return related_id
    proof_id, separator, _ = owner_id.partition("/")
    return f"{proof_id}/{related_id}" if separator else related_id


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContextValidationError(
            f"{name} must be sha256: followed by 64 lowercase hexadecimal "
            "characters"
        )
    return value


def _str_list(name: str, value: object, *, required: bool = True) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ContextValidationError(f"{name} must be a list of strings")
    items: list[str] = []
    for item in value:
        items.append(_require_str(name, item))
    return tuple(items)


def _vocab(value: object, allowed: frozenset[str]) -> bool:
    """True when ``value`` is one of the shared vocabulary strings."""
    return isinstance(value, str) and value in allowed


def _require_positive(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextValidationError(f"{name} must be a positive integer")
    return value


class ArtifactReader(Protocol):
    """Read-only access to complete source artifacts by ``source_id``."""

    def fetch(self, source_id: str) -> Mapping[str, Any]:
        ...


class MappingArtifactReader(ArtifactReader):
    """An :class:`ArtifactReader` backed by an in-memory mapping."""

    def __init__(self, artifacts: Mapping[str, Mapping[str, Any]]) -> None:
        self._artifacts = dict(artifacts)

    def fetch(self, source_id: str) -> Mapping[str, Any]:
        if source_id not in self._artifacts:
            raise ContextValidationError(f"missing source artifact: {source_id}")
        return self._artifacts[source_id]


@runtime_checkable
class TokenCounter(Protocol):
    """Counts the tokens of a rendered packet."""

    def count(self, text: str) -> int:
        ...


class CharHeuristicTokenCounter(TokenCounter):
    """Deterministic ~4-characters-per-token estimate with no dependencies."""

    def count(self, text: str) -> int:
        if not text:
            return 0
        return (len(text) + 3) // 4


@dataclass(frozen=True)
class FailedState:
    """One failed formal state, copied verbatim from its source artifact."""

    source_id: str
    state_id: str
    hypotheses: tuple[str, ...]
    target: str
    status: FormalStateStatus
    exact_state_hash: str
    semantic_signature: str | None = None


@dataclass(frozen=True)
class AttemptEvidence:
    """One recorded search attempt, copied verbatim from its source artifact."""

    source_id: str
    attempt_id: str
    state_id: str
    tactic_family: str
    premises: tuple[str, ...]
    executor_result: ExecutorResult
    diagnostic_hash: str | None = None
    failure_family: str | None = None


@dataclass(frozen=True)
class DiagnosticEvidence:
    """One recorded diagnostic, addressed by its content hash."""

    source_id: str
    artifact_hash: str
    text: str
    failure_family: str | None
    occurrences: int


@dataclass(frozen=True)
class ObservedFailure:
    """A selected failed state with its exact local context and attempts."""

    state_id: str
    hypotheses: tuple[str, ...]
    target: str
    status: FormalStateStatus
    exact_state_hash: str
    attempts: tuple[AttemptEvidence, ...]
    diagnostics: tuple[DiagnosticEvidence, ...]


@dataclass(frozen=True)
class SuspectedCause:
    """An unverified hypothesis about the failure, with the evidence behind it."""

    hypothesis: str
    basis: EvidenceKind
    note: str = ""
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_str("hypothesis", self.hypothesis)
        if not isinstance(self.basis, EvidenceKind):
            raise ContextValidationError("basis must be an EvidenceKind")
        if not isinstance(self.note, str):
            raise ContextValidationError("note must be a string")
        if not isinstance(self.source_ids, tuple):
            raise ContextValidationError("source_ids must be a tuple")
        for source_id in self.source_ids:
            _require_str("source_id", source_id)


@dataclass(frozen=True)
class ObstructionTask:
    """Canonical theorem context for interpreting one formal obstruction."""

    kernel: TheoremKernel
    kernel_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.kernel, TheoremKernel):
            raise ContextValidationError("kernel must be a TheoremKernel")
        _require_str("kernel_version", self.kernel_version)


@dataclass(frozen=True)
class ObstructionPayload:
    """The reusable structured obstruction: facts, suspicions, questions."""

    obstruction_id: str
    kind: ObstructionKind
    formal_run_id: str
    environment_hash: str
    selected_state_ids: tuple[str, ...]
    observed: tuple[ObservedFailure, ...]
    suspected: tuple[SuspectedCause, ...]
    research_questions: tuple[str, ...]
    premises_considered: tuple[str, ...]
    tactic_families_considered: tuple[str, ...]
    repeated_failure_family: str | None
    suggested_escalation: str | None
    minimal_state_artifact: str
    diagnostic_artifacts: tuple[str, ...]
    source_artifacts: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        if OBSTRUCTION_ID_RE.fullmatch(self.obstruction_id) is None:
            raise ContextValidationError(
                "obstruction_id must look like <proof>/obs-<n>"
            )
        if not isinstance(self.kind, ObstructionKind):
            raise ContextValidationError("kind must be an ObstructionKind")
        if RUN_ID_RE.fullmatch(self.formal_run_id) is None:
            raise ContextValidationError(
                "formal_run_id must look like <proof>/fr-<n>"
            )
        _require_sha256("environment_hash", self.environment_hash)
        _require_sha256("minimal_state_artifact", self.minimal_state_artifact)

        if not isinstance(self.observed, tuple) or not self.observed:
            raise ContextValidationError(
                "observed must be a non-empty tuple of ObservedFailure"
            )
        if not all(isinstance(item, ObservedFailure) for item in self.observed):
            raise ContextValidationError(
                "observed must contain ObservedFailure values"
            )
        observed_ids = tuple(item.state_id for item in self.observed)
        if self.selected_state_ids != observed_ids:
            raise ContextValidationError(
                "selected_state_ids must list the observed states in order"
            )

        if not isinstance(self.suspected, tuple):
            raise ContextValidationError("suspected must be a tuple")
        if not all(isinstance(item, SuspectedCause) for item in self.suspected):
            raise ContextValidationError("suspected must contain SuspectedCause values")

        questions = self.research_questions
        if not isinstance(questions, tuple) or not questions:
            raise ContextValidationError(
                "research_questions must be a non-empty tuple"
            )
        for question in questions:
            _require_str("research_question", question)
        if len(set(questions)) != len(questions):
            raise ContextValidationError("research_questions must be unique")

        for name in ("premises_considered", "tactic_families_considered"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise ContextValidationError(f"{name} must be a tuple")
            for item in value:
                _require_str(name, item)

        if self.repeated_failure_family is not None:
            _require_str("repeated_failure_family", self.repeated_failure_family)
        if self.suggested_escalation is not None:
            _require_str("suggested_escalation", self.suggested_escalation)
            if self.suggested_escalation not in ESCALATIONS:
                raise ContextValidationError(
                    "suggested_escalation must be a documented escalation"
                )

        if not isinstance(self.diagnostic_artifacts, tuple):
            raise ContextValidationError("diagnostic_artifacts must be a tuple")
        for artifact in self.diagnostic_artifacts:
            _require_sha256("diagnostic_artifacts", artifact)

        if not isinstance(self.source_artifacts, tuple) or not self.source_artifacts:
            raise ContextValidationError(
                "source_artifacts must be a non-empty tuple of SourceRef"
            )
        if not all(isinstance(ref, SourceRef) for ref in self.source_artifacts):
            raise ContextValidationError(
                "source_artifacts must contain SourceRef values"
            )

    def to_dict(self) -> dict[str, Any]:
        """The complete payload, including research questions and sources."""
        return _json_value(self)

    def to_schema_dict(self) -> dict[str, Any]:
        """Projection matching ``schemas/obstruction.schema.json`` exactly."""
        payload: dict[str, Any] = {
            "obstruction_id": self.obstruction_id,
            "kind": self.kind.value,
            "formal_run_id": self.formal_run_id,
            "environment_hash": self.environment_hash,
        }
        if self.selected_state_ids:
            payload["formal_state_ids"] = list(self.selected_state_ids)
        payload["minimal_state_artifact"] = self.minimal_state_artifact
        if self.diagnostic_artifacts:
            payload["diagnostic_artifacts"] = list(self.diagnostic_artifacts)
        if self.repeated_failure_family is not None:
            payload["repeated_failure_family"] = self.repeated_failure_family
        if self.premises_considered:
            payload["premises_considered"] = list(self.premises_considered)
        if self.tactic_families_considered:
            payload["tactic_families_considered"] = list(
                self.tactic_families_considered
            )
        evidence = [
            {"kind": cause.basis.value, "note": _evidence_note(cause)}
            for cause in self.suspected
        ]
        if evidence:
            payload["evidence"] = evidence
        if self.suggested_escalation is not None:
            payload["suggested_escalation"] = self.suggested_escalation
        return payload


@dataclass(frozen=True)
class ObstructionCompilation:
    """Payload, rendered packet, and the manifest of that packet."""

    payload: ObstructionPayload
    packet: ContextPacket
    result: CompiledResult

    @property
    def manifest(self) -> Mapping[str, object]:
        return self.result.manifest

    @property
    def packet_digest(self) -> str:
        return self.result.packet_digest


@dataclass(frozen=True)
class _Section:
    title: str
    lines: tuple[str, ...]
    mandatory: bool
    source_ids: tuple[str, ...] = ()
    priority: int = 0  # optional sections only; lower = kept first


@dataclass(frozen=True)
class _Records:
    states: tuple[FailedState, ...]
    attempts: tuple[AttemptEvidence, ...]
    diagnostics: tuple[DiagnosticEvidence, ...]
    obstruction: Mapping[str, Any] | None
    run: Mapping[str, Any] | None
    bodies: Mapping[str, Mapping[str, Any]]


def _evidence_note(cause: SuspectedCause) -> str:
    if cause.note:
        return f"{cause.hypothesis} ({cause.note})"
    return cause.hypothesis


def _parse_state(source: SourceRef, body: Mapping[str, Any]) -> FailedState:
    state_id = _require_str("state_id", body.get("state_id"))
    if state_id != source.source_id:
        raise ContextValidationError(
            f"formal-state state_id must match source_id {source.source_id}"
        )
    if STATE_ID_RE.fullmatch(state_id) is None:
        raise ContextValidationError("state_id must look like <proof>/fs-<n>")
    status = body.get("status")
    if not _vocab(status, values(FormalStateStatus)):
        raise ContextValidationError("status must be a formal state status")
    semantic = body.get("semantic_signature")
    if semantic is not None:
        _require_sha256("semantic_signature", semantic)
    return FailedState(
        source_id=source.source_id,
        state_id=state_id,
        hypotheses=_str_list("hypotheses", body.get("hypotheses")),
        target=_require_str("target", body.get("target")),
        status=FormalStateStatus(status),
        exact_state_hash=_require_sha256(
            "exact_state_hash", body.get("exact_state_hash")
        ),
        semantic_signature=semantic,
    )


def _parse_attempt(source: SourceRef, body: Mapping[str, Any]) -> AttemptEvidence:
    attempt_id = _require_str("attempt_id", body.get("attempt_id"))
    if attempt_id != source.source_id:
        raise ContextValidationError(
            f"attempt attempt_id must match source_id {source.source_id}"
        )
    result = body.get("executor_result")
    if not _vocab(result, values(ExecutorResult)):
        raise ContextValidationError("executor_result must be an executor result")
    diagnostic = body.get("diagnostic")
    if diagnostic is not None:
        _require_sha256("diagnostic", diagnostic)
    failure_family = body.get("failure_family")
    if failure_family is not None:
        _require_str("failure_family", failure_family)
    state_id = _require_str("state_id", body.get("state_id"))
    return AttemptEvidence(
        source_id=source.source_id,
        attempt_id=attempt_id,
        state_id=_scoped_reference(source.source_id, state_id),
        tactic_family=_require_str("tactic_family", body.get("tactic_family")),
        premises=_str_list("premises", body.get("premises"), required=False),
        executor_result=ExecutorResult(result),
        diagnostic_hash=diagnostic,
        failure_family=failure_family,
    )


def _parse_diagnostic(
    source: SourceRef, body: Mapping[str, Any]
) -> DiagnosticEvidence:
    artifact_hash = _require_sha256("artifact_hash", body.get("artifact_hash"))
    if artifact_hash != source.content_hash:
        raise ContextValidationError(
            f"diagnostic artifact_hash must equal the content_hash of "
            f"{source.source_id}"
        )
    family = body.get("failure_family")
    if family is not None:
        _require_str("failure_family", family)
    occurrences = body.get("occurrences", 1)
    if isinstance(occurrences, bool) or not isinstance(occurrences, int):
        raise ContextValidationError("occurrences must be a positive integer")
    if occurrences <= 0:
        raise ContextValidationError("occurrences must be a positive integer")
    return DiagnosticEvidence(
        source_id=source.source_id,
        artifact_hash=artifact_hash,
        text=_require_str("text", body.get("text")),
        failure_family=family,
        occurrences=occurrences,
    )


def _parse_obstruction(source: SourceRef, body: Mapping[str, Any]) -> None:
    obstruction_id = _require_str("obstruction_id", body.get("obstruction_id"))
    if obstruction_id != source.source_id:
        raise ContextValidationError(
            f"obstruction_id must match source_id {source.source_id}"
        )
    if OBSTRUCTION_ID_RE.fullmatch(obstruction_id) is None:
        raise ContextValidationError(
            "obstruction_id must look like <proof>/obs-<n>"
        )
    kind = body.get("kind")
    if not _vocab(kind, values(ObstructionKind)):
        raise ContextValidationError("kind must be an ObstructionKind value")
    run_id = body.get("formal_run_id")
    if run_id is not None and RUN_ID_RE.fullmatch(str(run_id)) is None:
        raise ContextValidationError(
            "formal_run_id must look like <proof>/fr-<n>"
        )
    for name in ("environment_hash", "minimal_state_artifact"):
        value = body.get(name)
        if value is not None:
            _require_sha256(name, value)
    for name in ("formal_state_ids", "premises_considered", "tactic_families_considered", "diagnostic_artifacts"):
        if name in body and body[name] is not None:
            _str_list(name, body[name])
    escalation = body.get("suggested_escalation")
    if escalation is not None:
        _require_str("suggested_escalation", escalation)
        if escalation not in ESCALATIONS:
            raise ContextValidationError(
                "suggested_escalation must be a documented escalation"
            )
    for entry in body.get("evidence") or ():
        if not isinstance(entry, Mapping):
            raise ContextValidationError("evidence must be a list of objects")
        if not _vocab(entry.get("kind"), values(EvidenceKind)):
            raise ContextValidationError("evidence kind must be an EvidenceKind")


def _parse_run(source: SourceRef, body: Mapping[str, Any]) -> None:
    run_id = _require_str("run_id", body.get("run_id"))
    if run_id != source.source_id:
        raise ContextValidationError(
            f"formal-run run_id must match source_id {source.source_id}"
        )
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ContextValidationError("run_id must look like <proof>/fr-<n>")
    disposition = body.get("disposition")
    if disposition is not None and not _vocab(disposition, values(RunDisposition)):
        raise ContextValidationError("disposition must be a run disposition")
    environment_hash = body.get("environment_hash")
    if environment_hash is not None:
        _require_sha256("environment_hash", environment_hash)


def _parse_records(
    bundle: SourceBundle, reader: ArtifactReader
) -> _Records:
    states: list[FailedState] = []
    attempts: list[AttemptEvidence] = []
    diagnostics: list[DiagnosticEvidence] = []
    obstruction: Mapping[str, Any] | None = None
    run: Mapping[str, Any] | None = None
    bodies: dict[str, Mapping[str, Any]] = {}

    for source in bundle.sources:
        if source.source_type not in SOURCE_TYPES:
            raise ContextValidationError(
                f"unknown source_type {source.source_type!r} for {source.source_id}"
            )
        body = reader.fetch(source.source_id)
        if not isinstance(body, Mapping):
            raise ContextValidationError(
                f"source artifact {source.source_id} must be a mapping"
            )
        bodies[source.source_id] = body
        if source.source_type == FORMAL_STATE_TYPE:
            states.append(_parse_state(source, body))
        elif source.source_type == ATTEMPT_TYPE:
            attempts.append(_parse_attempt(source, body))
        elif source.source_type == DIAGNOSTIC_TYPE:
            diagnostics.append(_parse_diagnostic(source, body))
        elif source.source_type == OBSTRUCTION_TYPE:
            _parse_obstruction(source, body)
            obstruction = body
        else:
            _parse_run(source, body)
            run = body

    states.sort(key=lambda state: state.state_id)
    attempts.sort(key=lambda attempt: attempt.attempt_id)
    diagnostics.sort(key=lambda diagnostic: diagnostic.artifact_hash)
    return _Records(
        states=tuple(states),
        attempts=tuple(attempts),
        diagnostics=tuple(diagnostics),
        obstruction=obstruction,
        run=run,
        bodies=bodies,
    )


def _identity(
    request: CompileRequest,
    records: _Records,
    formal_run_id: str | None,
    environment_hash: str | None,
) -> tuple[str, ObstructionKind, str, str]:
    obstruction = records.obstruction or {}
    run = records.run or {}

    obstruction_id = obstruction.get("obstruction_id")
    if obstruction_id is None:
        obstruction_id = f"{request.proof_id}/obs-1"

    kind_value = obstruction.get("kind")
    kind = (
        ObstructionKind(kind_value)
        if _vocab(kind_value, values(ObstructionKind))
        else ObstructionKind.UNKNOWN
    )

    run_id = formal_run_id or obstruction.get("formal_run_id") or run.get("run_id")
    if run_id is None:
        raise ContextValidationError(
            "formal_run_id is required: pass it or include a recorded "
            "obstruction or formal-run source"
        )
    _require_str("formal_run_id", run_id)
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ContextValidationError("formal_run_id must look like <proof>/fr-<n>")

    env = (
        environment_hash
        or obstruction.get("environment_hash")
        or run.get("environment_hash")
    )
    if env is None:
        raise ContextValidationError(
            "environment_hash is required: pass it or include a recorded "
            "obstruction or formal-run source"
        )
    _require_sha256("environment_hash", env)
    return obstruction_id, kind, run_id, env


def _repeated_failure_family(
    records: _Records, attempts: Sequence[AttemptEvidence]
) -> str | None:
    recorded = (records.obstruction or {}).get("repeated_failure_family")
    if recorded:
        return _require_str("repeated_failure_family", recorded)
    counts: dict[str, int] = {}
    for attempt in attempts:
        if attempt.failure_family:
            counts[attempt.failure_family] = counts.get(attempt.failure_family, 0) + 1
    if not counts:
        return None
    best = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return best[0]


def _select_states(
    records: _Records,
    *,
    max_failed_states: int,
    repeated_failure_family: str | None,
    frontier: frozenset[str],
    named: frozenset[str],
) -> tuple[tuple[FailedState, ...], dict[str, str]]:
    """Pick the failed states to show, and say why ranked ones were left out.

    The second element maps the id of every *ranked but unselected* state to
    its exclusion reason: a duplicate of a selected state's semantic
    signature, or over the failed-state limit.
    """
    failing: dict[str, list[AttemptEvidence]] = {}
    for attempt in records.attempts:
        if attempt.executor_result is not ExecutorResult.LEAN_ACCEPTED:
            failing.setdefault(attempt.state_id, []).append(attempt)

    ranked: list[tuple[int, int, int, str, FailedState]] = []
    for state in records.states:
        if (
            state.status in {
                FormalStateStatus.FORMALLY_CLOSED,
                FormalStateStatus.TAINTED,
            }
            and state.state_id not in named
        ):
            continue
        failures = failing.get(state.state_id, [])
        if not failures and state.state_id not in named:
            continue
        score = len(failures)
        if repeated_failure_family:
            score += sum(
                1
                for attempt in failures
                if attempt.failure_family == repeated_failure_family
            )
        ranked.append(
            (
                -int(state.state_id in named),
                -int(state.state_id in frontier),
                -score,
                state.state_id,
                state,
            )
        )
    ranked.sort(key=lambda item: item[:4])

    selected: list[FailedState] = []
    seen_signatures: dict[str, str] = {}
    left_out: dict[str, str] = {}
    for _, _, _, _, state in ranked:
        signature = state.semantic_signature
        if signature is not None and signature in seen_signatures:
            left_out[state.state_id] = duplicate_reason(seen_signatures[signature])
            continue
        if len(selected) >= max_failed_states:
            left_out[state.state_id] = REASON_OVER_FAILED_STATE_LIMIT
            continue
        if signature is not None:
            seen_signatures[signature] = state.state_id
        selected.append(state)
    return tuple(selected), left_out


def _derive_suspected(
    *,
    kind: ObstructionKind,
    records: _Records,
    selected: Sequence[FailedState],
    attempts: Sequence[AttemptEvidence],
    repeated_failure_family: str | None,
    supplied: Sequence[SuspectedCause],
) -> tuple[SuspectedCause, ...]:
    causes: list[SuspectedCause] = list(supplied)
    seen = {cause.hypothesis for cause in causes}
    derived_count = 0

    def add(cause: SuspectedCause) -> None:
        nonlocal derived_count
        if cause.hypothesis in seen or derived_count >= _MAX_DERIVED_SUSPECTED:
            return
        seen.add(cause.hypothesis)
        derived_count += 1
        causes.append(cause)

    recorded_evidence = list((records.obstruction or {}).get("evidence") or ())
    recorded_id = (records.obstruction or {}).get("obstruction_id", "")
    obstruction_sources = (recorded_id,) if recorded_id else ()
    recorded_basis = EvidenceKind.HEURISTIC_OPTIMIZER
    recorded_note = ""
    if recorded_evidence and isinstance(recorded_evidence[0], Mapping):
        entry = recorded_evidence[0]
        if _vocab(entry.get("kind"), values(EvidenceKind)):
            recorded_basis = EvidenceKind(entry["kind"])
        if entry.get("note"):
            recorded_note = str(entry["note"])

    if repeated_failure_family:
        add(
            SuspectedCause(
                hypothesis=(
                    "The environment lacks the symbol or lemma used by the "
                    f"failing tactics (failure family {repeated_failure_family!r})."
                ),
                basis=EvidenceKind.HEURISTIC_OPTIMIZER,
                note=f"same failure family across {len(attempts)} recorded attempts",
                source_ids=obstruction_sources,
            )
        )
    add(
        SuspectedCause(
            hypothesis=_hypothesis_for_kind(kind),
            basis=recorded_basis,
            note=recorded_note or f"recorded obstruction kind {kind.value!r}",
            source_ids=obstruction_sources,
        )
    )
    families = {attempt.tactic_family for attempt in attempts}
    if selected and len(attempts) >= 2 and len(families) == 1:
        family = sorted(families)[0]
        add(
            SuspectedCause(
                hypothesis=(
                    f"Tactic family {family!r} does not match the goal shape "
                    f"at {selected[0].state_id}."
                ),
                basis=EvidenceKind.HEURISTIC_OPTIMIZER,
                note=f"{len(attempts)} attempts, all in one tactic family",
                source_ids=(selected[0].source_id,),
            )
        )
    return tuple(causes)


def _hypothesis_for_kind(kind: ObstructionKind) -> str:
    return (
        f"Obstruction kind {kind.value!r} points at a structural gap rather "
        "than an exhausted search."
    )


def _derive_questions(
    *,
    kind: ObstructionKind,
    selected: Sequence[ObservedFailure],
    repeated_failure_family: str | None,
    environment_hash: str,
    supplied: Sequence[str],
) -> tuple[str, ...]:
    questions: list[str] = []
    for question in supplied:
        _require_str("research_question", question)
        if question not in questions:
            questions.append(question)
    for failure in selected:
        template = _QUESTION_BY_KIND[kind]
        evidence: list[str] = [f"target={failure.target!r}"]
        if failure.hypotheses:
            evidence.append(f"hypotheses={'; '.join(failure.hypotheses)!r}")
        if failure.attempts:
            attempts = ", ".join(
                attempt.tactic_family
                + (f" with {', '.join(attempt.premises)}" if attempt.premises else "")
                for attempt in failure.attempts
            )
            evidence.append(f"attempts={attempts!r}")
        if failure.diagnostics:
            diagnostics = "; ".join(
                diagnostic.text for diagnostic in failure.diagnostics
            )
            evidence.append(f"diagnostics={diagnostics!r}")
        question = (
            f"{template.format(state_id=failure.state_id)} "
            f"Use the recorded evidence ({'; '.join(evidence)})."
        )
        if question not in questions:
            questions.append(question)
    if repeated_failure_family:
        states = ", ".join(failure.state_id for failure in selected)
        question = (
            f"Across states {states}, how should the repeated failure family "
            f"{repeated_failure_family!r} be resolved in environment "
            f"{environment_hash}, given the recorded targets and diagnostics?"
        )
        if question not in questions:
            questions.append(question)
    if not questions:
        raise ContextValidationError(
            "at least one research question must be produced"
        )
    return tuple(questions)


def _considered(
    records: _Records, attempts: Sequence[AttemptEvidence]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    obstruction = records.obstruction or {}
    premises = _str_list(
        "premises_considered", obstruction.get("premises_considered"), required=False
    )
    families = _str_list(
        "tactic_families_considered",
        obstruction.get("tactic_families_considered"),
        required=False,
    )
    if not premises:
        premises = _ordered_unique(
            premise for attempt in attempts for premise in attempt.premises
        )
    if not families:
        families = _ordered_unique(attempt.tactic_family for attempt in attempts)
    return premises, families


def _ordered_unique(items: Any) -> tuple[str, ...]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _escalation(records: _Records, kind: ObstructionKind) -> str:
    recorded = (records.obstruction or {}).get("suggested_escalation")
    if recorded in ESCALATIONS:
        return str(recorded)
    return _ESCALATION_BY_KIND[kind]


def _observed_failures(
    selected: Sequence[FailedState],
    attempts: Sequence[AttemptEvidence],
    diagnostics: Sequence[DiagnosticEvidence],
) -> tuple[ObservedFailure, ...]:
    by_hash = {diagnostic.artifact_hash: diagnostic for diagnostic in diagnostics}
    failures: list[ObservedFailure] = []
    for state in selected:
        state_attempts = tuple(
            sorted(
                (attempt for attempt in attempts if attempt.state_id == state.state_id),
                key=lambda attempt: attempt.attempt_id,
            )
        )
        state_diagnostics = tuple(
            sorted(
                (
                    by_hash[attempt.diagnostic_hash]
                    for attempt in state_attempts
                    if attempt.diagnostic_hash in by_hash
                ),
                key=lambda diagnostic: diagnostic.artifact_hash,
            )
        )
        failures.append(
            ObservedFailure(
                state_id=state.state_id,
                hypotheses=state.hypotheses,
                target=state.target,
                status=state.status,
                exact_state_hash=state.exact_state_hash,
                attempts=state_attempts,
                diagnostics=state_diagnostics,
            )
        )
    return tuple(failures)


def _render_header(
    request: CompileRequest,
    *,
    obstruction_id: str,
    kind: ObstructionKind,
    formal_run_id: str,
    environment_hash: str,
) -> tuple[str, ...]:
    return (
        f"task={request.task_id} proof={request.proof_id} "
        f"revision={request.base_revision} run={formal_run_id}",
        f"obstruction={obstruction_id} kind={kind.value} "
        f"environment={environment_hash}",
    )


def _render_kernel(task: ObstructionTask) -> tuple[str, ...]:
    kernel = task.kernel
    lines = [
        f"id={kernel.kernel_id} version={task.kernel_version} "
        f"status={kernel.status.value}",
        f"statement: {kernel.statement}",
        "assumptions:",
    ]
    if kernel.assumptions:
        lines.extend(f"- {assumption}" for assumption in kernel.assumptions)
    else:
        lines.append("- (none)")
    return tuple(lines)


def _render_states(observed: Sequence[ObservedFailure]) -> tuple[str, ...]:
    lines: list[str] = []
    for failure in observed:
        lines.append(f"- {failure.state_id} [{failure.status.value}]")
        lines.append("  hypotheses:")
        if failure.hypotheses:
            for hypothesis in failure.hypotheses:
                lines.append(f"    - {hypothesis}")
        else:
            lines.append("    - (none recorded)")
        lines.append(f"  target: {failure.target}")
        lines.append(f"  exact-state: {failure.exact_state_hash}")
    return tuple(lines)


def _render_attempts(observed: Sequence[ObservedFailure]) -> tuple[str, ...]:
    lines: list[str] = []
    for failure in observed:
        for attempt in failure.attempts:
            premises = ", ".join(attempt.premises)
            line = (
                f"- {attempt.attempt_id} state={attempt.state_id} "
                f"tactic={attempt.tactic_family} premises=[{premises}] "
                f"result={attempt.executor_result.value}"
            )
            if attempt.diagnostic_hash:
                line += f" diagnostic={attempt.diagnostic_hash}"
            if attempt.failure_family:
                line += f" failure-family={attempt.failure_family}"
            lines.append(line)
    if not lines:
        lines.append("- (no attempts recorded for the selected states)")
    return tuple(lines)


def _render_diagnostics(observed: Sequence[ObservedFailure]) -> tuple[str, ...]:
    lines: list[str] = []
    for failure in observed:
        for diagnostic in failure.diagnostics:
            family = (
                f" family={diagnostic.failure_family}"
                if diagnostic.failure_family
                else ""
            )
            lines.append(
                f"- {diagnostic.artifact_hash}{family} "
                f"occurrences={diagnostic.occurrences}: {diagnostic.text}"
            )
    if not lines:
        lines.append("- (no diagnostics recorded for the selected states)")
    return tuple(lines)


def _render_suspected(causes: Sequence[SuspectedCause]) -> tuple[str, ...]:
    if not causes:
        return ("- (no suspected causes recorded)",)
    return tuple(
        f"- [{cause.basis.value}] {cause.hypothesis}"
        + (f" (note: {cause.note})" if cause.note else "")
        for cause in causes
    )


def _render_questions(questions: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{index}. {question}" for index, question in enumerate(questions, 1))


def _render_sources(bundle: SourceBundle) -> tuple[str, ...]:
    return tuple(
        f"- {source.source_type} {source.source_id} {source.source_version} "
        f"{source.content_hash}"
        for source in bundle.sources
    )


def _render(sections: Sequence[_Section]) -> str:
    return "\n\n".join(
        f"{section.title}\n" + "\n".join(section.lines) for section in sections
    )


def _fit_to_budget(
    sections: Sequence[_Section], budget: TextBudget, counter: TokenCounter
) -> tuple[str, tuple[str, ...]]:
    """Fit sections to ``budget`` with the shared policy.

    Returns the rendered text and the titles of the optional sections that
    were dropped.  Raises if the mandatory sections alone do not fit.
    """
    fit = fit_sections(
        [
            Section(
                key=section.title,
                heading=section.title,
                body="\n".join(section.lines),
                priority=section.priority,
                mandatory=section.mandatory,
            )
            for section in sections
        ],
        budget,
        counter,
        render=_render_fitted,
    )
    return fit.text, tuple(section.key for section in fit.dropped)


def _render_fitted(sections: Sequence[Section]) -> str:
    return "\n\n".join(f"{section.heading}\n{section.body}" for section in sections)


def _identity_status(body: Mapping[str, Any], records: _Records) -> str:
    """Lifecycle status of an identity source: the run disposition if known."""
    disposition = body.get("disposition") or (records.run or {}).get("disposition")
    if _vocab(disposition, values(RunDisposition)):
        return str(disposition)
    return AttemptStatus.PENDING.value


def compile_obstruction(
    request: CompileRequest,
    bundle: SourceBundle,
    reader: ArtifactReader,
    task: ObstructionTask,
    *,
    token_counter: TokenCounter | None = None,
    suspected_causes: Sequence[SuspectedCause | str] = (),
    research_questions: Sequence[str] = (),
    max_failed_states: int = 3,
    formal_run_id: str | None = None,
    environment_hash: str | None = None,
    compiler_version: str = "context/1",
    rendering_version: str = "text/1",
) -> ObstructionCompilation:
    """Compile recorded search failures into an obstruction packet.

    ``task`` supplies the canonical theorem kernel that makes the failure
    evidence meaningful.  The returned :class:`ObstructionCompilation`
    carries the reusable payload, the budget-fitted rendered packet, and that
    packet's manifest.
    """
    if not isinstance(request, CompileRequest):
        raise ContextValidationError("request must be a CompileRequest")
    if request.packet_kind is not PacketKind.OBSTRUCTION:
        raise ContextValidationError(
            "packet_kind must be PacketKind.OBSTRUCTION"
        )
    if not isinstance(bundle, SourceBundle):
        raise ContextValidationError("bundle must be a SourceBundle")
    if not isinstance(task, ObstructionTask):
        raise ContextValidationError("task must be an ObstructionTask")
    max_failed_states = _require_positive("max_failed_states", max_failed_states)

    counter: TokenCounter = token_counter or CharHeuristicTokenCounter()
    if not isinstance(counter, TokenCounter):
        raise ContextValidationError("token_counter must provide count(text)")

    supplied_causes = _coerce_suspected(suspected_causes)
    records = _parse_records(bundle, reader)
    obstruction_id, kind, run_id, env_hash = _identity(
        request, records, formal_run_id, environment_hash
    )

    frontier = frozenset(
        _str_list("frontier_state_ids", (records.run or {}).get("frontier_state_ids"), required=False)
    )
    named = frozenset(
        _str_list(
            "formal_state_ids",
            (records.obstruction or {}).get("formal_state_ids"),
            required=False,
        )
    )
    repeated_family = _repeated_failure_family(records, records.attempts)
    selected, left_out = _select_states(
        records,
        max_failed_states=max_failed_states,
        repeated_failure_family=repeated_family,
        frontier=frontier,
        named=named,
    )
    if not selected:
        raise ContextValidationError(
            "no failed states found in the source bundle"
        )

    attempts = tuple(
        attempt
        for attempt in records.attempts
        if attempt.state_id in {state.state_id for state in selected}
    )
    diagnostics = _diagnostics_for(records, attempts)
    observed = _observed_failures(selected, attempts, diagnostics)
    suspected = _derive_suspected(
        kind=kind,
        records=records,
        selected=selected,
        attempts=attempts,
        repeated_failure_family=repeated_family,
        supplied=supplied_causes,
    )
    questions = _derive_questions(
        kind=kind,
        selected=observed,
        repeated_failure_family=repeated_family,
        environment_hash=env_hash,
        supplied=research_questions,
    )
    premises, families = _considered(records, attempts)
    diagnostic_hashes = tuple(
        diagnostic.artifact_hash for diagnostic in diagnostics
    )

    payload = ObstructionPayload(
        obstruction_id=obstruction_id,
        kind=kind,
        formal_run_id=run_id,
        environment_hash=env_hash,
        selected_state_ids=tuple(state.state_id for state in selected),
        observed=observed,
        suspected=suspected,
        research_questions=questions,
        premises_considered=premises,
        tactic_families_considered=families,
        repeated_failure_family=repeated_family,
        suggested_escalation=_escalation(records, kind),
        minimal_state_artifact=selected[0].exact_state_hash,
        diagnostic_artifacts=diagnostic_hashes,
        source_artifacts=bundle.sources,
    )

    sections = _build_sections(
        request=request,
        task=task,
        payload=payload,
        bundle=bundle,
        run_id=run_id,
        env_hash=env_hash,
    )
    if request.text_budget is None:
        content, dropped = _render(sections), ()
    else:
        content, dropped = _fit_to_budget(sections, request.text_budget, counter)

    kernel = task.kernel
    decisions = (
        SelectionDecision(
            source_id=kernel.kernel_id,
            source_version=task.kernel_version,
            status=kernel.status.value,
            included=True,
            reason="theorem kernel",
        ),
        *_decisions(
            bundle=bundle,
            records=records,
            payload=payload,
            attempts=records.attempts,
            selected_ids=set(payload.selected_state_ids),
            section_of=_section_index(sections),
            dropped=set(dropped),
            left_out=left_out,
        ),
    )

    packet = ContextPacket(
        task_id=request.task_id,
        proof_id=request.proof_id,
        base_revision=request.base_revision,
        packet_kind=PacketKind.OBSTRUCTION,
        content=content,
        text_budget=request.text_budget,
        formal_limits=request.formal_limits,
    )
    result = finalize_packet(
        packet,
        decisions,
        compiler_version=compiler_version,
        rendering_version=rendering_version,
    )
    return ObstructionCompilation(payload=payload, packet=packet, result=result)


def _coerce_suspected(
    causes: Sequence[SuspectedCause | str],
) -> tuple[SuspectedCause, ...]:
    coerced: list[SuspectedCause] = []
    for cause in causes:
        if isinstance(cause, SuspectedCause):
            coerced.append(cause)
        elif isinstance(cause, str):
            coerced.append(
                SuspectedCause(
                    hypothesis=cause,
                    basis=EvidenceKind.HUMAN_GUIDANCE,
                    note="caller-supplied, unverified",
                )
            )
        else:
            raise ContextValidationError(
                "suspected_causes must hold SuspectedCause values or strings"
            )
    return tuple(coerced)


def _diagnostics_for(
    records: _Records, attempts: Sequence[AttemptEvidence]
) -> tuple[DiagnosticEvidence, ...]:
    wanted = {
        attempt.diagnostic_hash for attempt in attempts if attempt.diagnostic_hash
    }
    wanted.update(
        hash_value
        for hash_value in _str_list(
            "diagnostic_artifacts",
            (records.obstruction or {}).get("diagnostic_artifacts"),
            required=False,
        )
    )
    return tuple(
        diagnostic
        for diagnostic in records.diagnostics
        if diagnostic.artifact_hash in wanted
    )


def _build_sections(
    *,
    request: CompileRequest,
    task: ObstructionTask,
    payload: ObstructionPayload,
    bundle: SourceBundle,
    run_id: str,
    env_hash: str,
) -> tuple[_Section, ...]:
    selected_ids = set(payload.selected_state_ids)
    state_sources = tuple(
        source.source_id
        for source in bundle.sources
        if source.source_type == FORMAL_STATE_TYPE
        and source.source_id in selected_ids
    )
    attempt_sources = tuple(
        source.source_id
        for source in bundle.sources
        if source.source_type == ATTEMPT_TYPE
        and any(
            attempt.attempt_id == source.source_id for failure in payload.observed for attempt in failure.attempts
        )
    )
    diagnostic_sources = tuple(
        source.source_id
        for source in bundle.sources
        if source.source_type == DIAGNOSTIC_TYPE
        and source.content_hash in set(payload.diagnostic_artifacts)
    )

    return (
        _Section(
            title=_HEADER_TITLE,
            lines=_render_header(
                request,
                obstruction_id=payload.obstruction_id,
                kind=payload.kind,
                formal_run_id=run_id,
                environment_hash=env_hash,
            ),
            mandatory=True,
            source_ids=_identity_source_ids(bundle),
        ),
        _Section(
            title=_KERNEL_TITLE,
            lines=_render_kernel(task),
            mandatory=True,
            source_ids=(task.kernel.kernel_id,),
        ),
        _Section(
            title=_OBSERVED_TITLE,
            lines=_render_states(payload.observed),
            mandatory=True,
            source_ids=state_sources,
        ),
        _Section(
            title=_ATTEMPTS_TITLE,
            lines=_render_attempts(payload.observed),
            mandatory=False,
            source_ids=attempt_sources,
            priority=1,
        ),
        _Section(
            title=_DIAGNOSTICS_TITLE,
            lines=_render_diagnostics(payload.observed),
            mandatory=False,
            source_ids=diagnostic_sources,
            priority=2,
        ),
        _Section(
            title=_SUSPECTED_TITLE,
            lines=_render_suspected(payload.suspected),
            mandatory=True,
        ),
        _Section(
            title=_QUESTIONS_TITLE,
            lines=_render_questions(payload.research_questions),
            mandatory=True,
        ),
        _Section(
            title=_SOURCES_TITLE,
            lines=_render_sources(bundle),
            mandatory=False,
            source_ids=tuple(source.source_id for source in bundle.sources),
            priority=3,
        ),
    )


def _identity_source_ids(bundle: SourceBundle) -> tuple[str, ...]:
    """Source ids of the recorded obstruction and formal-run artifacts."""
    return tuple(
        source.source_id
        for source in bundle.sources
        if source.source_type in {OBSTRUCTION_TYPE, FORMAL_RUN_TYPE}
    )


def _section_index(sections: Sequence[_Section]) -> dict[str, str]:
    index: dict[str, str] = {}
    for section in sections:
        for source_id in section.source_ids:
            index.setdefault(source_id, section.title)
    return index


def _decisions(
    *,
    bundle: SourceBundle,
    records: _Records,
    payload: ObstructionPayload,
    attempts: Sequence[AttemptEvidence],
    selected_ids: set[str],
    section_of: Mapping[str, str],
    dropped: set[str],
    left_out: Mapping[str, str],
) -> tuple[SelectionDecision, ...]:
    state_status = {
        state.source_id: state.status.value for state in records.states
    }
    attempt_status = {
        attempt.source_id: attempt.executor_result.value for attempt in attempts
    }
    attempt_state = {
        attempt.source_id: attempt.state_id for attempt in attempts
    }
    attempt_status.update(
        {
            attempt.source_id: attempt.executor_result.value
            for attempt in records.attempts
            if attempt.source_id not in attempt_status
        }
    )
    attempt_state.update(
        {
            attempt.source_id: attempt.state_id
            for attempt in records.attempts
            if attempt.source_id not in attempt_state
        }
    )
    diagnostic_status = {
        attempt.diagnostic_hash: attempt.executor_result.value
        for attempt in records.attempts
        if attempt.diagnostic_hash
    }
    used_diagnostics = set(payload.diagnostic_artifacts)
    identity_ids = {payload.obstruction_id, payload.formal_run_id}

    decisions: list[SelectionDecision] = []
    for source in bundle.sources:
        section = section_of.get(source.source_id)
        body = records.bodies.get(source.source_id, {})

        if source.source_type == FORMAL_STATE_TYPE:
            status = state_status[source.source_id]
            if source.source_id in selected_ids:
                included, reason = True, "selected failed state"
            else:
                included, reason = (
                    False,
                    left_out.get(
                        source.source_id,
                        "not selected: beyond the small relevant set",
                    ),
                )
        elif source.source_type == ATTEMPT_TYPE:
            status = attempt_status[source.source_id]
            if attempt_state[source.source_id] in selected_ids:
                included, reason = True, "attempt of a selected failed state"
            else:
                included, reason = False, "attempt of an unselected state"
        elif source.source_type == DIAGNOSTIC_TYPE:
            status = diagnostic_status.get(
                source.content_hash, AttemptStatus.PENDING.value
            )
            if source.content_hash in used_diagnostics:
                included, reason = True, "recurring diagnostic for a selected state"
            else:
                included, reason = False, "diagnostic not attached to a selected state"
        else:
            status = _identity_status(body, records)
            if source.source_id in identity_ids:
                included, reason = True, "obstruction and run identity"
            else:
                included, reason = False, "recorded obstruction or run not used"

        if included and section is not None and section in dropped:
            included, reason = False, REASON_OVER_TEXT_BUDGET

        decisions.append(
            SelectionDecision(
                source_id=source.source_id,
                source_version=source.source_version,
                status=status,
                included=included,
                reason=reason,
            )
        )
    return tuple(decisions)
