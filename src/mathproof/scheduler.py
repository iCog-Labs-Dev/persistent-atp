"""The global scheduler: choose the next move, issue its lease (Section 8).

`GlobalScheduler.lease_next(proof_id, worker_class, ttl)` is `proof-lease-next`
(Section 2): one call builds the eligible frontier from committed state,
scores it with a transparent weighted-sum policy, selects one move, and
issues a per-move dispatch lease through the journal store -- expiry, mutual
exclusion and fencing included.

Invariants this module must never break (2.4 / 7 / 9):

* every score is advisory. The scheduler writes leases and score snapshots,
  never statuses; promotion stays the commit gate's job.
* an empty frontier is an expected terminal state: `lease_next` returns
  None, it does not raise.
* unpopulated features take documented neutral defaults -- zeroing would
  assert "no information gain", which is a claim, not a default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.gate import CommitResult
from commit_gate.state import ReadView
from commit_gate.store import JournalStore, LeaseRow
from commit_gate.vocab import (
    AttemptStatus,
    ClaimStatus,
    DeclarationStatus,
    FormalStateStatus,
    ResearchMoveStatus,
    ResearchStateStatus,
    RunDisposition,
    WorkerClass,
)
from .ids import IdType, full_id

__all__ = [
    "Candidate",
    "Lease",
    "SchedulerPolicy",
    "SchedulerStatistics",
    "GlobalScheduler",
]

POLICY_NAME = "global-best-first-v1"

FEATURES = (
    "dependency_centrality",
    "expected_theorem_impact",
    "expected_information_gain",
    "novelty_and_mechanism_diversity",
    "verification_value",
    "formalization_readiness",
    "estimated_cost",
    "repeated_failure_risk",
    "human_priority",
    "availability_of_suitable_worker_or_model_or_tool",
)

NEUTRAL_DEFAULTS: dict[str, float] = {
    # Section 4.4: unpopulated features take midpoints or category priors,
    # never silent zeros -- a zero is an assertion, not a default.
    "dependency_centrality": 0.5,
    "expected_theorem_impact": 0.5,
    "expected_information_gain": 0.5,
    "novelty_and_mechanism_diversity": 0.5,
    "verification_value": 0.5,
    "formalization_readiness": 0.5,
    "estimated_cost": 1.0,
    "repeated_failure_risk": 0.5,
    "human_priority": 0.5,
    "availability_of_suitable_worker_or_model_or_tool": 1.0,
}

CATEGORY_WORKER_CLASS = {
    "research-move": WorkerClass.LLM_RESEARCH.value,
    "formal-run": WorkerClass.FORMAL_ATP.value,
    "critic-task": WorkerClass.CRITIC.value,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One frontier entry: what could be worked on next, and why."""

    move_id: str
    category: str  # research-move | formal-run | critic-task
    detail: str
    features: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Lease:
    """What a dispatched worker receives (Section 2.3)."""

    lease_id: str
    proof_id: str
    worker_class: str
    fencing_token: int
    base_revision: int
    selected_move_id: str
    ttl_seconds: float
    issued_at: float
    expires_at: float | None
    score_snapshot: dict[str, float]
    policy_name: str = POLICY_NAME

    @classmethod
    def from_row(cls, row: LeaseRow, score_snapshot: dict[str, float], policy_name: str) -> "Lease":
        return cls(
            lease_id=row.lease_id,
            proof_id=row.proof_id,
            worker_class=row.worker_class,
            fencing_token=row.fencing_token,
            base_revision=row.base_revision,
            selected_move_id=row.selected_move_id or "",
            ttl_seconds=row.ttl_seconds,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            score_snapshot=score_snapshot,
            policy_name=policy_name,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lease_id": self.lease_id,
            "proof_id": self.proof_id,
            "worker_class": self.worker_class,
            "fencing_token": self.fencing_token,
            "base_revision": self.base_revision,
            "selected_move_id": self.selected_move_id,
            "ttl_seconds": self.ttl_seconds,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "policy_name": self.policy_name,
            "score_snapshot": dict(self.score_snapshot),
        }
        return payload


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    """Transparent best-first weights over the Section 8.2 feature vector.

    Configuration values, not code: pass different weights to change
    scheduling behaviour without touching the scheduler.
    """

    name: str = POLICY_NAME
    weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "expected_theorem_impact": 1.2,
            "expected_information_gain": 1.0,
            "verification_value": 1.0,
            "dependency_centrality": 0.8,
            "novelty_and_mechanism_diversity": 0.6,
            "formalization_readiness": 0.6,
            "human_priority": 0.8,
            "availability_of_suitable_worker_or_model_or_tool": 0.7,
            "estimated_cost": -0.4,
            "repeated_failure_risk": -0.6,
        }
    )

    def score(self, features: Mapping[str, float]) -> tuple[float, dict[str, float]]:
        """The derived priority and the snapshot that produced it."""
        snapshot = {name: float(features.get(name, NEUTRAL_DEFAULTS[name])) for name in FEATURES}
        priority = sum(self.weights[name] * snapshot[name] for name in FEATURES)
        return priority, snapshot


class SchedulerStatistics:
    """Per-category outcome counters feeding `repeated_failure_risk`."""

    def __init__(self):
        self._outcomes: dict[tuple[str, str], int] = {}

    def record(self, category: str, outcome: str) -> None:
        self._outcomes[(category, outcome)] = self._outcomes.get((category, outcome), 0) + 1

    def failure_risk(self, category: str) -> float:
        """Share of recorded attempts in this category that failed.

        Neutral midpoint while there is no history: zero would claim a clean
        record the system has not earned (Section 4.4).
        """
        attempted = self.attempts(category)
        if attempted == 0:
            return NEUTRAL_DEFAULTS["repeated_failure_risk"]
        failed = self._outcomes.get((category, "failed"), 0)
        return failed / attempted

    def attempts(self, category: str) -> int:
        return sum(
            count for (cat, _), count in self._outcomes.items() if cat == category
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            f"{category}:{outcome}": count
            for (category, outcome), count in sorted(self._outcomes.items())
        }


class GlobalScheduler:
    """Chooses among research moves, formal targets and critic tasks."""

    def __init__(
        self,
        view: ReadView,
        store: JournalStore,
        *,
        policy: SchedulerPolicy | None = None,
        clock: Callable[[], float] = time.time,
        statistics: SchedulerStatistics | None = None,
    ):
        self._view = view
        self._store = store
        self._policy = policy or SchedulerPolicy()
        self._clock = clock
        self.statistics = statistics or SchedulerStatistics()
        self._serials: dict[str, int] = {}

    # -- public entry point -------------------------------------------------

    def lease_next(
        self,
        proof_id: str,
        worker_class: str,
        ttl_seconds: float = 600.0,
    ) -> Lease | None:
        """`proof-lease-next`: select and lease the best eligible move.

        Returns None when nothing is eligible -- the outer coordinator's cue
        to audit or expand the frontier, never an error.
        """
        active_moves = {
            row.selected_move_id
            for row in self._store.active_leases(proof_id)
            if row.selected_move_id is not None
        }
        frontier = [
            candidate
            for candidate in self.build_frontier(proof_id)
            if candidate.move_id not in active_moves
        ]
        if not frontier:
            return None

        scored = []
        for candidate in frontier:
            features = self._feature_vector(candidate, worker_class)
            priority, snapshot = self._policy.score(features)
            scored.append((priority, candidate, snapshot))
        scored.sort(key=lambda item: item[0], reverse=True)
        _, chosen, snapshot = scored[0]

        lease_id = self._next_lease_id(proof_id)
        row = self._store.issue_lease(
            proof_id,
            lease_id,
            worker_class=worker_class,
            selected_move_id=chosen.move_id,
            ttl_seconds=ttl_seconds,
            score_snapshot=snapshot,
        )
        return Lease.from_row(row, snapshot, self._policy.name)

    def release(self, proof_id: str, lease_id: str) -> bool:
        """Mark a dispatched lease finished (its token dies either way)."""
        return self._store.release_lease(proof_id, lease_id)

    # -- frontier construction (Section 3) ----------------------------------

    def build_frontier(self, proof_id: str) -> list[Candidate]:
        """Every committable object eligible for dispatch right now."""
        prefix = f"{proof_id}/"
        nodes = {
            node_id: record
            for node_id in self._node_ids()
            if node_id.startswith(prefix)
            and (record := self._view.node(node_id)) is not None
        }
        candidates: list[Candidate] = []

        # Research moves under a live parent state (3.1, 3.4).
        for state_id, state in nodes.items():
            if state.label != "ResearchState":
                continue
            if state.fields.get("status") in (
                ResearchStateStatus.SUPERSEDED.value,
                ResearchStateStatus.REFUTED.value,
                ResearchStateStatus.STALE.value,
            ):
                continue
            for edge in self._view.edges_from(state_id, "PROPOSES"):
                move = nodes.get(edge.dst_id) or self._view.node(edge.dst_id)
                if move is None or move.label != "ResearchMove":
                    continue
                if move.fields.get("status") in (
                    ResearchMoveStatus.QUEUED.value,
                    ResearchMoveStatus.OPEN.value,
                ):
                    candidates.append(
                        Candidate(
                            move_id=edge.dst_id,
                            category="research-move",
                            detail=f"proposed by {state_id}",
                            features={
                                "expected_information_gain": 0.6,
                                "formalization_readiness": 0.3,
                                "estimated_cost": 1.0,
                            },
                        )
                    )

        for node_id, record in nodes.items():
            # Formal targets: aligned declarations and resumable runs (3.2).
            if record.label == "FormalDeclaration" and record.fields.get(
                "status"
            ) in (DeclarationStatus.ALIGNED.value, DeclarationStatus.SEARCHING.value):
                candidates.append(
                    Candidate(
                        move_id=node_id,
                        category="formal-run",
                        detail="declaration ready for formal search",
                        features={
                            "formalization_readiness": 1.0,
                            "expected_theorem_impact": 0.8,
                            "verification_value": 0.7,
                            "estimated_cost": 3.0,
                        },
                    )
                )
            elif record.label == "FormalRun":
                if record.fields.get("status") != RunDisposition.SEARCHING.value:
                    continue
                if self._run_has_open_checkpoint(node_id):
                    candidates.append(
                        Candidate(
                            move_id=node_id,
                            category="formal-run",
                            detail="searching run with an outstanding checkpoint",
                            features={
                                "formalization_readiness": 0.9,
                                "expected_theorem_impact": 0.8,
                                "verification_value": 0.7,
                                "estimated_cost": 2.0,
                            },
                        )
                    )

            # Critic tasks: provisional claims with no favorable verdict (3.3).
            elif record.label == "Claim":
                if record.fields.get("status") != ClaimStatus.PROVISIONAL.value:
                    continue
                if self._has_favorable_verdict(node_id):
                    continue
                dependencies = len(self._view.edges_from(node_id, "DEPENDS_ON"))
                candidates.append(
                    Candidate(
                        move_id=node_id,
                        category="critic-task",
                        detail="provisional claim awaiting a critic verdict",
                        features={
                            "verification_value": 0.9,
                            "dependency_centrality": min(1.0, 0.25 * (dependencies + 1)),
                            "estimated_cost": 1.5,
                        },
                    )
                )
        return candidates

    # -- scoring -------------------------------------------------------------

    def _feature_vector(
        self, candidate: Candidate, requested_worker_class: str
    ) -> dict[str, float]:
        """The complete Section 8.2 feature vector for one candidate.

        Neutral defaults form the base layer; category-specific features
        override them; statistics and worker availability come last.
        """
        features = dict(NEUTRAL_DEFAULTS)
        features.update(candidate.features)
        features["repeated_failure_risk"] = self.statistics.failure_risk(
            candidate.category
        )
        natural = CATEGORY_WORKER_CLASS.get(candidate.category, requested_worker_class)
        features["availability_of_suitable_worker_or_model_or_tool"] = (
            1.0 if requested_worker_class == natural else 0.3
        )
        return features

    # -- statistics feedback (Section 6.3) -----------------------------------

    def update_statistics(self, commit: CommitResult, category: str) -> None:
        """Close the loop: commit outcomes shape future frontier scores."""
        self.statistics.record(category, "succeeded" if commit.accepted else "failed")

    def policy_snapshot(self) -> dict[str, Any]:
        return {"policy_name": self._policy.name, "weights": dict(self._policy.weights)}

    # -- helpers ---------------------------------------------------------------

    def _node_ids(self) -> list[str]:
        nodes = getattr(self._view, "nodes", None)
        if nodes is None:
            raise TypeError(
                "frontier construction needs a view exposing known node ids "
                "(MemoryView provides `.nodes`; Neo4j adapters should query directly)"
            )
        return list(nodes.keys())

    def _has_favorable_verdict(self, claim_id: str) -> bool:
        for edge in self._view.edges_to(claim_id, "REVIEWS_CLAIM"):
            attempt = self._view.node(edge.src_id)
            if attempt is None or attempt.label != "Attempt":
                continue
            if (
                attempt.fields.get("worker_class") == WorkerClass.CRITIC.value
                and attempt.fields.get("status")
                in (AttemptStatus.SUPPORTED.value, AttemptStatus.CRITIC_ACCEPTED.value)
            ):
                return True
        return False

    def _run_has_open_checkpoint(self, run_id: str) -> bool:
        for edge in self._view.edges_from(run_id, "HAS_CHECKPOINT"):
            checkpoint = self._view.node(edge.dst_id)
            if checkpoint is None:
                continue
            open_states = [
                self._view.node(frontier.dst_id)
                for frontier in self._view.edges_from(
                    edge.dst_id, "CHECKPOINT_FRONTIER"
                )
            ]
            if any(
                state is not None
                and state.fields.get("status")
                in (FormalStateStatus.OPEN.value, FormalStateStatus.EXPANDED.value)
                for state in open_states
            ):
                return True
        return False

    def _next_lease_id(self, proof_id: str) -> str:
        serial = self._serials.get(proof_id, 0) + 1
        self._serials[proof_id] = serial
        return full_id(proof_id, IdType.LEASE, serial)
