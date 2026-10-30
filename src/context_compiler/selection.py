"""

Every compiler (research, formal, obstruction) follows the same small,
deterministic, explainable rules.  They may use different categories and
rendering formats; they do not use different *policy*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar

from .errors import ContextValidationError

T = TypeVar("T")

REASON_OVER_TEXT_BUDGET = "dropped: over text budget"
REASON_OVER_PREMISE_LIMIT = "dropped: over premise limit"
REASON_OVER_PRIOR_RUN_LIMIT = "dropped: over prior-run limit"
REASON_OVER_TRANSPOSITION_LIMIT = "dropped: over transposition limit"
REASON_OVER_FAILED_STATE_LIMIT = "dropped: over failed-state limit"


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
