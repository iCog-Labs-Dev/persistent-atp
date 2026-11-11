from __future__ import annotations

from typing import Set

from shared.vocab import (
    GRAPH_LABELS,
    AttemptStatus,
    ClaimStatus,
    MoveStatus,
    StateKind,
    StateStatus,
    canonical_status,
    values,
)

# Protocol status enums. Derived from the shared vocabulary so the graph and
# the commit gate cannot drift: add literals in `shared/vocab.py`.

STATE_STATUSES: Set[str] = values(StateStatus)
MOVE_STATUSES: Set[str] = values(MoveStatus)
CLAIM_STATUSES: Set[str] = values(ClaimStatus)
ATTEMPT_STATUSES: Set[str] = values(AttemptStatus)
STATE_KINDS: Set[str] = values(StateKind)

# Canonical literals the Cypher in adapter.py / rules.py writes.
STATE_OPEN = StateStatus.OPEN.value
STATE_CLOSED = StateStatus.FORMALLY_CLOSED.value
STATE_TAINTED = StateStatus.TAINTED.value
STATE_REOPENED = StateStatus.REOPENED.value
MOVE_OPEN = MoveStatus.OPEN.value
MOVE_QUEUED = MoveStatus.QUEUED.value
MOVE_CLOSED = MoveStatus.CLOSED.value
MOVE_LEASED = MoveStatus.LEASED.value
MOVE_REFUTED = MoveStatus.REFUTED.value
MOVE_DOMINATED = MoveStatus.DOMINATED.value
MOVE_EXHAUSTED = MoveStatus.EXHAUSTED.value
MOVE_REOPENED = MoveStatus.REOPENED.value
CLAIM_CONJECTURAL = ClaimStatus.CONJECTURAL.value
CLAIM_REFUTED = ClaimStatus.REFUTED.value
CLAIM_TAINTED = ClaimStatus.TAINTED.value
ATTEMPT_PENDING = AttemptStatus.PENDING.value
STATE_KIND_OR = StateKind.OR.value
STATE_KIND_AND = StateKind.AND.value


def _check(value: str, allowed: Set[str], label: str) -> str:
    """Validate `value` against a closed vocabulary and canonicalise it.

    Legacy underscore spellings (`critic_accepted`) and the graph's old
    `closed` state literal are accepted on input and returned in canonical
    form, so callers must use the return value rather than the raw argument.
    """
    return canonical_status(value, frozenset(allowed), label)


def _edge_id(event_id: str, kind: str) -> str:
    return f"{event_id}-{kind}" if kind else event_id
