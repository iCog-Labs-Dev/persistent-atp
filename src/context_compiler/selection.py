"""

Every compiler (research, formal, obstruction) follows the same small,
deterministic, explainable rules.  They may use different categories and
rendering formats; they do not use different *policy*.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Callable, Generic, Mapping, Sequence, TypeVar

from .errors import ContextValidationError

T = TypeVar("T")

REASON_OVER_TEXT_BUDGET = "dropped: over text budget"
REASON_OVER_PREMISE_LIMIT = "dropped: over premise limit"
REASON_OVER_PRIOR_RUN_LIMIT = "dropped: over prior-run limit"
REASON_OVER_TRANSPOSITION_LIMIT = "dropped: over transposition limit"
REASON_OVER_FAILED_STATE_LIMIT = "dropped: over failed-state limit"

CONTEXT_FEATURES = (
    "dependency",
    "ancestry",
    "semantic_similarity",
    "verification",
    "failure",
    "pln",
    "proof_number",
    "novelty",
)
CONTEXT_NEUTRAL_FEATURES = {name: 0.5 for name in CONTEXT_FEATURES}


@dataclass(frozen=True)
class ContextSelectionPolicy:
    """Deterministic relevance/diversity policy for optional packet sections."""

    name: str = "weighted-mmr-v1"
    weights: Mapping[str, float] = field(default_factory=lambda: {
        "dependency": 0.16,
        "ancestry": 0.14,
        "semantic_similarity": 0.18,
        "verification": 0.14,
        "failure": 0.12,
        "pln": 0.10,
        "proof_number": 0.08,
        "novelty": 0.08,
    })
    relevance_weight: float = 0.75
    token_cost_weight: float = 0.10

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ContextValidationError("selection policy name must be non-empty")
        if not isinstance(self.weights, Mapping) or set(self.weights) != set(CONTEXT_FEATURES):
            raise ContextValidationError("selection policy weights must cover every context feature")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 for value in self.weights.values()):
            raise ContextValidationError("selection weights must be finite nonnegative numbers")
        if sum(self.weights.values()) <= 0:
            raise ContextValidationError("selection weights must have positive total weight")
        for name in ("relevance_weight", "token_cost_weight"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ContextValidationError(f"{name} must be a finite number")
        if not 0 <= self.relevance_weight <= 1 or self.token_cost_weight < 0:
            raise ContextValidationError("MMR relevance weight must be in [0,1] and token cost nonnegative")

    @staticmethod
    def normalize_features(features: Mapping[str, float]) -> dict[str, float]:
        if not isinstance(features, Mapping) or set(features) - set(CONTEXT_FEATURES):
            raise ContextValidationError("selection features must use the shared context vocabulary")
        normalized = dict(CONTEXT_NEUTRAL_FEATURES)
        for name, value in features.items():
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ContextValidationError(f"selection feature {name} must be finite and within [0,1]")
            normalized[name] = float(value)
        return normalized

    def relevance(self, features: Mapping[str, float]) -> float:
        normalized = self.normalize_features(features)
        total = sum(self.weights.values())
        return sum(self.weights[name] * normalized[name] for name in CONTEXT_FEATURES) / total

    def breakdown(self, features: Mapping[str, float], *, redundancy: float,
                  token_cost: float, cost_fraction: float,
                  mechanism_redundancy: float = 0.0,
                  source_redundancy: float = 0.0) -> dict[str, float]:
        normalized = self.normalize_features(features)
        relevance = self.relevance(normalized)
        score = (self.relevance_weight * relevance
            - (1.0 - self.relevance_weight) * min(1.0, max(0.0, redundancy))
            - self.token_cost_weight * min(1.0, max(0.0, cost_fraction)))
        return {**normalized, "relevance": relevance,
            "redundancy": min(1.0, max(0.0, redundancy)),
            "mechanism_redundancy": min(1.0, max(0.0, mechanism_redundancy)),
            "source_redundancy": min(1.0, max(0.0, source_redundancy)),
            "token_cost": max(0.0, token_cost),
            "token_cost_fraction": min(1.0, max(0.0, cost_fraction)),
            "mmr_score": score}

    def manifest_config(self, *, cost_normalization: str) -> dict[str, object]:
        if not isinstance(cost_normalization, str) or not cost_normalization.strip():
            raise ContextValidationError("cost_normalization must be a non-empty string")
        return {
            "algorithm": "weighted-max-marginal-relevance-v1",
            "score_formula": (
                "relevance_weight * weighted_mean(features) "
                "- (1 - relevance_weight) * max_redundancy "
                "- token_cost_weight * normalized_token_cost"
            ),
            "relevance_normalization": "divide weighted feature sum by sum(weights)",
            "redundancy_composition": (
                "0.50*lexical + 0.35*mechanism + 0.15*source when identity keys are present; "
                "lexical otherwise"
            ),
            "name": self.name,
            "weights": {name: float(self.weights[name]) for name in CONTEXT_FEATURES},
            "feature_defaults": dict(CONTEXT_NEUTRAL_FEATURES),
            "relevance_weight": float(self.relevance_weight),
            "token_cost_weight": float(self.token_cost_weight),
            "similarity": ["lexical-jaccard-token-set-v1", "mechanism-source-jaccard-v1"],
            "cost_normalization": cost_normalization,
        }


DEFAULT_CONTEXT_POLICY = ContextSelectionPolicy()


def text_similarity(left: str, right: str) -> float:
    """Version-1 deterministic lexical overlap used only for diversity ranking."""
    if not isinstance(left, str) or not isinstance(right, str):
        raise ContextValidationError("MMR similarity inputs must be strings")
    left_tokens = set(re.findall(r"[\w]+", left.casefold()))
    right_tokens = set(re.findall(r"[\w]+", right.casefold()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def categorical_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Jaccard overlap for versioned mechanism/source identity keys."""
    left_keys, right_keys = set(left), set(right)
    if not left_keys or not right_keys:
        return 0.0
    return len(left_keys & right_keys) / len(left_keys | right_keys)


@dataclass(frozen=True)
class RankedItem(Generic[T]):
    item: T
    score_breakdown: Mapping[str, float]


def weighted_mmr_order(
    items: Sequence[T],
    *,
    features: Callable[[T], Mapping[str, float]],
    similarity_text: Callable[[T], str],
    identity: Callable[[T], str],
    token_cost: Callable[[T], int] | None = None,
    policy: ContextSelectionPolicy = DEFAULT_CONTEXT_POLICY,
) -> tuple[RankedItem[T], ...]:
    """Return a deterministic greedy MMR ordering with per-item score evidence."""
    if not isinstance(policy, ContextSelectionPolicy):
        raise ContextValidationError("policy must be a ContextSelectionPolicy")
    if not items:
        return ()
    item_by_id: dict[str, T] = {}
    texts: dict[str, str] = {}
    feature_vectors: dict[str, Mapping[str, float]] = {}
    costs: dict[str, int] = {}
    for item in items:
        key = identity(item)
        if not isinstance(key, str) or not key:
            raise ContextValidationError("MMR item identities must be non-empty strings")
        if key in item_by_id:
            raise ContextValidationError("MMR item identities must be unique")
        text = similarity_text(item)
        if not isinstance(text, str):
            raise ContextValidationError("MMR similarity text must be a string")
        item_by_id[key] = item
        texts[key] = text
        feature_vectors[key] = policy.normalize_features(features(item))
        costs[key] = token_cost(item) if token_cost is not None else len(text)
    if any(type(cost) is not int or cost < 0 for cost in costs.values()):
        raise ContextValidationError("token costs must be nonnegative integers")
    maximum_cost = max(max(costs.values()), 1)
    remaining = dict(item_by_id)
    chosen_ids: list[str] = []
    ranked: list[RankedItem[T]] = []
    while remaining:
        candidates = []
        for key, item in remaining.items():
            text = texts[key]
            redundancy = max((text_similarity(text, texts[other_id]) for other_id in chosen_ids), default=0.0)
            breakdown = policy.breakdown(feature_vectors[key], redundancy=redundancy,
                token_cost=float(costs[key]), cost_fraction=costs[key] / maximum_cost)
            candidates.append((breakdown["mmr_score"], key, item, breakdown))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _score, key, item, breakdown = candidates[0]
        ranked.append(RankedItem(item, breakdown))
        chosen_ids.append(key)
        del remaining[key]
    return tuple(ranked)


def duplicate_reason(winner_id: str) -> str:
    """Reason for an exact duplicate excluded in favour of ``winner_id``."""
    return f"duplicate of {winner_id}"


@dataclass(frozen=True)
class LimitedSelection(Generic[T]):
    kept: tuple[T, ...]
    dropped: tuple[T, ...]


def apply_item_limit(ordered: Sequence[T], limit: int | None) -> LimitedSelection[T]:
    """Keep the first ``limit`` items of an already priority-ordered sequence.

    ``limit=None`` means unlimited.  Order is preserved on both sides, so the
    result is deterministic whenever ``ordered`` is.
    """
    if limit is None:
        return LimitedSelection(kept=tuple(ordered), dropped=())
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ContextValidationError("limit must be a non-negative integer")
    return LimitedSelection(kept=tuple(ordered[:limit]), dropped=tuple(ordered[limit:]))


_ORDINAL = re.compile(r"(\d+)$")


def trailing_ordinal(source_id: str) -> int:
    """The trailing integer of an id like ``proof/fr-12`` (``-1`` if none).

    Used to rank runs by recency: the proof runtime allocates ids in
    increasing order, so a higher ordinal is a more recent artifact.
    """
    match = _ORDINAL.search(source_id)
    return int(match.group(1)) if match else -1
