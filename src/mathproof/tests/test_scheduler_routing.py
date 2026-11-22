"""Global dispatch chooses a worker from the selected move, not the caller."""

import math
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.scheduler import FEATURES, GlobalScheduler, SchedulerPolicy, SchedulerPortfolio
from mathproof.schemas import validate


ACTORS = {
    "critic": "critic-service",
    "formal-atp": "atp-service",
    "llm-research": "research-service",
}


def frontier() -> MemoryView:
    view = MemoryView()
    view.add_node("p1/c-1", "Claim", {"status": "provisional"})
    view.add_node("p1/fd-1", "FormalDeclaration", {"status": "aligned"})
    view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
    view.add_node("p1/rm-1", "ResearchMove", {"status": "queued"})
    view.add_edge("PROPOSES", "p1/rs-1", "p1/rm-1", "p1/e-1")
    return view


def policy(**weights: float) -> SchedulerPolicy:
    return SchedulerPolicy(weights={name: weights.get(name, 0.0) for name in FEATURES})


@pytest.mark.parametrize(
    "feature, move_id, worker_class",
    [
        ("verification_value", "p1/c-1", "critic"),
        ("formalization_readiness", "p1/fd-1", "formal-atp"),
        ("expected_information_gain", "p1/rm-1", "llm-research"),
    ],
)
def test_global_ranking_binds_the_selected_moves_worker(feature, move_id, worker_class):
    view, store = frontier(), JournalStore()
    scheduler = GlobalScheduler(
        view, store, policy=policy(**{feature: 1.0}), worker_actors=ACTORS,
    )

    lease = scheduler.lease_best_global_move("p1")

    assert lease.selected_move_id == move_id
    assert lease.worker_class == worker_class
    assert lease.actor == ACTORS[worker_class]
    assert validate("lease", lease.to_dict()) == []
    assert set(lease.score_snapshot) == set(FEATURES)
    issued = store.read_lease_events("p1")
    assert len(issued) == 1
    assert issued[0]["worker_class"] == worker_class
    assert issued[0]["selected_move_id"] == move_id
    assert store.read_events("p1") == []


def test_unavailable_top_route_is_skipped_before_leasing():
    store = JournalStore()
    scheduler = GlobalScheduler(
        frontier(), store, policy=policy(verification_value=1.0),
        worker_actors={"formal-atp": ACTORS["formal-atp"]},
    )

    lease = scheduler.lease_best_global_move("p1")

    assert (lease.selected_move_id, lease.worker_class) == ("p1/fd-1", "formal-atp")
    assert len(store.read_lease_events("p1")) == 1


def test_busy_top_route_falls_through_to_next_free_route():
    store = JournalStore()
    store.issue_lease(
        "p1", "p1/ls-busy", actor="other-critic", worker_class="critic",
        selected_move_id="p1/c-1",
    )
    scheduler = GlobalScheduler(
        frontier(), store, policy=policy(verification_value=1.0),
        worker_actors=ACTORS,
    )

    lease = scheduler.lease_best_global_move("p1")

    assert (lease.selected_move_id, lease.worker_class) == ("p1/fd-1", "formal-atp")
    assert {row.selected_move_id for row in store.active_leases("p1")} == {
        "p1/c-1", "p1/fd-1",
    }


def test_no_available_route_is_idle_without_a_lease_or_proof_event():
    store = JournalStore()
    scheduler = GlobalScheduler(frontier(), store, worker_actors={})

    assert scheduler.lease_best_global_move("p1") is None
    assert store.read_lease_events("p1") == []
    assert store.read_events("p1") == []


@pytest.mark.parametrize(
    "actors",
    [
        {"unknown-worker": "trusted-service"},
        {"critic": " "},
        {"critic": None},
    ],
)
def test_route_configuration_rejects_unknown_or_unbound_workers(actors):
    with pytest.raises(ValueError):
        GlobalScheduler(frontier(), JournalStore(), worker_actors=actors)


def test_actor_configuration_is_copied_before_dispatch():
    actors = dict(ACTORS)
    scheduler = GlobalScheduler(
        frontier(), JournalStore(), policy=policy(verification_value=1.0),
        worker_actors=actors,
    )
    actors["critic"] = "substituted-actor"

    assert scheduler.lease_best_global_move("p1").actor == ACTORS["critic"]


def test_equal_priority_uses_stable_move_id_order():
    view = MemoryView()
    view.add_node("p1/c-2", "Claim", {"status": "provisional"})
    view.add_node("p1/c-1", "Claim", {"status": "provisional"})
    scheduler = GlobalScheduler(
        view, JournalStore(), policy=policy(),
        worker_actors={"critic": ACTORS["critic"]},
    )

    assert scheduler.lease_best_global_move("p1").selected_move_id == "p1/c-1"


def test_nonfinite_priority_cannot_create_a_lease():
    store = JournalStore()
    with pytest.raises(ValueError, match="weights must be finite"):
        SchedulerPolicy(weights=policy(verification_value=math.nan).weights)
    assert store.read_lease_events("p1") == []


def test_policy_portfolio_rotates_deterministically_by_issued_lease_audit():
    store = JournalStore()
    portfolio = SchedulerPortfolio((
        SchedulerPolicy(name="verification-first", weights=policy(verification_value=1.0).weights),
        SchedulerPolicy(name="formalization-first", weights=policy(formalization_readiness=1.0).weights),
    ))
    scheduler = GlobalScheduler(frontier(), store, portfolio=portfolio, worker_actors=ACTORS)

    first = scheduler.lease_best_global_move("p1")
    assert first is not None and first.selected_move_id == "p1/c-1"
    assert scheduler.release("p1", first.lease_id, fencing_token=first.fencing_token)
    second = scheduler.lease_best_global_move("p1")
    assert second is not None and second.selected_move_id == "p1/fd-1"
    issued = [event for event in store.read_lease_events("p1") if event["event"] == "issued"]
    assert [event["policy_name"] for event in issued] == [
        "verification-first", "formalization-first",
    ]
    assert issued[0]["policy_config"]["weights"] == dict(portfolio.policies[0].weights)
    assert issued[1]["policy_config"]["weights"] == dict(portfolio.policies[1].weights)


def test_concurrent_portfolio_rotation_is_atomic_and_persisted(tmp_path):
    path = str(tmp_path / "portfolio.db")
    JournalStore(path)._conn.close()
    view = frontier()
    portfolio = SchedulerPortfolio((
        SchedulerPolicy(name="verification-first", weights=policy(verification_value=1.0).weights),
        SchedulerPolicy(name="formalization-first", weights=policy(formalization_readiness=1.0).weights),
    ))
    barrier = Barrier(2)

    class RacingStore(JournalStore):
        def claim_best_routed(self, *args, **kwargs):
            barrier.wait(timeout=10)
            return super().claim_best_routed(*args, **kwargs)

    def dispatch():
        store = RacingStore(path)
        try:
            scheduler = GlobalScheduler(view, store, portfolio=portfolio, worker_actors=ACTORS)
            return scheduler.lease_best_global_move("p1")
        finally:
            store._conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(lambda _index: dispatch(), range(2)))

    assert all(lease is not None for lease in leases)
    assert {lease.policy_name for lease in leases} == {
        "verification-first", "formalization-first",
    }
    store = JournalStore(path)
    try:
        issued = [event for event in store.read_lease_events("p1") if event["event"] == "issued"]
        assert [event["policy_name"] for event in issued] == [
            "verification-first", "formalization-first",
        ]
        assert [event["policy_config"]["weights"] for event in issued] == [
            dict(portfolio.policies[0].weights), dict(portfolio.policies[1].weights),
        ]
    finally:
        store._conn.close()


def test_committed_annotations_change_formal_candidate_ranking():
    view = frontier()
    view.add_node("p1/fd-2", "FormalDeclaration", {"status": "aligned", "annotations": {
        "pln_strength": 0.95, "pln_confidence": 0.9, "proof_number": 1,
        "disproof_number": 20, "state_novelty": 0.9, "transposition_count": 0,
    }})
    view.add_node("p1/fd-1", "FormalDeclaration", {"status": "aligned", "annotations": {
        "pln_strength": 0.1, "pln_confidence": 0.2, "proof_number": 50,
        "disproof_number": 1, "state_novelty": 0.1, "transposition_count": 9,
    }})
    weights = {name: 0.0 for name in FEATURES}
    weights.update({name: 1.0 for name in (
        "pln_strength", "pln_confidence", "proof_number_priority",
        "disproof_number_priority", "state_novelty", "transposition_novelty",
    )})
    scheduler = GlobalScheduler(view, JournalStore(), policy=SchedulerPolicy(weights=weights),
        worker_actors={"formal-atp": "atp"})

    lease = scheduler.lease_best_global_move("p1")

    assert lease is not None and lease.selected_move_id == "p1/fd-2"
    assert lease.score_snapshot["pln_strength"] == 0.95
    assert lease.score_snapshot["proof_number_priority"] == 0.5
    assert lease.score_snapshot["transposition_novelty"] == 1.0


def test_parent_research_state_annotations_change_research_move_ranking():
    view = MemoryView()
    view.add_node("p1/rs-1", "ResearchState", {
        "status": "open",
        "annotations": {"state_novelty": 0.1, "pln_strength": 0.2, "pln_confidence": 0.3},
    })
    view.add_node("p1/rs-2", "ResearchState", {
        "status": "open",
        "annotations": {"state_novelty": 0.9, "pln_strength": 0.8, "pln_confidence": 0.9},
    })
    for index in (1, 2):
        move_id = f"p1/rm-{index}"
        view.add_node(move_id, "ResearchMove", {"status": "queued"})
        view.add_edge("PROPOSES", f"p1/rs-{index}", move_id, f"p1/e-proposes-{index}")
    weights = {name: 0.0 for name in FEATURES}
    weights.update({"state_novelty": 1.0, "pln_strength": 1.0, "pln_confidence": 1.0})
    scheduler = GlobalScheduler(view, JournalStore(), policy=SchedulerPolicy(weights=weights),
        worker_actors={"llm-research": "research"})

    lease = scheduler.lease_best_global_move("p1")

    assert lease is not None and lease.selected_move_id == "p1/rm-2"
    assert lease.score_snapshot["state_novelty"] == 0.9
    assert lease.score_snapshot["pln_strength"] == 0.8
    assert lease.score_snapshot["pln_confidence"] == 0.9


def test_transitive_dependency_fan_in_changes_critic_ranking():
    view = MemoryView()
    for claim_id in ("p1/c-1", "p1/c-2", "p1/c-3", "p1/c-4", "p1/c-5"):
        view.add_node(claim_id, "Claim", {"status": "provisional"})
    view.add_edge("DEPENDS_ON", "p1/c-3", "p1/c-2", "p1/e-c3-c2")
    view.add_edge("DEPENDS_ON", "p1/c-4", "p1/c-2", "p1/e-c4-c2")
    view.add_edge("DEPENDS_ON", "p1/c-5", "p1/c-3", "p1/e-c5-c3")
    scheduler = GlobalScheduler(view, JournalStore(), policy=policy(dependency_centrality=1.0),
        worker_actors={"critic": "critic"})

    lease = scheduler.lease_best_global_move("p1")

    assert lease is not None and lease.selected_move_id == "p1/c-2"
    assert lease.score_snapshot["dependency_centrality"] == 0.75


def test_committed_review_failures_affect_repeat_risk_after_scheduler_restart():
    view = MemoryView()
    for claim_id in ("p1/c-1", "p1/c-2"):
        view.add_node(claim_id, "Claim", {"status": "provisional"})
    for index, status in enumerate(("refuted", "refuted"), start=1):
        attempt_id = f"p1/at-{index}"
        view.add_node(attempt_id, "Attempt", {"worker_class": "critic", "status": status})
        view.add_edge("REVIEWS_CLAIM", attempt_id, "p1/c-1", f"p1/e-review-{index}")
    weights = {name: 0.0 for name in FEATURES}
    weights["repeated_failure_risk"] = -1.0
    scheduler = GlobalScheduler(view, JournalStore(), policy=SchedulerPolicy(weights=weights),
        worker_actors={"critic": "critic"})

    lease = scheduler.lease_best_global_move("p1")

    assert lease is not None and lease.selected_move_id == "p1/c-2"
    assert lease.score_snapshot["repeated_failure_risk"] == pytest.approx(0.5)


def test_racing_global_schedulers_reserve_the_one_move_once(tmp_path):
    path = str(tmp_path / "journal.db")
    JournalStore(path)._conn.close()
    view = MemoryView()
    view.add_node("p1/c-1", "Claim", {"status": "provisional"})
    barrier = Barrier(2)

    class RacingStore(JournalStore):
        def claim_best_routed(self, *args, **kwargs):
            barrier.wait(timeout=10)
            return super().claim_best_routed(*args, **kwargs)

    def dispatch():
        store = RacingStore(path)
        try:
            return GlobalScheduler(
                view, store, worker_actors={"critic": ACTORS["critic"]},
            ).lease_best_global_move("p1")
        finally:
            store._conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = [lease for lease in pool.map(lambda _: dispatch(), range(2)) if lease]

    assert len(leases) == 1
    store = JournalStore(path)
    try:
        assert len(store.active_leases("p1")) == 1
        assert len(store.read_lease_events("p1")) == 1
        assert store.head("p1")[0] == 0
    finally:
        store._conn.close()


def test_racing_global_schedulers_route_distinct_classes_atomically(tmp_path):
    path = str(tmp_path / "journal.db")
    JournalStore(path)._conn.close()
    view = MemoryView()
    view.add_node("p1/c-1", "Claim", {"status": "provisional"})
    view.add_node("p1/fd-1", "FormalDeclaration", {"status": "aligned"})
    barrier = Barrier(2)

    class RacingStore(JournalStore):
        def claim_best_routed(self, *args, **kwargs):
            barrier.wait(timeout=10)
            return super().claim_best_routed(*args, **kwargs)

    def dispatch():
        store = RacingStore(path)
        try:
            return GlobalScheduler(
                view, store, policy=policy(verification_value=1.0),
                worker_actors=ACTORS,
            ).lease_best_global_move("p1")
        finally:
            store._conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(lambda _: dispatch(), range(2)))

    assert {(lease.selected_move_id, lease.worker_class) for lease in leases} == {
        ("p1/c-1", "critic"), ("p1/fd-1", "formal-atp"),
    }
    store = JournalStore(path)
    try:
        assert len(store.active_leases("p1")) == 2
        assert len({lease.fencing_token for lease in leases}) == 2
        assert store.head("p1")[0] == 0
    finally:
        store._conn.close()
