"""Deterministic text budgeting shared by the context compilers.

A packet is a list of ``Section`` objects.  Mandatory sections are always kept
(and the fit fails loudly if they alone exceed the budget).  Optional sections
are admitted greedily in priority order (lower number = more important, ties
broken by original position) and are dropped, or truncated when marked
``truncatable``, if the rendered packet would exceed the budget.

"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import TextBudget
from .errors import ContextValidationError
from .selection import (
    CONTEXT_FEATURES,
    ContextSelectionPolicy,
    DEFAULT_CONTEXT_POLICY,
    REASON_OVER_TEXT_BUDGET,
    categorical_similarity,
    text_similarity,
)

TRUNCATION_MARKER = " …[truncated]"


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class CanonicalTokenCounter:
    """Count with the project-wide ``cl100k_base`` canonical encoding."""

    encoding_name = "cl100k_base"

    def __init__(self) -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover - installation failure path
            raise ContextValidationError(
                "CanonicalTokenCounter requires the tiktoken dependency"
            ) from exc
        self._encoding = tiktoken.get_encoding(self.encoding_name)
        try:
            self._library_version = version("tiktoken")
        except PackageNotFoundError:  # pragma: no cover - nonstandard vendoring
            self._library_version = "unknown"

    def count(self, text: str) -> int:
        if not isinstance(text, str):
            raise ContextValidationError("token counter input must be a string")
        # Treat special-token spellings as ordinary packet text. A context
        # packet cannot introduce control tokens by including their spelling.
        return len(self._encoding.encode(text, disallowed_special=()))

    def manifest_config(self) -> dict[str, str]:
        return {
            "algorithm": "tiktoken-encoding",
            "encoding": self.encoding_name,
            "library": "tiktoken",
            "library_version": self._library_version,
            "special_token_handling": "encode_as_ordinary_text",
        }


class CharHeuristicTokenCounter:
    """Deterministic approximation: ceil(len(text) / chars_per_token)."""

    def __init__(self, chars_per_token: int = 4) -> None:
        if (
            isinstance(chars_per_token, bool)
            or not isinstance(chars_per_token, int)
            or chars_per_token <= 0
        ):
            raise ContextValidationError(
                "chars_per_token must be a positive integer"
            )
        self._chars_per_token = chars_per_token

    @property
    def chars_per_token(self) -> int:
        return self._chars_per_token

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token) if text else 0

    def manifest_config(self) -> dict[str, object]:
        return {
            "algorithm": "character-length-heuristic",
            "chars_per_token": self._chars_per_token,
            "unit": "estimated tokens",
        }


def token_counter_manifest(counter: TokenCounter) -> dict[str, object]:
    """Describe the exact configured counter for deterministic packet audits."""
    describe = getattr(counter, "manifest_config", None)
    if callable(describe):
        config = describe()
        if isinstance(config, Mapping):
            return dict(config)
    return {
        "algorithm": "custom-token-counter",
        "implementation": f"{type(counter).__module__}.{type(counter).__qualname__}",
        "version": "caller-managed",
    }


@dataclass(frozen=True)
class Section:
    key: str
    heading: str
    body: str
    priority: int = 0
    mandatory: bool = False
    truncatable: bool = False
    source_id: str | None = None
    features: Mapping[str, float] | None = None
    similarity_text: str | None = None
    diversity_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("key", "heading"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContextValidationError(f"{name} must be a non-empty string")
        if not isinstance(self.body, str):
            raise ContextValidationError("body must be a string")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ContextValidationError("priority must be an integer")
        if self.features is not None:
            if not isinstance(self.features, Mapping) or set(self.features) - set(CONTEXT_FEATURES):
                raise ContextValidationError("section features contain an unknown context score")
            for name, value in self.features.items():
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ContextValidationError(f"section feature {name} must be within [0,1]")
        if self.similarity_text is not None and not isinstance(self.similarity_text, str):
            raise ContextValidationError("similarity_text must be a string")
        if not isinstance(self.diversity_keys, tuple) or not all(
            isinstance(item, str) and item for item in self.diversity_keys
        ):
            raise ContextValidationError("diversity_keys must be a tuple of non-empty strings")


@dataclass(frozen=True)
class FitResult:
    text: str
    token_count: int
    kept: tuple[Section, ...]       # original order; bodies may be truncated
    dropped: tuple[Section, ...]    # original order
    truncated_keys: tuple[str, ...]
    score_breakdowns: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    reasons: Mapping[str, str] = field(default_factory=dict)


def render_sections(sections: Sequence[Section]) -> str:
    return "\n\n".join(f"## {s.heading}\n{s.body}" for s in sections)


def truncate_at_whitespace(text: str, max_chars: int) -> str:
    """Cut ``text`` to at most ``max_chars`` on a whitespace boundary."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if not text[max_chars].isspace():
        boundary = max(
            (i for i, ch in enumerate(cut) if ch.isspace()), default=-1
        )
        cut = cut[:boundary] if boundary >= 0 else ""
    return cut.rstrip()


Renderer = Callable[[Sequence[Section]], str]


def _fits(
    sections: Sequence[Section],
    budget: int,
    counter: TokenCounter,
    render: Renderer = render_sections,
) -> bool:
    return counter.count(render(sections)) <= budget


def _truncate_to_fit(
    candidate: Section,
    chosen_in_order: Sequence[Section],
    position: int,
    budget: int,
    counter: TokenCounter,
    render: Renderer = render_sections,
) -> Section | None:
    def trial(body: str) -> list[Section]:
        updated = replace(candidate, body=body)
        return [*chosen_in_order[:position], updated, *chosen_in_order[position:]]

    lo, hi, best = 0, len(candidate.body), None
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = truncate_at_whitespace(candidate.body, mid)
        body = f"{cut}{TRUNCATION_MARKER}" if cut else ""
        if cut and _fits(trial(body), budget, counter, render):
            best = replace(candidate, body=body)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def fit_sections(
    sections: Sequence[Section],
    budget: TextBudget,
    counter: TokenCounter,
    render: Renderer = render_sections,
    policy: ContextSelectionPolicy = DEFAULT_CONTEXT_POLICY,
) -> FitResult:
    """Fit ``sections`` into ``budget`` deterministically.

    ``render`` turns the kept sections into packet text.  Compilers that keep
    their own wire format pass it in; selection and budgeting stay shared.
    """
    keys = [s.key for s in sections]
    if len(keys) != len(set(keys)):
        raise ContextValidationError("section keys must be unique")
    index = {s.key: i for i, s in enumerate(sections)}

    chosen: dict[str, Section] = {s.key: s for s in sections if s.mandatory}

    def ordered() -> list[Section]:
        return [chosen[s.key] for s in sections if s.key in chosen]

    if not _fits(ordered(), budget.max_tokens, counter, render):
        raise ContextValidationError(
            "mandatory sections exceed the text budget"
        )

    optional = sorted(
        (s for s in sections if not s.mandatory),
        key=lambda s: (s.priority, index[s.key]),
    )
    truncated: list[str] = []
    score_breakdowns: dict[str, Mapping[str, float]] = {}
    reasons: dict[str, str] = {}

    # Keep the historical stable priority policy for call sites that have not
    # supplied auditable features yet. Scored packet builders use the shared
    # weighted-MMR path below.
    if any(section.features is not None for section in optional):
        remaining = {section.key: section for section in optional}

        def similarity_text(section: Section) -> str:
            return section.similarity_text if section.similarity_text is not None else section.body

        def scored(section: Section) -> tuple[float, dict[str, float]]:
            features = dict(section.features or {})
            selected_sections = ordered()
            lexical_redundancy = max(
                (text_similarity(similarity_text(section), similarity_text(selected))
                 for selected in selected_sections),
                default=0.0,
            )
            mechanism_keys = lambda item: tuple(
                key for key in item.diversity_keys
                if key.startswith(("tactic:", "failure:", "executor:"))
            )
            source_keys = lambda item: tuple(
                key for key in item.diversity_keys
                if key.startswith(("state:", "source:"))
            )
            mechanism_redundancy = max((
                categorical_similarity(mechanism_keys(section), mechanism_keys(selected))
                for selected in selected_sections
            ), default=0.0)
            source_redundancy = max((
                categorical_similarity(source_keys(section), source_keys(selected))
                for selected in selected_sections
            ), default=0.0)
            redundancy = (
                0.50 * lexical_redundancy
                + 0.35 * mechanism_redundancy
                + 0.15 * source_redundancy
                if section.diversity_keys else lexical_redundancy
            )
            token_cost = counter.count(render([section]))
            cost_fraction = min(1.0, token_cost / budget.max_tokens)
            breakdown = policy.breakdown(features, redundancy=redundancy,
                token_cost=float(token_cost), cost_fraction=cost_fraction,
                mechanism_redundancy=mechanism_redundancy,
                source_redundancy=source_redundancy)
            return breakdown["mmr_score"], breakdown

        while remaining:
            ranked = []
            for position, section in enumerate(sections):
                if section.key not in remaining:
                    continue
                score, breakdown = scored(section)
                ranked.append((score, section.priority, position, section, breakdown))
            ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
            selected_one = False
            for score, _priority, _position, candidate, breakdown in ranked:
                chosen[candidate.key] = candidate
                if _fits(ordered(), budget.max_tokens, counter, render):
                    score_breakdowns[candidate.key] = breakdown
                    reasons[candidate.key] = "included by weighted MMR"
                    del remaining[candidate.key]
                    selected_one = True
                    break
                del chosen[candidate.key]
                if candidate.truncatable:
                    before = ordered()
                    position = sum(1 for item in before if index[item.key] < index[candidate.key])
                    shortened = _truncate_to_fit(candidate, before, position,
                        budget.max_tokens, counter, render)
                    if shortened is not None:
                        chosen[candidate.key] = shortened
                        actual_cost = counter.count(render([shortened]))
                        cost_fraction = min(1.0, actual_cost / budget.max_tokens)
                        breakdown = policy.breakdown(candidate.features or {},
                            redundancy=breakdown["redundancy"], token_cost=float(actual_cost),
                            cost_fraction=cost_fraction,
                            mechanism_redundancy=breakdown["mechanism_redundancy"],
                            source_redundancy=breakdown["source_redundancy"])
                        score_breakdowns[candidate.key] = breakdown
                        reasons[candidate.key] = "included by weighted MMR (truncated)"
                        truncated.append(candidate.key)
                        del remaining[candidate.key]
                        selected_one = True
                        break
                del remaining[candidate.key]
                score_breakdowns[candidate.key] = breakdown
                reasons[candidate.key] = REASON_OVER_TEXT_BUDGET
            if not selected_one and not remaining:
                break

        kept = tuple(ordered())
        text = render(kept)
        return FitResult(text=text, token_count=counter.count(text), kept=kept,
            dropped=tuple(section for section in sections if section.key not in chosen),
            truncated_keys=tuple(sorted(truncated, key=index.__getitem__)),
            score_breakdowns=score_breakdowns, reasons=reasons)

    for cand in optional:
        chosen[cand.key] = cand
        current = ordered()
        if _fits(current, budget.max_tokens, counter, render):
            reasons[cand.key] = "included by fixed priority"
            continue
        del chosen[cand.key]
        if cand.truncatable:
            before = ordered()
            position = sum(1 for s in before if index[s.key] < index[cand.key])
            shortened = _truncate_to_fit(
                cand, before, position, budget.max_tokens, counter, render
            )
            if shortened is not None:
                chosen[cand.key] = shortened
                truncated.append(cand.key)
                reasons[cand.key] = "included by fixed priority (truncated)"
            else:
                reasons[cand.key] = REASON_OVER_TEXT_BUDGET
        else:
            reasons[cand.key] = REASON_OVER_TEXT_BUDGET

    kept = tuple(ordered())
    text = render(kept)
    return FitResult(
        text=text,
        token_count=counter.count(text),
        kept=kept,
        dropped=tuple(s for s in sections if s.key not in chosen),
        truncated_keys=tuple(sorted(truncated, key=index.__getitem__)),
        reasons=reasons,
    )
