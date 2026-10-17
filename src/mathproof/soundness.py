"""Structural soundness validation of formal-search results (A3).

Implements the schema-level half of the Section 6.12 hardening checks against
adapter output -- typically the fake adapter's:

* **Child-count completeness** (Invariant 2): a tactic's declared subgoal
  count, its produced-goal list, and the states actually reported must all
  agree. One missing child rejects the whole result.
* **Score/status separation** (Invariant 3): a state closes only when a
  Lean-accepted tactic has every child recursively closed by Lean.
* **Environment-hash presence**: the run and every obstruction identify the
  pinned environment, so Phase 1's replay gating has something to bind to.

Full independent-replay verification is Phase 1 scope. Like the commit gate,
findings are typed values, not exceptions, and are grouped by reason so the
Section 16.2 rejection-rate metrics can count them directly.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from commit_gate.vocab import (
    NON_KERNEL_TACTICS,
    TERMINAL_EXECUTOR_FAILURES,
    ExecutorResult,
)

__all__ = [
    "SoundnessReason",
    "SoundnessViolation",
    "validate_formal_search_result",
    "check_certificate_replay_binding",
    "check_environment_staleness",
    "violation_counts",
]

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

TRUTH_LIKE_ANNOTATIONS = frozenset(
    {"truth", "is_true", "proved", "valid", "certainty_of_truth"}
)


class SoundnessReason(StrEnum):
    """Why a formal-search result fails structural validation."""

    SUBGOAL_COUNT_MISMATCH = "subgoal-count-mismatch"
    OMITTED_SUBGOAL = "omitted-subgoal"
    DUPLICATED_SUBGOAL = "duplicated-subgoal"
    UNKNOWN_CHILD_STATE = "unknown-child-state"

    EXECUTOR_FAILURE_AS_SUCCESS = "executor-failure-as-success"
    CLOSURE_WITHOUT_ZERO_GOALS = "closure-without-zero-goals"
    HEURISTIC_CLOSURE_ATTEMPT = "heuristic-closure-attempt"
    NON_KERNEL_CLOSURE = "non-kernel-closure"
    SCORE_AS_TRUTH = "score-as-truth"

    MISSING_ENVIRONMENT_HASH = "missing-environment-hash"
    MALFORMED_ENVIRONMENT_HASH = "malformed-environment-hash"
    OBSTRUCTION_ENVIRONMENT_MISMATCH = "obstruction-environment-mismatch"

    CERTIFICATE_REQUIRED = "certificate-required"
    CERTIFICATE_DISPOSITION_MISMATCH = "certificate-disposition-mismatch"
    CERTIFICATE_WITHOUT_KERNEL_PROOF = "certificate-without-kernel-proof"
    PROMOTION_WITHOUT_REPLAY = "promotion-without-replay"
    REPLAY_ENVIRONMENT_MISMATCH = "replay-environment-mismatch"
    ENVIRONMENT_STALE = "environment-stale"


@dataclass(frozen=True, slots=True)
class SoundnessViolation:
    """One structural defect found in a formal-search result."""

    reason: SoundnessReason
    detail: str
    pointer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "pointer": self.pointer,
        }


def violation_counts(violations: Iterable[SoundnessViolation]) -> dict[str, int]:
    """Rejection counts keyed by reason code -- the seed of the Section 16.2 metrics."""
    return dict(Counter(str(v.reason) for v in violations))


def _check_environment(result: Mapping[str, Any]) -> Iterable[SoundnessViolation]:
    env_hash = result.get("environment_hash")
    if env_hash is None:
        yield SoundnessViolation(
            SoundnessReason.MISSING_ENVIRONMENT_HASH,
            "result carries no environment_hash; certificates could never be "
            "bound to a toolchain",
            "environment_hash",
        )
    elif not isinstance(env_hash, str) or not SHA256_RE.match(env_hash):
        yield SoundnessViolation(
            SoundnessReason.MALFORMED_ENVIRONMENT_HASH,
            f"environment_hash {env_hash!r} is not a sha256 content address",
            "environment_hash",
        )
        env_hash = None

    for index, obstruction in enumerate(result.get("obstructions") or []):
        observed = obstruction.get("environment_hash")
        if observed is None:
            yield SoundnessViolation(
                SoundnessReason.MISSING_ENVIRONMENT_HASH,
                "obstruction carries no environment_hash",
                f"obstructions[{index}]",
            )
        elif observed != env_hash:
            yield SoundnessViolation(
                SoundnessReason.OBSTRUCTION_ENVIRONMENT_MISMATCH,
                f"obstruction {obstruction.get('obstruction_id')!r} names "
                f"environment {observed!r}, run searched under {env_hash!r}",
                f"obstructions[{index}].environment_hash",
            )


def _check_tactic_edges(result: Mapping[str, Any]) -> Iterable[tuple[SoundnessViolation | None, Mapping[str, Any]]]:
    """Yield (violation-or-None, edge); the violation slot carries completeness defects."""
    states = {s.get("state_id") for s in result.get("states") or []}
    for index, edge in enumerate(result.get("tactic_edges") or []):
        pointer = f"tactic_edges[{index}]"
        declared = edge.get("subgoal_count")
        children = edge.get("produced_goal_ids")
        tactic_id = edge.get("tactic_id", "<missing>")

        if type(declared) is not int or declared < 0:
            yield (
                SoundnessViolation(
                    SoundnessReason.SUBGOAL_COUNT_MISMATCH,
                    f"tactic {tactic_id!r} declares no integer subgoal_count",
                    pointer,
                ),
                edge,
            )
            continue

        if not isinstance(children, list) or len(children) != declared:
            yield (
                SoundnessViolation(
                    SoundnessReason.OMITTED_SUBGOAL,
                    f"tactic {tactic_id!r} declares {declared} Lean-produced "
                    f"goal(s) but lists {len(children) if isinstance(children, list) else 'no list'}",
                    pointer,
                ),
                edge,
            )
            continue

        duplicated = {c for c in children if isinstance(c, str) and children.count(c) > 1}
        if duplicated:
            yield (
                SoundnessViolation(
                    SoundnessReason.DUPLICATED_SUBGOAL,
                    f"tactic {tactic_id!r} lists child goal(s) {sorted(duplicated)} twice",
                    pointer,
                ),
                edge,
            )
            continue

        unknown = [c for c in children if not isinstance(c, str) or c not in states]
        if unknown:
            # Invariant 2: a listed obligation with no state behind it is as
            # lost as one that was never listed.
            yield (
                SoundnessViolation(
                    SoundnessReason.UNKNOWN_CHILD_STATE,
                    f"tactic {tactic_id!r} requires child state(s) {unknown} "
                    "that the result does not report",
                    pointer,
                ),
                edge,
            )
            continue

        if str(edge.get("tactic_label", "")) in NON_KERNEL_TACTICS and (
            edge.get("executor_result") == str(ExecutorResult.LEAN_ACCEPTED)
        ):
            # C6: a branch scored shut by a heuristic is not kernel evidence,
            # whatever executor_result claims. An honestly-labelled fallback
            # edge (empty-output) passes here; closure rules catch its targets.
            yield (
                SoundnessViolation(
                    SoundnessReason.NON_KERNEL_CLOSURE,
                    f"tactic {tactic_id!r} carries non-kernel label "
                    f"{edge.get('tactic_label')!r}; its result cannot be "
                    "read as Lean acceptance",
                    pointer,
                ),
                edge,
            )
            continue

        yield (None, edge)


def _kernel_closed_states(result: Mapping[str, Any]) -> set[str]:
    """Find states with a complete, acyclic Lean-only AND/OR proof."""
    state_ids = {
        state.get("state_id")
        for state in result.get("states") or []
        if isinstance(state.get("state_id"), str)
    }
    outgoing: dict[str, list[Mapping[str, Any]]] = {}
    for edge in result.get("tactic_edges") or []:
        children = edge.get("produced_goal_ids")
        if (
            edge.get("executor_result") != ExecutorResult.LEAN_ACCEPTED.value
            or edge.get("tactic_label") in NON_KERNEL_TACTICS
            or type(edge.get("subgoal_count")) is not int
            or not isinstance(children, list)
            or len(children) != edge["subgoal_count"]
            or any(not isinstance(child, str) for child in children)
            or len(set(children)) != len(children)
            or any(child not in state_ids for child in children)
        ):
            continue
        outgoing.setdefault(edge.get("source_state_id"), []).append(edge)

    closed: set[str] = set()

    def closes(node_id: str, visiting: frozenset[str]) -> bool:
        if node_id in visiting or node_id not in state_ids:
            return False
        if node_id in closed:
            return True
        path = visiting | {node_id}
        for edge in outgoing.get(node_id, ()):
            if all(closes(child, path) for child in edge["produced_goal_ids"]):
                closed.add(node_id)
                return True
        return False

    for state_id in state_ids:
        if isinstance(state_id, str):
            closes(state_id, frozenset())
    return closed


def _check_closure_and_scores(result: Mapping[str, Any]) -> Iterable[SoundnessViolation]:
    """A state closes only through a complete Lean-only proof path."""
    closed = _kernel_closed_states(result)

    for index, edge in enumerate(result.get("tactic_edges") or []):
        pointer = f"tactic_edges[{index}]"
        outcome = edge.get("executor_result")
        if outcome in TERMINAL_EXECUTOR_FAILURES and edge.get("subgoal_count") == 0:
            source_status = next(
                (
                    s.get("status")
                    for s in result.get("states") or []
                    if s.get("state_id") == edge.get("source_state_id")
                ),
                None,
            )
            if source_status == "formally-closed" and edge.get("source_state_id") not in closed:
                yield SoundnessViolation(
                    SoundnessReason.EXECUTOR_FAILURE_AS_SUCCESS,
                    f"{outcome} on {edge.get('source_state_id')!r} is infrastructure "
                    "failure, not closure",
                    pointer,
                )

    for index, state in enumerate(result.get("states") or []):
        pointer = f"states[{index}]"
        annotations = state.get("annotations") or {}
        truthy = sorted(set(annotations) & TRUTH_LIKE_ANNOTATIONS)
        if truthy:
            yield SoundnessViolation(
                SoundnessReason.SCORE_AS_TRUTH,
                f"annotations {truthy} assert truth; scores are scheduling signals",
                f"{pointer}.annotations",
            )

        if state.get("status") in ("formally-closed", "lean-verified"):
            if state["state_id"] not in closed:
                scored = bool(annotations)
                hint = (
                    " while carrying heuristic scores" if scored else ""
                )
                yield SoundnessViolation(
                    SoundnessReason.HEURISTIC_CLOSURE_ATTEMPT,
                    f"state {state['state_id']!r} is marked {state['status']} "
                    f"with no complete Lean-only proof path{hint}",
                    f"{pointer}.status",
                )


def validate_formal_search_result(
    result: Mapping[str, Any],
    *,
    pinned_environment_hash: str | None = None,
) -> tuple[SoundnessViolation, ...]:
    """Every structural violation in a formal-search-result payload."""
    findings: list[SoundnessViolation] = []
    findings.extend(_check_environment(result))

    for violation, _edge in _check_tactic_edges(result):
        if violation is not None:
            findings.append(violation)

    findings.extend(_check_closure_and_scores(result))
    findings.extend(check_certificate_replay_binding(result))
    if pinned_environment_hash is not None:
        stale = check_environment_staleness(result, pinned_environment_hash)
        if stale is not None:
            findings.append(stale)
    return tuple(findings)


def check_certificate_replay_binding(
    result: Mapping[str, Any],
) -> tuple[SoundnessViolation, ...]:
    """The replay contract (6.11, Invariant 4), checked structurally.

    A certificate only becomes accepted/rejected through an attached
    ``replay_result`` produced by the independent replayer -- never by the
    searching adapter's own word. A ``proved-pending-replay`` run must in
    fact carry a candidate certificate.
    """
    findings: list[SoundnessViolation] = []
    certificate = result.get("certificate")

    if result.get("disposition") == "proved-pending-replay" and not certificate:
        findings.append(
            SoundnessViolation(
                SoundnessReason.CERTIFICATE_REQUIRED,
                "disposition proved-pending-replay carries no certificate",
                "certificate",
            )
        )
    if not certificate:
        return tuple(findings)

    if result.get("disposition") != "proved-pending-replay":
        findings.append(
            SoundnessViolation(
                SoundnessReason.CERTIFICATE_DISPOSITION_MISMATCH,
                f"disposition {result.get('disposition')!r} cannot carry a certificate",
                "certificate",
            )
        )
    elif result.get("root_state_id") not in _kernel_closed_states(result):
        findings.append(
            SoundnessViolation(
                SoundnessReason.CERTIFICATE_WITHOUT_KERNEL_PROOF,
                "candidate certificate has no complete Lean-only proof of its root",
                "certificate",
            )
        )

    status = certificate.get("status")
    if status in ("replay-accepted", "replay-rejected"):
        replay = certificate.get("replay_result")
        if not isinstance(replay, Mapping) or not replay:
            findings.append(
                SoundnessViolation(
                    SoundnessReason.PROMOTION_WITHOUT_REPLAY,
                    f"certificate status {status!r} without an attached "
                    "replay_result; only the independent replayer may set it",
                    "certificate.status",
                )
            )
        elif replay.get("status") != ("verified" if status == "replay-accepted" else "rejected"):
            findings.append(
                SoundnessViolation(
                    SoundnessReason.PROMOTION_WITHOUT_REPLAY,
                    f"certificate status {status!r} contradicts its replay_result "
                    f"{replay.get('status')!r}",
                    "certificate.replay_result",
                )
            )
        replay_env = replay.get("environment_hash") if isinstance(replay, Mapping) else None
        if isinstance(replay, Mapping) and replay and replay_env != result.get("environment_hash"):
            findings.append(
                SoundnessViolation(
                    SoundnessReason.REPLAY_ENVIRONMENT_MISMATCH,
                    f"replayed under {replay_env!r} but the run searched "
                    f"under {result.get('environment_hash')!r}",
                    "certificate.replay_result.environment_hash",
                )
            )

    return tuple(findings)


def check_environment_staleness(
    result: Mapping[str, Any],
    pinned_environment_hash: str,
) -> SoundnessViolation | None:
    """Compare a search result with the declaration's current environment pin."""
    observed = result.get("environment_hash")
    if observed == pinned_environment_hash:
        return None
    return SoundnessViolation(
        SoundnessReason.ENVIRONMENT_STALE,
        f"result for {result.get('declaration_id')!r} searched under {observed!r}, "
        f"current pin is {pinned_environment_hash!r}",
        "environment_hash",
    )
