"""Global dispatch chooses a worker from the selected move, not the caller."""

import math
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.scheduler import FEATURES, GlobalScheduler, SchedulerPolicy
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
        {"coordinator": "trusted-service"},
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
    scheduler = GlobalScheduler(
        frontier(), store, policy=policy(verification_value=math.nan),
        worker_actors=ACTORS,
    )

    with pytest.raises(ValueError, match="finite"):
        scheduler.lease_best_global_move("p1")
    assert store.read_lease_events("p1") == []


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
