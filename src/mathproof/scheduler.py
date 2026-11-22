"""The global scheduler: choose the next move, issue its lease (Section 8).

`GlobalScheduler.lease_best_global_move(proof_id)` is the coordinator's
routing-aware entry point. It ranks the eligible frontier across available
worker classes, then atomically leases the best free move to its required
class and configured actor. `lease_next` remains the class-supplied lower-level
entry point for existing callers.

Invariants this module must never break (2.4 / 7 / 9):

* every score is advisory. The scheduler writes leases and score snapshots,
  never statuses; promotion stays the commit gate's job.
* no claimable work is an expected idle state: `lease_next` returns None.
  Stale revisions and unavailable snapshots are reported as errors.
* unpopulated features take documented neutral defaults -- zeroing would
  assert "no information gain", which is a claim, not a default.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.gate import CommitResult
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import JournalOverlayView, ReadView
from commit_gate.store import (
    ConcurrencyError, JournalStore, LeaseCandidate, LeaseRow, RoutedLeaseCandidate,
)
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
    "SchedulerPortfolio",
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
    "pln_strength",
    "pln_confidence",
    "proof_number_priority",
    "disproof_number_priority",
    "state_novelty",
    "transposition_novelty",
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
    "pln_strength": 0.5,
    "pln_confidence": 0.5,
    "proof_number_priority": 0.5,
    "disproof_number_priority": 0.5,
    "state_novelty": 0.5,
    "transposition_novelty": 0.5,
}

CATEGORY_WORKER_CLASS = {
    "research-move": WorkerClass.LLM_RESEARCH.value,
    "formal-run": WorkerClass.FORMAL_ATP.value,
    "critic-task": WorkerClass.CRITIC.value,
    "obstruction-task": WorkerClass.COORDINATOR.value,
}
ROUTABLE_WORKER_CLASSES = frozenset(CATEGORY_WORKER_CLASS.values())

OBSTRUCTION_DISPOSITIONS = frozenset(
    {RunDisposition.STAGNATED.value, RunDisposition.COUNTEREXAMPLE.value}
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One frontier entry: what could be worked on next, and why."""

    move_id: str
    category: str  # research-move | formal-run | critic-task | obstruction-task
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
    def from_row(cls, row: LeaseRow, score_snapshot: dict[str, float],
                 policy_name: str | None = None) -> "Lease":
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
            policy_name=policy_name or row.policy_name or POLICY_NAME,
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
            "pln_strength": 0.35,
            "pln_confidence": 0.25,
            "proof_number_priority": 0.35,
            "disproof_number_priority": 0.15,
            "state_novelty": 0.25,
            "transposition_novelty": 0.15,
        }
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("scheduler policy name must be a non-empty string")
        if not isinstance(self.weights, Mapping) or set(self.weights) != set(FEATURES):
            raise ValueError("scheduler policy weights must define every scheduler feature")
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in self.weights.values()):
            raise ValueError("scheduler policy weights must be finite numbers")

    def score(self, features: Mapping[str, float]) -> tuple[float, dict[str, float]]:
        """The derived priority and the snapshot that produced it."""
        snapshot = {name: float(features.get(name, NEUTRAL_DEFAULTS[name])) for name in FEATURES}
        priority = sum(self.weights[name] * snapshot[name] for name in FEATURES)
        return priority, snapshot


@dataclass(frozen=True, slots=True)
class SchedulerPortfolio:
    """A deterministic rotation of named policies using the lease audit stream."""

    policies: tuple[SchedulerPolicy, ...]
    name: str = "lease-audit-round-robin-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.policies, tuple) or not self.policies:
            raise ValueError("scheduler portfolio must contain at least one policy")
        if not all(isinstance(policy, SchedulerPolicy) for policy in self.policies):
            raise ValueError("scheduler portfolio entries must be SchedulerPolicy values")
        if len({policy.name for policy in self.policies}) != len(self.policies):
            raise ValueError("scheduler portfolio policy names must be unique")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("scheduler portfolio name must be non-empty")

    def select(self, issued_lease_count: int) -> SchedulerPolicy:
        if type(issued_lease_count) is not int or issued_lease_count < 0:
            raise ValueError("issued_lease_count must be nonnegative")
        return self.policies[issued_lease_count % len(self.policies)]


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
    """Choose among research, formal, critic, and obstruction tasks."""

    def __init__(
        self,
        view: ReadView,
        store: JournalStore,
        *,
        policy: SchedulerPolicy | None = None,
        portfolio: SchedulerPortfolio | None = None,
        clock: Callable[[], float] = time.time,
        statistics: SchedulerStatistics | None = None,
        worker_actors: Mapping[str, str] | None = None,
        obstruction_is_handled: Callable[[ReadView, str], bool] | None = None,
    ):
        self._view = view
        self._store = store
        if policy is not None and portfolio is not None:
            raise ValueError("select either one scheduler policy or a policy portfolio")
        if portfolio is not None and not isinstance(portfolio, SchedulerPortfolio):
            raise TypeError("portfolio must be a SchedulerPortfolio")
        self._policy = policy or SchedulerPolicy()
        self._portfolio = portfolio
        self._clock = clock
        self.statistics = statistics or SchedulerStatistics()
        self._worker_actors = dict(worker_actors or {})
        for worker_class, actor in self._worker_actors.items():
            if worker_class not in ROUTABLE_WORKER_CLASSES:
                raise ValueError(f"no route is defined for worker class {worker_class!r}")
            if not isinstance(actor, str) or not actor.strip():
                raise ValueError(f"worker class {worker_class!r} needs a nonempty actor")
        if obstruction_is_handled is not None and not callable(
            obstruction_is_handled
        ):
            raise TypeError("obstruction_is_handled must be callable")
        self._obstruction_is_handled = obstruction_is_handled or (
            lambda _view, _obstruction_id: False
        )

    # -- public entry point -------------------------------------------------

    def lease_best_global_move(
        self, proof_id: str, ttl_seconds: float = 600.0,
        *, category: str | None = None,
    ) -> Lease | None:
        """Lease the highest-ranked free move to its required worker class.

        Only classes with configured actors are available. An unserved class
        cannot gain a lease through an availability score. `None` means no
        currently routable move is free, never proof completion. Journal
        revision races still raise `ConcurrencyError` from `claim_next`.
        """
        if category is not None and category not in CATEGORY_WORKER_CLASS:
            raise ValueError(f"unknown frontier category {category!r}")
        revision, frontier = self._snapshot_frontier(proof_id)
        policies = self._policies()
        candidate_sets: list[list[RoutedLeaseCandidate]] = []
        scored_by_policy: dict[str, list[tuple[float, Candidate, str, dict[str, float]]]] = {}
        for policy in policies:
            scored: list[tuple[float, Candidate, str, dict[str, float]]] = []
            for candidate in frontier:
                if category is not None and candidate.category != category:
                    continue
                try:
                    worker_class = CATEGORY_WORKER_CLASS[candidate.category]
                except KeyError as exc:
                    raise ValueError(f"no route for candidate category {candidate.category!r}") from exc
                if worker_class not in self._worker_actors:
                    continue
                features = self._feature_vector(candidate, worker_class)
                priority, snapshot = policy.score(features)
                if not math.isfinite(priority):
                    raise ValueError("routing priority must be finite")
                scored.append((priority, candidate, worker_class, snapshot))
            scored.sort(key=lambda item: (-item[0], item[1].move_id))
            scored_by_policy[policy.name] = scored
            policy_config = self._policy_audit_config(policy)
            candidate_sets.append([
                RoutedLeaseCandidate(
                    candidate.move_id, snapshot, self._worker_actors[worker_class],
                    worker_class, candidate.conflict_ids, policy.name, policy_config,
                )
                for _priority, candidate, worker_class, snapshot in scored
            ])

        if self._portfolio is None:
            row = self._store.claim_best_routed(
                proof_id, candidate_sets[0], base_revision=revision, ttl_seconds=ttl_seconds,
            )
        else:
            row = self._store.claim_best_routed(
                proof_id, (), base_revision=revision, ttl_seconds=ttl_seconds,
                candidate_sets=candidate_sets,
            )
        if row is None:
            return None
        selected_policy = next(policy for policy in policies if policy.name == row.policy_name)
        scored = scored_by_policy[selected_policy.name]
        snapshot = next(snapshot for _, candidate, worker_class, snapshot in scored
                        if candidate.move_id == row.selected_move_id
                        and worker_class == row.worker_class)
        return Lease.from_row(row, snapshot, selected_policy.name)

    def lease_selected(self, proof_id: str, move_id: str, worker_class: str, *, actor: str,
                       ttl_seconds: float = 600.0) -> Lease | None:
        """Lease one authorized selected move only if it remains schedulable."""
        revision, frontier = self._snapshot_frontier(proof_id)
        candidate = next((item for item in frontier if item.move_id == move_id
            and CATEGORY_WORKER_CLASS[item.category] == worker_class), None)
        if candidate is None:
            return None
        policies = self._policies()
        candidate_sets: list[list[RoutedLeaseCandidate]] = []
        snapshots: dict[str, dict[str, float]] = {}
        for policy in policies:
            _, snapshot = policy.score(self._feature_vector(candidate, worker_class))
            snapshots[policy.name] = snapshot
            candidate_sets.append([RoutedLeaseCandidate(
                candidate.move_id, snapshot, actor, worker_class, candidate.conflict_ids,
                policy.name, self._policy_audit_config(policy),
            )])
        if self._portfolio is None:
            row = self._store.claim_next(
                proof_id,
                [LeaseCandidate(move_id, snapshots[policies[0].name], candidate.conflict_ids,
                                policies[0].name, self._policy_audit_config(policies[0]))],
                actor=actor, worker_class=worker_class,
                base_revision=revision, ttl_seconds=ttl_seconds,
            )
        else:
            row = self._store.claim_best_routed(
                proof_id, (), base_revision=revision, ttl_seconds=ttl_seconds,
                candidate_sets=candidate_sets,
            )
        if row is None:
            return None
        return Lease.from_row(row, snapshots[row.policy_name], row.policy_name)

    def lease_next(
        self,
        proof_id: str,
        worker_class: str,
        ttl_seconds: float = 600.0,
        *,
        actor: str,
    ) -> Lease | None:
        """`proof-lease-next`: select and lease the best eligible move.

        Returns None when no eligible move is free. A changed journal revision
        raises ConcurrencyError; the caller may retry with a fresh snapshot.
        The trusted caller supplies the actual worker's actor and class.
        """
        revision, frontier = self._snapshot_frontier(proof_id)
        policies = self._policies()
        candidate_sets: list[list[RoutedLeaseCandidate]] = []
        scored_by_policy: dict[str, list[tuple[float, Candidate, dict[str, float]]]] = {}
        for policy in policies:
            scored = []
            for candidate in frontier:
                features = self._feature_vector(candidate, worker_class)
                priority, snapshot = policy.score(features)
                scored.append((priority, candidate, snapshot))
            scored.sort(key=lambda item: (-item[0], item[1].move_id))
            scored_by_policy[policy.name] = scored
            policy_config = self._policy_audit_config(policy)
            candidate_sets.append([
                RoutedLeaseCandidate(
                    candidate.move_id, snapshot, actor, worker_class,
                    candidate.conflict_ids, policy.name, policy_config,
                )
                for _, candidate, snapshot in scored
            ])
        if self._portfolio is None:
            row = self._store.claim_next(
                proof_id,
                [LeaseCandidate(
                    candidate.move_id, snapshot, candidate.conflict_ids,
                    policies[0].name, self._policy_audit_config(policies[0]),
                ) for _, candidate, snapshot in scored_by_policy[policies[0].name]],
                actor=actor, worker_class=worker_class,
                base_revision=revision, ttl_seconds=ttl_seconds,
            )
        else:
            row = self._store.claim_best_routed(
                proof_id, (), base_revision=revision, ttl_seconds=ttl_seconds,
                candidate_sets=candidate_sets,
            )
        if row is None:
            return None
        selected_policy_name = row.policy_name or policies[0].name
        scored = scored_by_policy[selected_policy_name]
        snapshot = next(snapshot for _, candidate, snapshot in scored
                        if candidate.move_id == row.selected_move_id)
        return Lease.from_row(row, snapshot, selected_policy_name)

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
        node_ids = dict.fromkeys(self._node_ids(snapshot, proof_id))
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
        centrality = self._dependency_centrality(view, nodes)
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
                    features = self._candidate_features(
                        view, nodes, edge.dst_id, "research-move",
                        {"expected_information_gain": 0.6,
                         "formalization_readiness": 0.3, "estimated_cost": 1.0},
                        centrality,
                    )
                    candidates.append(
                        Candidate(
                            move_id=edge.dst_id,
                            category="research-move",
                            detail=f"proposed by {state_id}",
                            features=features,
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
                features = self._candidate_features(
                    view, nodes, node_id, "formal-run",
                    {"formalization_readiness": 1.0,
                     "expected_theorem_impact": 0.8,
                     "verification_value": 0.7,
                     "estimated_cost": 3.0},
                    centrality,
                )
                candidates.append(
                    Candidate(
                        move_id=node_id,
                        category="formal-run",
                        detail="declaration ready for formal search",
                        features=features,
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
                    features = self._candidate_features(
                        view, nodes, node_id, "formal-run",
                        {"formalization_readiness": 0.9,
                         "expected_theorem_impact": 0.8,
                         "verification_value": 0.7,
                         "estimated_cost": 2.0},
                        centrality,
                    )
                    candidates.append(
                        Candidate(
                            move_id=node_id,
                            category="formal-run",
                            detail="searching run with an outstanding checkpoint",
                            conflict_ids=(target_id, *(edge.src_id for edge in view.edges_to(target_id, "SEARCHES"))),
                            features=features,
                        )
                    )

            # Formal failures surfaced to the research/coordinator loop.
            elif record.label == "Obstruction":
                if self._obstruction_is_handled(view, node_id):
                    continue
                raised_by = view.edges_to(node_id, "RAISED_OBSTRUCTION")
                if len(raised_by) != 1:
                    continue
                run_id = raised_by[0].src_id
                run = nodes.get(run_id)
                if (
                    run is None
                    or run.label != "FormalRun"
                    or run.fields.get("status") not in OBSTRUCTION_DISPOSITIONS
                ):
                    continue
                recorded_run = record.fields.get("formal_run_id")
                if recorded_run is not None and recorded_run != run_id:
                    continue
                state_ids = tuple(
                    dict.fromkeys(
                        edge.dst_id
                        for edge in view.edges_from(node_id, "AT_STATE")
                    )
                )
                if not state_ids or any(
                    (state := nodes.get(state_id)) is None
                    or state.label != "FormalState"
                    for state_id in state_ids
                ):
                    continue
                kernels = tuple(
                    dict.fromkeys(
                        edge.src_id
                        for edge in view.edges_to(node_id, "RESOLVES")
                    )
                )
                if len(kernels) != 1:
                    continue
                kernel = nodes.get(kernels[0])
                if kernel is None or kernel.label != "Claim":
                    continue
                environments = view.edges_from(run_id, "RAN_UNDER")
                if len(environments) != 1:
                    continue
                environment = nodes.get(environments[0].dst_id)
                if environment is None or environment.label != "Environment":
                    continue
                sibling_ids = tuple(
                    edge.dst_id
                    for edge in view.edges_from(run_id, "RAISED_OBSTRUCTION")
                    if edge.dst_id != node_id
                )
                features = self._candidate_features(
                    view, nodes, node_id, "obstruction-task",
                    {"expected_information_gain": 0.9,
                     "verification_value": 0.7,
                     "formalization_readiness": 0.5,
                     "estimated_cost": 1.5},
                    centrality,
                )
                candidates.append(
                    Candidate(
                        move_id=node_id,
                        category="obstruction-task",
                        detail=f"formal failure raised by {run_id}",
                        conflict_ids=(run_id, *sibling_ids, *state_ids),
                        features=features,
                    )
                )

            # Critic tasks: provisional claims with no favorable verdict (3.3).
            elif record.label == "Claim":
                if record.fields.get("status") != ClaimStatus.PROVISIONAL.value:
                    continue
                if self._has_favorable_verdict(view, node_id):
                    continue
                features = self._candidate_features(
                    view, nodes, node_id, "critic-task",
                    {"verification_value": 0.9,
                     "dependency_centrality": 0.25,
                     "estimated_cost": 1.5},
                    centrality,
                )
                candidates.append(
                    Candidate(
                        move_id=node_id,
                        category="critic-task",
                        detail="provisional claim awaiting a critic verdict",
                        features=features,
                    )
                )
        return candidates

    def _dependency_centrality(
        self, view: ReadView, nodes: Mapping[str, Any]
    ) -> dict[str, float]:
        """Normalized transitive DEPENDS_ON fan-in, scoped to committed Claims."""
        claims = {node_id for node_id, node in nodes.items() if node.label == "Claim"}
        dependents: dict[str, set[str]] = {claim_id: set() for claim_id in claims}
        for dependent in claims:
            for edge in view.edges_from(dependent, "DEPENDS_ON"):
                if edge.dst_id in claims:
                    dependents[edge.dst_id].add(dependent)
        denominator = max(len(claims) - 1, 1)
        centrality: dict[str, float] = {}
        for claim_id in sorted(claims):
            seen = {claim_id}
            pending = list(sorted(dependents[claim_id], reverse=True))
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                pending.extend(sorted(dependents.get(current, ()), reverse=True))
            centrality[claim_id] = min(1.0, (len(seen) - 1) / denominator)
        return centrality

    def _candidate_features(
        self,
        view: ReadView,
        nodes: Mapping[str, Any],
        move_id: str,
        category: str,
        base: Mapping[str, float],
        centrality: Mapping[str, float],
    ) -> dict[str, float]:
        """Overlay reproducible graph/annotation signals on category priors."""
        features = dict(base)
        record = nodes.get(move_id)
        if record is None:
            return features
        claim_ids = self._candidate_claim_ids(view, nodes, record)
        if claim_ids:
            features["dependency_centrality"] = sum(
                centrality.get(claim_id, 0.0) for claim_id in claim_ids
            ) / len(claim_ids)

        signal_records = [record]
        state_ids: set[str] = set()
        run_ids: set[str] = set()
        if record.label == "FormalRun":
            run_ids.add(record.node_id)
            state_ids.update(edge.dst_id for edge in view.edges_from(record.node_id, "HAS_ROOT"))
            for checkpoint_edge in view.edges_from(record.node_id, "HAS_CHECKPOINT"):
                state_ids.update(edge.dst_id for edge in view.edges_from(checkpoint_edge.dst_id, "CHECKPOINT_FRONTIER"))
        elif record.label == "Obstruction":
            state_ids.update(edge.dst_id for edge in view.edges_from(record.node_id, "AT_STATE"))
            run_ids.update(edge.src_id for edge in view.edges_to(record.node_id, "RAISED_OBSTRUCTION"))
        elif record.label == "FormalDeclaration":
            for edge in view.edges_to(record.node_id, "SEARCHES"):
                run_ids.add(edge.src_id)
                state_ids.update(root.dst_id for root in view.edges_from(edge.src_id, "HAS_ROOT"))
        elif record.label == "ResearchMove":
            state_ids.update(edge.src_id for edge in view.edges_to(record.node_id, "PROPOSES"))
        for state_id in sorted(state_ids):
            state = nodes.get(state_id) or view.node(state_id)
            if state is not None and state.label in {"FormalState", "ResearchState"}:
                signal_records.append(state)

        def annotation(record_: Any, field_name: str) -> float | None:
            raw = record_.fields.get(field_name)
            if raw is None:
                annotations = record_.fields.get("annotations")
                raw = annotations.get(field_name) if isinstance(annotations, Mapping) else None
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                return None
            return float(raw)

        for annotation_name, feature_name in (
            ("pln_strength", "pln_strength"),
            ("pln_confidence", "pln_confidence"),
            ("state_novelty", "state_novelty"),
        ):
            observed = [annotation(item, annotation_name) for item in signal_records]
            observed = [value for value in observed if value is not None]
            if observed:
                features[feature_name] = min(1.0, max(0.0, sum(observed) / len(observed)))

        proof_numbers = [annotation(item, "proof_number") for item in signal_records]
        proof_numbers = [value for value in proof_numbers if value is not None and value >= 0]
        if proof_numbers:
            features["proof_number_priority"] = sum(1.0 / (1.0 + value) for value in proof_numbers) / len(proof_numbers)
        disproof_numbers = [annotation(item, "disproof_number") for item in signal_records]
        disproof_numbers = [value for value in disproof_numbers if value is not None and value >= 0]
        if disproof_numbers:
            features["disproof_number_priority"] = sum(value / (1.0 + value) for value in disproof_numbers) / len(disproof_numbers)
        transpositions = [annotation(item, "transposition_count") for item in signal_records]
        transpositions = [value for value in transpositions if value is not None and value >= 0]
        if transpositions:
            features["transposition_novelty"] = sum(1.0 / (1.0 + value) for value in transpositions) / len(transpositions)

        failure = self._committed_failure_risk(view, nodes, record, state_ids, run_ids)
        if failure is not None:
            features["repeated_failure_risk"] = failure
        return features

    @staticmethod
    def _candidate_claim_ids(view: ReadView, nodes: Mapping[str, Any], record: Any) -> tuple[str, ...]:
        claims: set[str] = set()
        if record.label == "Claim":
            claims.add(record.node_id)
        elif record.label == "Obstruction":
            claims.update(edge.src_id for edge in view.edges_to(record.node_id, "RESOLVES"))
        elif record.label == "FormalRun":
            declarations = {edge.dst_id for edge in view.edges_from(record.node_id, "SEARCHES")}
            claims.update(
                nodes[declaration_id].fields["claim_id"]
                for declaration_id in declarations
                if declaration_id in nodes and nodes[declaration_id].fields.get("claim_id") in nodes
            )
        elif record.label == "FormalDeclaration":
            declared = record.fields.get("claim_id")
            if isinstance(declared, str) and declared in nodes and nodes[declared].label == "Claim":
                claims.add(declared)
            for alignment_edge in view.edges_to(record.node_id, "ALIGNS_DECLARATION"):
                claims.update(edge.dst_id for edge in view.edges_from(alignment_edge.src_id, "ALIGNS_CLAIM"))
        elif record.label == "ResearchMove":
            claims.update(edge.dst_id for edge in view.edges_from(record.node_id, "MOVE_TARGETS"))
            raw = record.fields.get("required_claim_ids", ())
            if isinstance(raw, (tuple, list)):
                claims.update(item for item in raw if isinstance(item, str) and item in nodes)
        return tuple(sorted(claim_id for claim_id in claims if claim_id in nodes and nodes[claim_id].label == "Claim"))

    @staticmethod
    def _committed_failure_risk(
        view: ReadView,
        nodes: Mapping[str, Any],
        record: Any,
        state_ids: set[str],
        run_ids: set[str],
    ) -> float | None:
        outcomes: list[bool] = []
        failed_dispositions = {
            RunDisposition.STAGNATED.value, RunDisposition.BUDGET_EXHAUSTED.value,
            RunDisposition.COUNTEREXAMPLE.value, RunDisposition.INVALID_REQUEST.value,
            RunDisposition.ENVIRONMENT_ERROR.value, RunDisposition.INTERNAL_ERROR.value,
        }
        completed_dispositions = {RunDisposition.PROVED_PENDING_REPLAY.value}
        failed_executor_results = {"lean-rejected", "timeout", "backend-missing", "parse-failure", "crash", "empty-output"}
        attempt_ids: set[str] = set()
        for state_id in state_ids:
            attempt_ids.update(edge.src_id for edge in view.edges_to(state_id, "ON_STATE"))
        if record.label == "Claim":
            attempt_ids.update(edge.src_id for edge in view.edges_to(record.node_id, "REVIEWS_CLAIM"))
        for attempt_id in sorted(attempt_ids):
            attempt = nodes.get(attempt_id) or view.node(attempt_id)
            if attempt is None or attempt.label != "Attempt":
                continue
            status = attempt.fields.get("status")
            result = attempt.fields.get("executor_result")
            if status in {AttemptStatus.REFUTED.value, AttemptStatus.RETRACTED.value} or result in failed_executor_results:
                outcomes.append(True)
            elif status in {AttemptStatus.SUPPORTED.value, AttemptStatus.CRITIC_ACCEPTED.value} or result == "lean-accepted":
                outcomes.append(False)
        for run_id in sorted(run_ids):
            run = nodes.get(run_id) or view.node(run_id)
            if run is None or run.label != "FormalRun":
                continue
            history = run.fields.get("attempt_history", ())
            if isinstance(history, (tuple, list)) and history:
                for item in history:
                    if isinstance(item, Mapping):
                        disposition = item.get("disposition")
                        if disposition in failed_dispositions:
                            outcomes.append(True)
                        elif disposition in completed_dispositions:
                            outcomes.append(False)
            else:
                disposition = run.fields.get("status")
                if disposition in failed_dispositions:
                    outcomes.append(True)
                elif disposition in completed_dispositions:
                    outcomes.append(False)
        if not outcomes:
            return None
        # Beta(1,1) smoothing gives a neutral 0.5 when only one observation exists.
        return (sum(outcomes) + 1.0) / (len(outcomes) + 2.0)

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
        if "repeated_failure_risk" not in candidate.features:
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
        if self._portfolio is None:
            return {"policy_name": self._policy.name, "weights": dict(self._policy.weights)}
        return {
            "portfolio_name": self._portfolio.name,
            "selection": "round-robin by issued lease audit count",
            "policies": [
                {"policy_name": policy.name, "weights": dict(policy.weights)}
                for policy in self._portfolio.policies
            ],
        }

    # -- helpers ---------------------------------------------------------------
    def _node_ids(self, view: ReadView, proof_id: str) -> list[str]:
        return [record.node_id for record in view.list_nodes(proof_id)]

    def _policies(self) -> tuple[SchedulerPolicy, ...]:
        return self._portfolio.policies if self._portfolio is not None else (self._policy,)

    def _policy_audit_config(self, policy: SchedulerPolicy) -> dict[str, Any]:
        config: dict[str, Any] = {
            "policy_name": policy.name,
            "weights": dict(policy.weights),
        }
        if self._portfolio is not None:
            config.update({
                "portfolio_name": self._portfolio.name,
                "rotation": "atomic round-robin by issued lease audit sequence",
            })
        return config

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
