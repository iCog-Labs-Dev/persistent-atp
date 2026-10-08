"""Deterministic text budgeting shared by the context compilers.

A packet is a list of ``Section`` objects.  Mandatory sections are always kept
(and the fit fails loudly if they alone exceed the budget).  Optional sections
are admitted greedily in priority order (lower number = more important, ties
broken by original position) and are dropped, or truncated when marked
``truncatable``, if the rendered packet would exceed the budget.

"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Protocol, Sequence, runtime_checkable

from .contracts import TextBudget
from .errors import ContextValidationError

TRUNCATION_MARKER = " …[truncated]"


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


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

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token) if text else 0


@dataclass(frozen=True)
class Section:
    key: str
    heading: str
    body: str
    priority: int = 0
    mandatory: bool = False
    truncatable: bool = False
    source_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("key", "heading"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContextValidationError(f"{name} must be a non-empty string")
        if not isinstance(self.body, str):
            raise ContextValidationError("body must be a string")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ContextValidationError("priority must be an integer")


@dataclass(frozen=True)
class FitResult:
    text: str
    token_count: int
    kept: tuple[Section, ...]       # original order; bodies may be truncated
    dropped: tuple[Section, ...]    # original order
    truncated_keys: tuple[str, ...]


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


def _fits(
    sections: Sequence[Section], budget: int, counter: TokenCounter
) -> bool:
    return counter.count(render_sections(sections)) <= budget


def _truncate_to_fit(
    candidate: Section,
    chosen_in_order: Sequence[Section],
    position: int,
    budget: int,
    counter: TokenCounter,
) -> Section | None:
    def trial(body: str) -> list[Section]:
        updated = replace(candidate, body=body)
        return [*chosen_in_order[:position], updated, *chosen_in_order[position:]]

    lo, hi, best = 0, len(candidate.body), None
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = truncate_at_whitespace(candidate.body, mid)
        body = f"{cut}{TRUNCATION_MARKER}" if cut else ""
        if cut and _fits(trial(body), budget, counter):
            best = replace(candidate, body=body)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def fit_sections(
    sections: Sequence[Section],
    budget: TextBudget,
    counter: TokenCounter,
) -> FitResult:
    """Fit ``sections`` into ``budget`` deterministically."""
    keys = [s.key for s in sections]
    if len(keys) != len(set(keys)):
        raise ContextValidationError("section keys must be unique")
    index = {s.key: i for i, s in enumerate(sections)}

    chosen: dict[str, Section] = {s.key: s for s in sections if s.mandatory}

    def ordered() -> list[Section]:
        return [chosen[s.key] for s in sections if s.key in chosen]

    if not _fits(ordered(), budget.max_tokens, counter):
        raise ContextValidationError(
            "mandatory sections exceed the text budget"
        )

    optional = sorted(
        (s for s in sections if not s.mandatory),
        key=lambda s: (s.priority, index[s.key]),
    )
    truncated: list[str] = []
    for cand in optional:
        chosen[cand.key] = cand
        current = ordered()
        if _fits(current, budget.max_tokens, counter):
            continue
        del chosen[cand.key]
        if cand.truncatable:
            before = ordered()
            position = sum(1 for s in before if index[s.key] < index[cand.key])
            shortened = _truncate_to_fit(
                cand, before, position, budget.max_tokens, counter
            )
            if shortened is not None:
                chosen[cand.key] = shortened
                truncated.append(cand.key)

    kept = tuple(ordered())
    text = render_sections(kept)
    return FitResult(
        text=text,
        token_count=counter.count(text),
        kept=kept,
        dropped=tuple(s for s in sections if s.key not in chosen),
        truncated_keys=tuple(sorted(truncated, key=index.__getitem__)),
    )