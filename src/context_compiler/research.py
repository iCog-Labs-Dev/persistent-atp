"""Research context compiler

``compile_research`` turns a selected research task and a ``SourceBundle`` into
one rendered ``ContextPacket`` inside the worker's text budget, plus a manifest
produced by ``finalize_packet``.

"""

from __future__ import annotations

import sys
import math
from dataclasses import dataclass
from typing import Mapping, Protocol

from shared.vocab import (
    AttemptStatus,
    ClaimStatus,
    ResearchMoveStatus,
    ResearchStateStatus,
)

from .budgeting import (
    CanonicalTokenCounter,
    Section,
    TokenCounter,
    fit_sections,
    token_counter_manifest,
)
from .contracts import (
    CompiledResult,
    CompileRequest,
    ContextPacket,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from .errors import ContextValidationError
from .finalize import finalize_packet
from .selection import (
    DEFAULT_CONTEXT_POLICY,
    ContextSelectionPolicy,
    REASON_OVER_TEXT_BUDGET,
    duplicate_reason,
    text_similarity,
)

DEFAULT_COMPILER_VERSION = "context/2"
DEFAULT_RENDERING_VERSION = "text/1"
_UNBOUNDED = sys.maxsize  # used when the request carries no text budget

OBSTRUCTION_TYPE = "obstruction"
STATE_TYPE = "research-state"
MOVE_TYPE = "research-move"
CLAIM_TYPE = "claim"

DEFAULT_SUPPORT_CATEGORIES: Mapping[str, str] = {
    "attempt": "failure",
    "obstruction": "failure",
    CLAIM_TYPE: "lemma",
    MOVE_TYPE: "alternative",
}
CATEGORY_RANK = {"failure": 0, "feedback": 1, "lemma": 2, "alternative": 3}
_CATEGORY_LABEL = {
    "failure": "Failure",
    "feedback": "Feedback",
    "lemma": "Reusable lemma",
    "alternative": "Alternative strategy",
}


# Statuses that make a source unsafe to present as usable context.
_DEAD_STATES = frozenset(
    {
        ResearchStateStatus.SUPERSEDED.value,
        ResearchStateStatus.REFUTED.value,
        ResearchStateStatus.STALE.value,
    }
)
_UNUSABLE_CLAIMS = frozenset(
    {
        ClaimStatus.REFUTED.value,
        ClaimStatus.RETRACTED.value,
        ClaimStatus.TAINTED.value,
        ClaimStatus.STALE.value,
    }
)
_FAILED_ATTEMPTS = frozenset(
    {AttemptStatus.REFUTED.value, AttemptStatus.RETRACTED.value}
)
_FRONTIER_MOVES = frozenset(
    {ResearchMoveStatus.QUEUED.value, ResearchMoveStatus.OPEN.value}
)
_SELECTABLE_MOVES = _FRONTIER_MOVES | {ResearchMoveStatus.LEASED.value}

class ArtifactReader(Protocol):
    """Read-only access to complete source artifacts by ``source_id``.
    """

    def fetch(self, source_id: str) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class ResearchTask:
    kernel: TheoremKernel
    kernel_version: str
    active_state_id: str
    selected_move_id: str
    instructions: str
    output_schema: str
    tool_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.kernel, TheoremKernel):
            raise ContextValidationError("kernel must be a TheoremKernel")
        for name in (
            "kernel_version",
            "active_state_id",
            "selected_move_id",
            "instructions",
            "output_schema",
            "tool_policy",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContextValidationError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class ResearchCompilation:
    result: CompiledResult
    token_count: int

    @property
    def packet(self) -> ContextPacket:
        return self.result.packet

    @property
    def manifest(self) -> Mapping[str, object]:
        return self.result.manifest

    @property
    def packet_digest(self) -> str:
        return self.result.packet_digest


# --------------------------------------------------------------------------
# body parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Source:
    ref: SourceRef
    status: str
    text: str
    body: Mapping[str, object]

    @property
    def state_id(self) -> str | None:
        value = self.body.get("state_id")
        if not isinstance(value, str):
            return None
        if "/" in value:
            return value
        proof_id, separator, _ = self.ref.source_id.partition("/")
        return f"{proof_id}/{value}" if separator else value

    @property
    def move_id(self) -> str | None:
        value = self.body.get("move_id")
        return value if isinstance(value, str) else None


def _string_list(ref: SourceRef, body: Mapping[str, object], name: str) -> tuple[str, ...]:
    raw = body.get(name, ())
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ContextValidationError(
            f"{ref.source_id}: {name} must be a list of non-empty strings"
        )
    return tuple(raw)


def _load(ref: SourceRef, reader: ArtifactReader) -> _Source:
    body = reader.fetch(ref.source_id)
    if not isinstance(body, Mapping):
        raise ContextValidationError(f"{ref.source_id}: body must be a mapping")
    status = body.get("status")
    if status is None and ref.source_type == OBSTRUCTION_TYPE:
        # Recorded obstructions carry no lifecycle status (see obstruction.py).
        status = AttemptStatus.PENDING.value
    if not isinstance(status, str) or not status.strip():
        raise ContextValidationError(f"{ref.source_id}: body needs a status")
    text = ""
    for field in ("claim_text", "statement", "summary", "detail", "description", "goal"):
        candidate = body.get(field)
        if isinstance(candidate, str) and candidate.strip():
            text = candidate
            break
    if not text:
        text = _fallback_text(ref, body)
    if not text:
        raise ContextValidationError(
            f"{ref.source_id}: body needs statement, summary, detail, description or goal"
        )
    return _Source(ref=ref, status=status, text=text, body=body)


def _fallback_text(ref: SourceRef, body: Mapping[str, object]) -> str:
    """Text for records whose committed fields carry no prose of their own."""
    if ref.source_type == "attempt":
        actor = body.get("actor") or "unknown actor"
        role = body.get("worker_class") or "worker"
        return f"Attempt by {actor} ({role}): {body.get('status')}."
    if ref.source_type == STATE_TYPE:
        origin = body.get("origin_state")
        if isinstance(origin, str) and origin.strip():
            return f"Opened from formal state {origin}."
        return "(no description recorded)"
    if ref.source_type == OBSTRUCTION_TYPE:
        parts = []
        kind = body.get("kind")
        if isinstance(kind, str) and kind.strip():
            parts.append(f"Obstruction kind: {kind}.")
        evidence = body.get("evidence")
        if isinstance(evidence, (list, tuple)):
            parts.extend(
                str(entry["note"])
                for entry in evidence
                if isinstance(entry, Mapping) and entry.get("note")
            )
        return " ".join(parts)
    return ""


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _selection_features(
    source: _Source,
    category: str,
    active_state: _Source,
    selected_move: _Source,
    *,
    related: bool,
) -> dict[str, float]:
    """Normalize committed source metadata into the shared MMR feature vector."""
    annotations = source.body.get("annotations")

    def number(*names: str) -> float | None:
        for name in names:
            raw = source.body.get(name)
            if raw is None and isinstance(annotations, Mapping):
                raw = annotations.get(name)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                continue
            return float(raw)
        return None

    def unit(value: float | None, default: float = 0.5) -> float:
        return default if value is None else min(1.0, max(0.0, value))

    similarity = max(
        text_similarity(source.text, active_state.text),
        text_similarity(source.text, selected_move.text),
    )
    status = source.status
    verification = {
        ClaimStatus.LEAN_VERIFIED.value: 1.0,
        ClaimStatus.FORMALLY_CLOSED.value: 0.9,
        ClaimStatus.CRITIC_ACCEPTED.value: 0.7,
        ClaimStatus.PROVISIONAL.value: 0.45,
        ClaimStatus.CONJECTURAL.value: 0.3,
    }.get(status, 0.5)
    dependency_count = source.body.get("dependency_count")
    dependency = number("dependency_centrality")
    if dependency is None and isinstance(dependency_count, int) and not isinstance(dependency_count, bool):
        dependency = min(1.0, max(0, dependency_count) / 8.0)

    pln_values = [value for value in (number("pln_strength"), number("pln_confidence")) if value is not None]
    pln = sum(unit(value) for value in pln_values) / len(pln_values) if pln_values else 0.5
    proof_number = number("proof_number")
    proof_score = 1.0 / (1.0 + max(0.0, proof_number)) if proof_number is not None else 0.5
    novelty = number("state_novelty", "novelty")
    transpositions = number("transposition_count")
    if novelty is None and transpositions is not None:
        novelty = 1.0 / (1.0 + max(0.0, transpositions))
    if novelty is None:
        novelty = 1.0 - similarity
    failure = 1.0 if category == "failure" else 0.75 if category == "feedback" else 0.25
    return {
        "dependency": unit(dependency),
        "ancestry": 1.0 if related else 0.0,
        "semantic_similarity": unit(similarity, 0.0),
        "verification": verification,
        "failure": failure,
        "pln": pln,
        "proof_number": unit(proof_score),
        "novelty": unit(novelty),
    }


def _signature(category: str, src: _Source) -> tuple:
    # Status and proof obligations are part of identity, so two claims with the
    # same statement but different statuses, or two moves with the same text but
    # different required claims, are never merged.
    obligations = tuple(sorted(src.body.get("required_claim_ids", ()) or ()))
    goals = tuple(sorted(src.body.get("open_goals", ()) or ()))
    return (category, src.status, _normalize(src.text), obligations, goals)


# --------------------------------------------------------------------------
# compiler
# --------------------------------------------------------------------------

def compile_research(
    request: CompileRequest,
    bundle: SourceBundle,
    reader: ArtifactReader,
    task: ResearchTask,
    *,
    token_counter: TokenCounter | None = None,
    support_categories: Mapping[str, str] = DEFAULT_SUPPORT_CATEGORIES,
    selection_policy: ContextSelectionPolicy = DEFAULT_CONTEXT_POLICY,
    compiler_version: str = DEFAULT_COMPILER_VERSION,
    rendering_version: str = DEFAULT_RENDERING_VERSION,
) -> ResearchCompilation:
    if request.packet_kind is not PacketKind.RESEARCH:
        raise ContextValidationError("request packet_kind must be research")
    if not isinstance(task, ResearchTask):
        raise ContextValidationError("task must be a ResearchTask")
    if not isinstance(selection_policy, ContextSelectionPolicy):
        raise ContextValidationError("selection_policy must be a ContextSelectionPolicy")
    counter = token_counter if token_counter is not None else CanonicalTokenCounter()
    if not isinstance(counter, TokenCounter):
        raise ContextValidationError("token_counter must provide count(text)")

    known_types = {STATE_TYPE, MOVE_TYPE, CLAIM_TYPE, *support_categories}
    refs = sorted(bundle.sources, key=lambda r: r.source_id)
    for ref in refs:
        if ref.source_type not in known_types:
            raise ContextValidationError(
                f"{ref.source_id}: unknown source type {ref.source_type!r}"
            )
    sources = {ref.source_id: _load(ref, reader) for ref in refs}

    active_state = sources.get(task.active_state_id)
    if active_state is None or active_state.ref.source_type != STATE_TYPE:
        raise ContextValidationError("active state is not a research-state in the bundle")
    selected_move = sources.get(task.selected_move_id)
    if selected_move is None or selected_move.ref.source_type != MOVE_TYPE:
        raise ContextValidationError("selected move is not a research-move in the bundle")

    if selected_move.state_id != task.active_state_id:
        raise ContextValidationError(
            f"selected move {task.selected_move_id} belongs to state "
            f"{selected_move.state_id!r}, not the active state {task.active_state_id}"
        )
    if selected_move.status not in _SELECTABLE_MOVES:
        raise ContextValidationError(
            f"selected move {task.selected_move_id} is {selected_move.status}"
        )
    if active_state.status in _DEAD_STATES:
        raise ContextValidationError(
            f"active state {task.active_state_id} is {active_state.status}"
        )
    required_ids = _string_list(
        selected_move.ref, selected_move.body, "required_claim_ids"
    )
    required: list[_Source] = []
    for claim_id in required_ids:
        claim = sources.get(claim_id)
        if claim is None or claim.ref.source_type != CLAIM_TYPE:
            raise ContextValidationError(f"required claim {claim_id} is missing from bundle")
        if claim.status in _UNUSABLE_CLAIMS:
            raise ContextValidationError(
                f"required claim {claim_id} is {claim.status}"
            )
        required.append(claim)
    open_goals = _string_list(active_state.ref, active_state.body, "open_goals")
    assumptions = _string_list(active_state.ref, active_state.body, "assumptions")

    reserved = {task.active_state_id, task.selected_move_id, *required_ids}
    decisions: dict[str, SelectionDecision] = {}

    def decide(src: _Source, included: bool, reason: str,
               score_breakdown: Mapping[str, float] | None = None) -> None:
        decisions[src.ref.source_id] = SelectionDecision(
            source_id=src.ref.source_id,
            source_version=src.ref.source_version,
            status=src.status,
            included=included,
            reason=reason,
            selection_score=(score_breakdown.get("mmr_score") if score_breakdown else None),
            score_breakdown=score_breakdown or {},
            selection_policy=selection_policy.name if score_breakdown else None,
        )

    decide(active_state, True, "active branch state")
    decide(selected_move, True, "selected move")
    for claim in required:
        decide(claim, True, "required by selected move")

    # ---- supporting candidates --------------------------------------------
    candidates: list[tuple[tuple[int, int, str], str, _Source, Mapping[str, float]]] = []
    for ref in refs:
        if ref.source_id in reserved:
            continue
        src = sources[ref.source_id]
        category = _support_category(ref.source_type, support_categories)
        if ref.source_type == "attempt" and src.body.get("worker_class") == "critic":
            category = "feedback"
        formal_ids = src.body.get("formal_state_ids")
        origin = active_state.body.get("origin_state")
        related = (
            src.state_id == task.active_state_id
            or src.move_id == task.selected_move_id
            or (
                isinstance(origin, str)
                and isinstance(formal_ids, (list, tuple))
                and origin in formal_ids
            )
        )
        if ref.source_type == STATE_TYPE:
            decide(src, False, "not the active branch state")
        elif (
            ref.source_type == "attempt"
            and category == "failure"
            and src.status not in _FAILED_ATTEMPTS
        ):
            decide(src, False, f"attempt status {src.status} is not a recorded failure")
        elif category in ("failure", "feedback") and not related:
            decide(src, False, "not related to active state or selected move")
        elif category == "lemma" and src.body.get("reusable") is not True:
            decide(src, False, "claim is not marked reusable")
        elif category == "lemma" and src.status in _UNUSABLE_CLAIMS:
            decide(src, False, f"claim status {src.status} is not usable as a lemma")
        elif category == "alternative" and src.state_id != task.active_state_id:
            decide(src, False, "move belongs to a different state")
        elif category == "alternative" and src.status not in _FRONTIER_MOVES:
            decide(src, False, f"move status {src.status} is not on the frontier")
        else:
            relevance = 0 if related or src.state_id == task.active_state_id else 1
            features = _selection_features(
                src, category, active_state, selected_move, related=related,
            )
            candidates.append(((CATEGORY_RANK[category], relevance, ref.source_id), category, src, features))

    candidates.sort(key=lambda item: item[0])
    seen: dict[tuple, str] = {}
    supporting: list[tuple[tuple[int, int, str], str, _Source, Mapping[str, float]]] = []
    for key, category, src, features in candidates:
        signature = _signature(category, src)
        if signature in seen:
            decide(src, False, duplicate_reason(seen[signature]))
        else:
            seen[signature] = src.ref.source_id
            supporting.append((key, category, src, features))

    # ---- sections ----------------------------------------------------------
    kernel = task.kernel
    kernel_lines = [kernel.statement]
    if kernel.assumptions:
        kernel_lines.append("Assumptions:\n" + "\n".join(f"- {a}" for a in kernel.assumptions))
    branch_lines = [f"State {active_state.ref.source_id} [{active_state.status}]: {active_state.text}"]
    if assumptions:
        branch_lines.append("Assumptions:\n" + "\n".join(f"- {a}" for a in assumptions))
    sections: list[Section] = [
        Section("instructions", "Worker instructions", task.instructions, mandatory=True),
        Section(
            "kernel",
            f"Theorem kernel {kernel.kernel_id} [{kernel.status.value}]",
            "\n".join(kernel_lines),
            mandatory=True,
        ),
        Section("state", "Active branch", "\n".join(branch_lines), mandatory=True),
        Section(
            "move",
            f"Selected move {selected_move.ref.source_id} [{selected_move.status}]",
            selected_move.text,
            mandatory=True,
        ),
        Section(
            "required",
            "Required claims",
            "\n".join(f"- {c.ref.source_id} [{c.status}]: {c.text}" for c in required)
            or "(none)",
            mandatory=True,
        ),
        Section(
            "goals",
            "Open goals",
            "\n".join(f"- {g}" for g in open_goals) or "(none recorded)",
            mandatory=True,
        ),
    ]
    for key, category, src, features in supporting:
        sections.append(
            Section(
                key=f"support:{src.ref.source_id}",
                heading=f"{_CATEGORY_LABEL[category]} {src.ref.source_id} [{src.status}]",
                body=src.text,
                priority=key[0] * 2 + key[1],
                source_id=src.ref.source_id,
                features=features,
                similarity_text=src.text,
            )
        )
    sections.append(Section("schema", "Output schema", task.output_schema, mandatory=True))
    sections.append(Section("tools", "Tool policy", task.tool_policy, mandatory=True))

    budget = request.text_budget or TextBudget(_UNBOUNDED)
    fit = fit_sections(sections, budget, counter, policy=selection_policy)
    kept_ids = {s.source_id for s in fit.kept if s.source_id}
    for _, category, src, _features in supporting:
        if src.ref.source_id in kept_ids:
            decision_key = f"support:{src.ref.source_id}"
            breakdown = fit.score_breakdowns.get(decision_key)
            decide(src, True, fit.reasons.get(decision_key, f"supporting {category}"), breakdown)
        else:
            decision_key = f"support:{src.ref.source_id}"
            breakdown = fit.score_breakdowns.get(decision_key)
            decide(src, False, fit.reasons.get(decision_key, REASON_OVER_TEXT_BUDGET), breakdown)

    # ---- finalize -----------------------------------------------------------
    ordered_decisions = (
        SelectionDecision(
            source_id=kernel.kernel_id,
            source_version=task.kernel_version,
            status=kernel.status.value,
            included=True,
            reason="theorem kernel",
        ),
        *(decisions[ref.source_id] for ref in refs),
    )
    packet = ContextPacket(
        task_id=request.task_id,
        proof_id=request.proof_id,
        base_revision=request.base_revision,
        packet_kind=PacketKind.RESEARCH,
        content=fit.text,
        text_budget=request.text_budget,
    )
    result = finalize_packet(
        packet,
        ordered_decisions,
        compiler_version=compiler_version,
        rendering_version=rendering_version,
        selection_policy_configs={selection_policy.name: selection_policy.manifest_config(
            cost_normalization="counter.count(render([section])) / text_budget.max_tokens",
        )},
        token_counter_config=token_counter_manifest(counter),
    )
    return ResearchCompilation(result=result, token_count=fit.token_count)


def _support_category(source_type: str, categories: Mapping[str, str]) -> str:
    if source_type == STATE_TYPE:
        return "state"
    category = categories.get(source_type)
    if category not in CATEGORY_RANK:
        raise ContextValidationError(f"no supporting category for {source_type!r}")
    return category
