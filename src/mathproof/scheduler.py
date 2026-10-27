"""The global scheduler: choose the next move, issue its lease (Section 8).

`GlobalScheduler.lease_next(proof_id, worker_class, ttl, actor=...)` is `proof-lease-next`
(Section 2): one call builds the eligible frontier from committed state,
scores it with a transparent weighted-sum policy, selects one move, and
issues a per-move dispatch lease through the journal store -- expiry, mutual
exclusion and fencing included.

Invariants this module must never break (2.4 / 7 / 9):

* every score is advisory. The scheduler writes leases and score snapshots,
  never statuses; promotion stays the commit gate's job.
* no claimable work is an expected idle state: `lease_next` returns None.
  Stale revisions and unavailable snapshots are reported as errors.
* unpopulated features take documented neutral defaults -- zeroing would
  assert "no information gain", which is a claim, not a default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.gate import CommitResult
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import JournalOverlayView, ReadView
from commit_gate.store import ConcurrencyError, JournalStore, LeaseCandidate, LeaseRow
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
    conflict_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Lease:
    """What a dispatched worker receives (Section 2.3)."""

    lease_id: str
    proof_id: str
    actor: str
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
            actor=row.actor,
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
            "actor": self.actor,
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

    # -- public entry point -------------------------------------------------

    def lease_next(
        self,
        proof_id: str,
        worker_class: str,
        ttl_seconds: float = 600.0,
        *,
        actor: str,
        category: str | None = None,
    ) -> Lease | None:
        """`proof-lease-next`: select and lease the best eligible move.

        Returns None when no eligible move is free. A changed journal revision
        raises ConcurrencyError; the caller may retry with a fresh snapshot.
        The trusted caller supplies the actual worker's actor and class.
        """
        if category is not None and CATEGORY_WORKER_CLASS.get(category) != worker_class:
            raise ValueError(f"{worker_class!r} cannot lease category {category!r}")
        revision, frontier = self._snapshot_frontier(proof_id)
        if category is not None:
            frontier = [candidate for candidate in frontier if candidate.category == category]
        scored = []
        for candidate in frontier:
            features = self._feature_vector(candidate, worker_class)
            priority, snapshot = self._policy.score(features)
            scored.append((priority, candidate, snapshot))
        scored.sort(key=lambda item: item[0], reverse=True)
        row = self._store.claim_next(
            proof_id,
            [LeaseCandidate(candidate.move_id, snapshot, candidate.conflict_ids)
             for _, candidate, snapshot in scored],
            actor=actor,
            worker_class=worker_class,
            base_revision=revision,
            ttl_seconds=ttl_seconds,
        )
        if row is None:
            return None
        snapshot = next(snapshot for _, candidate, snapshot in scored
                        if candidate.move_id == row.selected_move_id)
        return Lease.from_row(row, snapshot, self._policy.name)

    def release(self, proof_id: str, lease_id: str, *, fencing_token: int) -> bool:
        """Mark a dispatched lease finished (its token dies either way)."""
        return self._store.release_lease(proof_id, lease_id, fencing_token=fencing_token)

    # -- frontier construction (Section 3) ----------------------------------

    def build_frontier(self, proof_id: str) -> list[Candidate]:
        """Every committable object eligible for dispatch right now."""
        return self._snapshot_frontier(proof_id)[1]

    def _snapshot_frontier(self, proof_id: str) -> tuple[int, list[Candidate]]:
        try:
            view_revision, snapshot = self._view.snapshot(proof_id)
        except Exception as exc:
            raise ConcurrencyError(
                Reason.READ_VIEW_OUT_OF_SYNC, f"could not snapshot scheduling state: {exc}",
            ) from exc
        revision = self._store.head(proof_id)[0]
        if type(view_revision) is not int or not 0 <= view_revision <= revision:
            raise ConcurrencyError(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"view revision {view_revision!r} is invalid for journal head {revision}",
            )
        events = self._store.read_events_between(proof_id, view_revision, revision)
        ops = tuple(op for event in events for op in Proposal.from_dict(event).ops)
        node_ids = dict.fromkeys(self._node_ids(snapshot))
        node_ids.update((op.node_id, None) for op in ops if isinstance(op, UpsertNode))
        view = JournalOverlayView(snapshot, revision, ops)
        return revision, self._build_frontier(proof_id, view, node_ids)

    def _build_frontier(self, proof_id: str, view: ReadView, node_ids: Mapping[str, None]) -> list[Candidate]:
        prefix = f"{proof_id}/"
        nodes = {
            node_id: record
            for node_id in node_ids
            if node_id.startswith(prefix)
            and (record := view.node(node_id)) is not None
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
            for edge in view.edges_from(state_id, "PROPOSES"):
                move = nodes.get(edge.dst_id)
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
                # An existing run, not its declaration, is the continuation target.
                if record.fields.get("status") != DeclarationStatus.ALIGNED.value or any(
                    nodes.get(edge.src_id) is not None and nodes[edge.src_id].label == "FormalRun"
                    for edge in view.edges_to(node_id, "SEARCHES")
                ):
                    continue
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
                targets = {edge.dst_id for edge in view.edges_from(node_id, "SEARCHES")}
                if len(targets) != 1:
                    continue
                target_id = next(iter(targets))
                target = nodes.get(target_id)
                if target is None or target.label != "FormalDeclaration" or target.fields.get("status") not in (
                    DeclarationStatus.ALIGNED.value, DeclarationStatus.SEARCHING.value,
                ):
                    continue
                if self._run_has_open_checkpoint(view, node_id):
                    candidates.append(
                        Candidate(
                            move_id=node_id,
                            category="formal-run",
                            detail="searching run with an outstanding checkpoint",
                            conflict_ids=(target_id, *(edge.src_id for edge in view.edges_to(target_id, "SEARCHES"))),
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
                if self._has_favorable_verdict(view, node_id):
                    continue
                dependencies = len(view.edges_from(node_id, "DEPENDS_ON"))
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

    def _node_ids(self, view: ReadView) -> list[str]:
        nodes = getattr(view, "nodes", None)
        if nodes is None:
            raise TypeError(
                "frontier construction needs a view exposing known node ids "
                "(MemoryView provides `.nodes`; Neo4j adapters should query directly)"
            )
        return list(nodes.keys())

    def _has_favorable_verdict(self, view: ReadView, claim_id: str) -> bool:
        for edge in view.edges_to(claim_id, "REVIEWS_CLAIM"):
            attempt = view.node(edge.src_id)
            if attempt is None or attempt.label != "Attempt":
                continue
            if (
                attempt.fields.get("worker_class") == WorkerClass.CRITIC.value
                and attempt.fields.get("status")
                in (AttemptStatus.SUPPORTED.value, AttemptStatus.CRITIC_ACCEPTED.value)
            ):
                return True
        return False

    def _run_has_open_checkpoint(self, view: ReadView, run_id: str) -> bool:
        for edge in view.edges_from(run_id, "HAS_CHECKPOINT"):
            checkpoint = view.node(edge.dst_id)
            if checkpoint is None:
                continue
            open_states = [
                view.node(frontier.dst_id)
                for frontier in view.edges_from(
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
