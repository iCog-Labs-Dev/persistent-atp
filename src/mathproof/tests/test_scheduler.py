"""The global scheduler: frontier correctness, scoring, auditability.

Covers the sign-off tests that need no live dispatch: the eligible set
(Section 3), exclusion rules, config-driven selection, score-snapshot
auditability (Section 8.4), statistics feedback, the empty-frontier terminal
state, and the Invariant 7/9 boundary -- scheduling never touches status.
"""

import unittest

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.vocab import WorkerClass
from mathproof.ids import parse_local_id
from mathproof.scheduler import (
    FEATURES,
    SchedulerPolicy,
    SchedulerStatistics,
    GlobalScheduler,
)


def fixture_view() -> MemoryView:
    """One proof carrying every frontier category, plus excluded lookalikes."""
    v = MemoryView()
    # Research layer: one live move, one under a superseded parent state.
    v.add_node("p1/rs-1", "ResearchState", {"status": "open"})
    v.add_node("p1/rs-2", "ResearchState", {"status": "superseded"})
    v.add_node("p1/rm-1", "ResearchMove", {"status": "queued"})
    v.add_node("p1/rm-2", "ResearchMove", {"status": "queued"})
    v.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/e-rm1")
    v.add_edge("PROPOSES", "p1/rs-2", "p1/rm-2", "p1/e-rm2")
    # Claims: one clean provisional, one already reviewed, one tainted.
    v.add_node("p1/c-1", "Claim", {"status": "provisional"})
    v.add_node("p1/c-2", "Claim", {"status": "provisional"})
    v.add_node(
        "p1/at-9", "Attempt", {"worker_class": "critic", "status": "supported"}
    )
    v.add_edge("REVIEWS_CLAIM", "p1/at-9", "p1/c-2", "p1/e-rev9")
    v.add_node("p1/c-3", "Claim", {"status": "tainted"})
    # Formal layer: aligned declaration; searching runs with and without an
    # open checkpoint frontier; a closed declaration; a stagnated run.
    v.add_node("p1/fd-1", "FormalDeclaration", {"status": "aligned"})
    v.add_node("p1/fd-2", "FormalDeclaration", {"status": "replay-accepted"})
    v.add_node("p1/fs-open", "FormalState", {"status": "open"})
    v.add_node("p1/fs-done", "FormalState", {"status": "formally-closed"})
    v.add_node("p1/fr-1", "FormalRun", {"status": "searching"})
    v.add_node("p1/fr-2", "FormalRun", {"status": "searching"})
    v.add_node("p1/fr-3", "FormalRun", {"status": "stagnated"})
    v.add_node("p1/fc-1", "FormalCheckpoint", {})
    v.add_node("p1/fc-2", "FormalCheckpoint", {})
    v.add_edge("HAS_CHECKPOINT", "p1/fr-1", "p1/fc-1", "p1/e-fc1")
    v.add_edge("CHECKPOINT_FRONTIER", "p1/fc-1", "p1/fs-open", "p1/e-cf1")
    v.add_edge("HAS_CHECKPOINT", "p1/fr-2", "p1/fc-2", "p1/e-fc2")
    v.add_edge("CHECKPOINT_FRONTIER", "p1/fc-2", "p1/fs-done", "p1/e-cf2")
    return v


ELIGIBLE = {"p1/rm-1", "p1/c-1", "p1/fd-1", "p1/fr-1"}


def zeroed(**overrides) -> SchedulerPolicy:
    weights = {name: 0.0 for name in FEATURES}
    weights.update(overrides)
    return SchedulerPolicy(weights=weights)


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.store = JournalStore()
        self.view = fixture_view()

    def scheduler(self, **kwargs) -> GlobalScheduler:
        return GlobalScheduler(
            self.view, self.store, clock=lambda: 1000.0, **kwargs
        )


class TestFrontier(SchedulerCase):
    def test_the_eligible_set_is_exactly_expected(self):
        moves = {
            candidate.move_id for candidate in self.scheduler().build_frontier("p1")
        }
        self.assertEqual(moves, ELIGIBLE)

    def test_repeat_queries_are_stable(self):
        scheduler = self.scheduler()
        first = {c.move_id for c in scheduler.build_frontier("p1")}
        second = {c.move_id for c in scheduler.build_frontier("p1")}
        self.assertEqual(first, second)

    def test_other_proofs_are_invisible(self):
        moves = {c.move_id for c in self.scheduler().build_frontier("p2")}
        self.assertEqual(moves, set())


class TestLeaseNext(SchedulerCase):
    def test_lease_next_returns_a_schema_shaped_lease(self):
        from mathproof.schemas import validate

        lease = self.scheduler().lease_next(
            "p1", WorkerClass.CRITIC.value, ttl_seconds=120.0
        )
        self.assertIsNotNone(lease)
        self.assertEqual(validate("lease", lease.to_dict()), [])

    def test_lease_ids_come_from_the_allocator_namespace(self):
        lease = self.scheduler().lease_next("p1", "critic")
        prefix, _serial = parse_local_id(lease.lease_id.split("/", 1)[1])
        self.assertEqual(prefix.value, "ls")

    def test_empty_frontier_is_none_not_an_exception(self):
        scheduler = GlobalScheduler(MemoryView(), JournalStore())
        self.assertIsNone(scheduler.lease_next("p3", "formal-atp"))

    def test_a_leased_move_is_never_double_dispatched(self):
        scheduler = self.scheduler()
        seen = set()
        for _ in range(len(ELIGIBLE)):
            lease = scheduler.lease_next("p1", WorkerClass.COORDINATOR.value)
            self.assertIsNotNone(lease)
            self.assertNotIn(lease.selected_move_id, seen)
            seen.add(lease.selected_move_id)
        self.assertIsNone(scheduler.lease_next("p1", WorkerClass.COORDINATOR.value))
        self.assertEqual(seen, ELIGIBLE)

    def test_every_snapshot_names_all_ten_features_and_is_journalled(self):
        scheduler = self.scheduler()
        lease = scheduler.lease_next("p1", "critic")
        self.assertEqual(set(lease.score_snapshot.keys()), set(FEATURES))

        issued = [
            event
            for event in self.store.read_lease_events("p1")
            if event["event"] == "issued"
        ]
        self.assertEqual(len(issued), 1)
        self.assertEqual(set(issued[0]["score_snapshot"].keys()), set(FEATURES))


class TestScoring(SchedulerCase):
    def test_weights_are_configuration_not_code(self):
        critic_first = zeroed(verification_value=1.0)
        formal_first = zeroed(formalization_readiness=1.0)

        by_verification = GlobalScheduler(
            fixture_view(), JournalStore(), policy=critic_first
        )
        by_readiness = GlobalScheduler(
            fixture_view(), JournalStore(), policy=formal_first
        )

        self.assertEqual(
            by_verification.lease_next("p1", "critic").selected_move_id, "p1/c-1"
        )
        self.assertEqual(
            by_readiness.lease_next("p1", "formal-atp").selected_move_id, "p1/fd-1"
        )

    def test_availability_feature_prefers_the_matching_worker_class(self):
        scheduler = self.scheduler(policy=zeroed(availability_of_suitable_worker_or_model_or_tool=1.0))
        lease = scheduler.lease_next("p1", WorkerClass.FORMAL_ATP.value)
        self.assertIn(lease.selected_move_id, {"p1/fd-1", "p1/fr-1"})

    def test_unpopulated_features_take_neutral_defaults_not_zeros(self):
        from mathproof.scheduler import NEUTRAL_DEFAULTS

        scheduler = self.scheduler()
        vector = scheduler._feature_vector(
            next(c for c in scheduler.build_frontier("p1") if c.category == "research-move"),
            "llm-research",
        )
        for name in ("expected_theorem_impact", "human_priority"):
            self.assertEqual(vector[name], NEUTRAL_DEFAULTS[name])
            self.assertNotEqual(vector[name], 0.0)


class TestStatistics(SchedulerCase):
    def test_commit_outcomes_feed_repeated_failure_risk(self):
        statistics = SchedulerStatistics()
        self.assertAlmostEqual(statistics.failure_risk("critic-task"), 0.5)

        statistics.record("critic-task", "failed")
        statistics.record("critic-task", "succeeded")
        self.assertAlmostEqual(statistics.failure_risk("critic-task"), 0.5)
        statistics.record("critic-task", "failed")
        self.assertAlmostEqual(statistics.failure_risk("critic-task"), 2 / 3)

        scheduler = GlobalScheduler(self.view, self.store, statistics=statistics)
        vector = scheduler._feature_vector(
            next(
                c
                for c in scheduler.build_frontier("p1")
                if c.category == "critic-task"
            ),
            "critic",
        )
        self.assertAlmostEqual(vector["repeated_failure_risk"], 2 / 3)

    def test_update_statistics_records_gate_outcomes_by_category(self):
        from commit_gate.gate import CommitResult

        scheduler = self.scheduler()
        accepted = CommitResult(True, (), "hash", 1)
        refused = CommitResult(False, (), None, None)
        scheduler.update_statistics(accepted, "critic-task")
        scheduler.update_statistics(refused, "critic-task")
        self.assertEqual(scheduler.statistics.attempts("critic-task"), 2)


class TestInvariantBoundary(SchedulerCase):
    def test_scheduling_writes_no_committed_state(self):
        """Invariant 7/9 negative test: only leases move, the graph stands still."""
        before = {
            node_id: (record.label, dict(record.fields))
            for node_id, record in self.view.nodes.items()
        }
        scheduler = self.scheduler()
        for _ in range(len(ELIGIBLE)):
            if scheduler.lease_next("p1", "coordinator") is None:
                break
        after = {
            node_id: (record.label, dict(record.fields))
            for node_id, record in self.view.nodes.items()
        }
        self.assertEqual(before, after)
        self.assertEqual(self.store.read_events("p1"), [])


if __name__ == "__main__":
    unittest.main()
