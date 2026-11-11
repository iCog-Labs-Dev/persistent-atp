"""Regression tests for lease ownership and scheduling from current state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from commit_gate.gate import CommitGate
from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import ConcurrencyError, JournalStore
from mathproof.scheduler import GlobalScheduler


def claims(count=1):
    view = MemoryView()
    for serial in range(1, count + 1):
        view.add_node(f"p1/c-{serial}", "Claim", {"status": "provisional"})
    return view


def formal_runs(count=1):
    view = MemoryView()
    view.add_node("p1/fd-1", "FormalDeclaration", {"status": "searching"})
    view.add_node("p1/fs-1", "FormalState", {"status": "open"})
    view.add_node("p1/fc-1", "FormalCheckpoint", {"epoch_ms": 1})
    view.add_edge("CHECKPOINT_FRONTIER", "p1/fc-1", "p1/fs-1", "p1/e-1")
    for serial in range(1, count + 1):
        run = f"p1/fr-{serial}"
        view.add_node(run, "FormalRun", {"status": "searching"})
        view.add_edge("SEARCHES", run, "p1/fd-1", f"p1/e-{serial * 2}")
        view.add_edge("HAS_CHECKPOINT", run, "p1/fc-1", f"p1/e-{serial * 2 + 1}")
    return view


def proposal(lease, *ops):
    return Proposal(
        proof_id=lease.proof_id, actor=lease.actor, worker_class=lease.worker_class,
        base_revision=lease.base_revision, lease_id=lease.lease_id,
        fencing_token=lease.fencing_token, selected_move_id=lease.selected_move_id,
        ops=ops,
    )


def commit(view, store, *ops):
    lease = store.issue_lease(
        "p1", "writer", actor="writer", worker_class="coordinator", selected_move_id="p1/c-1",
    )
    result = CommitGate(view, store).commit(proposal(lease, *ops))
    store.release_lease("p1", lease.lease_id, fencing_token=lease.fencing_token)
    assert result.accepted, result.rejections


def test_scheduler_lease_can_commit_through_gate_and_release():
    view, store = claims(), JournalStore()
    scheduler = GlobalScheduler(view, store)
    lease = scheduler.lease_next("p1", "critic", actor="critic-a")
    result = CommitGate(view, store).commit(proposal(
        lease, SetField("Claim", "p1/c-1", "status", "refuted", prior="provisional"),
    ))
    assert result.accepted, result.rejections
    assert store.verify_chain("p1") == 1
    assert scheduler.release("p1", lease.lease_id, fencing_token=lease.fencing_token)


def test_restart_does_not_overwrite_an_existing_lease(tmp_path):
    path = str(tmp_path / "journal.db")
    view, store = claims(2), JournalStore(path)
    first = GlobalScheduler(view, store).lease_next("p1", "critic", actor="a")
    store._conn.close()
    store = JournalStore(path)
    try:
        second = GlobalScheduler(view, store).lease_next("p1", "critic", actor="b")
        assert first.lease_id != second.lease_id
        assert first.selected_move_id != second.selected_move_id
        assert len(store.active_leases("p1")) == 2
    finally:
        store._conn.close()


def test_generated_id_does_not_overwrite_a_manual_lease():
    view, store = claims(2), JournalStore()
    manual = store.issue_lease(
        "p1", "p1/ls-2", actor="a", worker_class="critic", selected_move_id="p1/c-1",
    )
    lease = GlobalScheduler(view, store).lease_next("p1", "critic", actor="b")
    assert lease.lease_id != manual.lease_id
    assert len(store.active_leases("p1")) == 2


@pytest.mark.parametrize("kind, count, expected", [("claim", 1, 1), ("claim", 2, 2), ("formal", 2, 1)])
def test_racing_schedulers_do_not_replace_or_duplicate_work(tmp_path, kind, count, expected):
    path = str(tmp_path / "journal.db")
    JournalStore(path)._conn.close()
    view = claims(count) if kind == "claim" else formal_runs(count)
    barrier = Barrier(2)

    class RacingStore(JournalStore):
        def claim_next(self, *args, **kwargs):
            barrier.wait(timeout=10)
            return super().claim_next(*args, **kwargs)

    def dispatch(actor):
        store = RacingStore(path)
        try:
            return GlobalScheduler(view, store).lease_next("p1", "coordinator", actor=actor)
        finally:
            store._conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = [lease for lease in pool.map(dispatch, ("a", "b")) if lease is not None]
    assert len(leases) == expected
    assert len({lease.selected_move_id for lease in leases}) == expected
    store = JournalStore(path)
    try:
        assert len(store.active_leases("p1")) == expected
        assert [event["event"] for event in store.read_lease_events("p1")] == ["issued"] * expected
        assert store.head("p1")[0] == 0
    finally:
        store._conn.close()


def test_stale_release_cannot_cancel_a_replacement():
    store = JournalStore()
    scheduler = GlobalScheduler(claims(), store)
    old = scheduler.lease_next("p1", "critic", actor="a")
    new = store.issue_lease(
        "p1", old.lease_id, actor="b", worker_class="critic", selected_move_id=old.selected_move_id,
    )
    assert not scheduler.release("p1", old.lease_id, fencing_token=old.fencing_token)
    assert store.active_leases("p1") == [new]


def test_exclusive_lease_blocks_dispatch_until_released():
    store = JournalStore()
    token = store.acquire_lease("p1", "exclusive", actor="owner", worker_class="coordinator")
    scheduler = GlobalScheduler(claims(), store)
    assert scheduler.lease_next("p1", "critic", actor="critic") is None
    store.release_lease("p1", "exclusive", fencing_token=token)
    assert scheduler.lease_next("p1", "critic", actor="critic") is not None


def test_expiry_allows_reassignment_without_reusing_the_old_lease():
    now = [1_000_000_000]
    store = JournalStore(clock_ns=lambda: now[0])
    scheduler = GlobalScheduler(claims(), store)
    first = scheduler.lease_next("p1", "critic", actor="a", ttl_seconds=1)
    now[0] += 1_000_000_000
    second = scheduler.lease_next("p1", "critic", actor="b")
    assert second.selected_move_id == first.selected_move_id
    assert second.lease_id != first.lease_id
    assert second.fencing_token > first.fencing_token
    assert [event["event"] for event in store.read_lease_events("p1")] == ["issued", "expired", "issued"]


def test_journal_changes_are_seen_without_mutating_the_projection():
    view, store = claims(), JournalStore()
    commit(view, store,
        SetField("Claim", "p1/c-1", "status", "refuted", prior="provisional"),
        UpsertNode("Claim", "p1/c-2", {"status": "provisional"}),
    )
    lease = GlobalScheduler(view, store).lease_next("p1", "critic", actor="critic")
    assert lease.selected_move_id == "p1/c-2"
    assert lease.base_revision == 1
    assert view.node("p1/c-1").fields["status"] == "provisional"
    assert view.node("p1/c-2") is None
    assert view.snapshot("p1")[0] == 0


def test_already_projected_events_are_not_applied_twice():
    view, store = claims(), JournalStore()
    commit(view, store, SetField("Claim", "p1/c-1", "status", "refuted", prior="provisional"))
    view.apply_journal_event(store, "p1", 1)
    commit(view, store, UpsertNode("Claim", "p1/c-2", {"status": "provisional"}))
    lease = GlobalScheduler(view, store).lease_next("p1", "critic", actor="critic")
    assert lease.selected_move_id == "p1/c-2"
    assert lease.base_revision == 2
    assert view.snapshot("p1")[0] == 1


def test_revision_race_does_not_issue_a_stale_assignment():
    view = claims()

    class RacingStore(JournalStore):
        def claim_next(self, *args, **kwargs):
            commit(view, self, SetField("Claim", "p1/c-1", "status", "refuted", prior="provisional"))
            return super().claim_next(*args, **kwargs)

    store = RacingStore()
    with pytest.raises(ConcurrencyError) as error:
        GlobalScheduler(view, store).lease_next("p1", "critic", actor="critic")
    assert error.value.reason == Reason.STALE_BASE_REVISION
    assert store.active_leases("p1") == []


@pytest.mark.parametrize("revision", [True, -1, 1, "0"])
def test_invalid_snapshot_revision_cannot_dispatch(revision):
    class InvalidView(MemoryView):
        def snapshot(self, proof_id):
            _, snapshot = super().snapshot(proof_id)
            return revision, snapshot

    store = JournalStore()
    with pytest.raises(ConcurrencyError) as error:
        GlobalScheduler(InvalidView(), store).lease_next("p1", "critic", actor="critic")
    assert error.value.reason == Reason.READ_VIEW_OUT_OF_SYNC
    assert store.read_lease_events("p1") == []


@pytest.mark.parametrize("busy", ["p1/fd-1", "p1/fr-1"])
def test_declaration_and_run_are_not_dispatched_as_separate_work(busy):
    view, store = formal_runs(), JournalStore()
    scheduler = GlobalScheduler(view, store)
    assert [candidate.move_id for candidate in scheduler.build_frontier("p1")] == ["p1/fr-1"]
    store.issue_lease("p1", "busy", actor="a", worker_class="formal-atp", selected_move_id=busy)
    assert scheduler.lease_next("p1", "formal-atp", actor="b") is None


@pytest.mark.parametrize("status", ["stale", "certificate-produced", "replay-accepted"])
def test_finished_or_stale_declaration_cannot_resume_a_run(status):
    view = formal_runs()
    view.set_field("p1/fd-1", "status", status)
    assert GlobalScheduler(view, JournalStore()).build_frontier("p1") == []


def test_run_without_a_declaration_is_not_dispatched():
    view = formal_runs()
    view.remove_edge("p1/e-2")
    assert GlobalScheduler(view, JournalStore()).build_frontier("p1") == []


def test_cross_proof_research_move_is_not_dispatched():
    view = MemoryView()
    view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
    view.add_node("p2/rm-1", "ResearchMove", {"status": "queued"})
    view.add_edge("PROPOSES", "p1/rs-1", "p2/rm-1", "p1/e-1")
    assert GlobalScheduler(view, JournalStore()).build_frontier("p1") == []
