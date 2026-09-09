"""Stable reason codes for rejected proposals.

Rejections are journalled and counted, so these strings are protocol rather
than log prose. Codes are added as the validators that emit them land; this
set covers the proposal-only validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["Reason", "Rejection"]


class Reason(StrEnum):
    """Why the gate refused a proposal."""

    UNKNOWN_STATUS_VALUE = "unknown-status-value"
    NAMESPACE_MISMATCH = "namespace-mismatch"
    MISSING_REQUIRED_FIELD = "missing-required-field"
    MISSING_PRIOR_VALUE = "missing-prior-value"

    SUBGOAL_COUNT_MISMATCH = "subgoal-count-mismatch"
    SUBGOAL_DUPLICATED = "subgoal-duplicated"
    SUBGOAL_INDEX_INVALID = "subgoal-index-invalid"
    ORPHAN_SUBGOAL_EDGE = "orphan-subgoal-edge"
    FORMAL_REQUIRES_REMOVAL = "formal-requires-removal"

    EXECUTOR_FAILURE_AS_SUCCESS = "executor-failure-as-success"
    CLOSURE_WITHOUT_LEAN_ACCEPTED = "closure-without-lean-accepted"
    CLOSURE_WITHOUT_ZERO_GOALS = "closure-without-zero-goals"
    DEAD_EDGE_MISSING_DIAGNOSTIC = "dead-edge-missing-diagnostic"
    FAILURE_WITH_MATHEMATICAL_DIAGNOSTIC = "failure-with-mathematical-diagnostic"
    ORPHAN_CLOSURE_EDGE = "orphan-closure-edge"

    HEURISTIC_CLOSURE_ATTEMPT = "heuristic-closure-attempt"

    UNKNOWN_NODE = "unknown-node"
    UNKNOWN_EDGE = "unknown-edge"
    NODE_ALREADY_EXISTS_WITH_LABEL = "node-already-exists-with-label"
    UPSERT_FIELD_CONFLICT = "upsert-field-conflict"          
    EDGE_ENDPOINT_TYPE_INVALID = "edge-endpoint-type-invalid"

    PRIOR_VALUE_MISMATCH = "prior-value-mismatch"
    ILLEGAL_STATUS_TRANSITION = "illegal-status-transition"
    IMMUTABLE_FIELD_OVERWRITE = "immutable-field-overwrite"

    STAGNATION_WITHOUT_OBSTRUCTION = "stagnation-without-obstruction"
    SELF_CERTIFICATION = "self-certification"
    PROMOTION_WITHOUT_REPLAY = "promotion-without-replay"
    PROMOTION_WITHOUT_ALIGNMENT = "promotion-without-alignment"
    ENVIRONMENT_DRIFT = "environment-drift"

    # Lost races. The proposal was well formed; the journal moved under it.
    STALE_BASE_REVISION = "stale-base-revision"
    LEASE_NOT_HELD = "lease-not-held"
    FENCING_TOKEN_SUPERSEDED = "fencing-token-superseded"
    JOURNAL_BUSY = "journal-busy"

    # Missing concurrency control. A proposal that names no base revision
    # cannot be checked against the journal at all, so it is never committed.
    MISSING_CONCURRENCY_TOKEN = "missing-concurrency-token"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One violation found in a proposal."""

    reason: Reason
    detail: str
    op_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "op_index": self.op_index,
        }
