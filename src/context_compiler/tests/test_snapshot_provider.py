import pytest

from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.errors import ContextValidationError
from context_compiler.snapshot import StaleSnapshotError
from context_compiler.snapshot_provider import LeaseSnapshotProvider
from mathproof.scheduler import GlobalScheduler


class FakeLease:
    lease_id = "p1/ls-2"
    proof_id = "p1"
    worker_class = "llm-research"
    fencing_token = 2
    base_revision = 1
    selected_move_id = "p1/rm-1"


def append_claim(store: JournalStore) -> None:
    row = store.issue_lease(
        "p1",
        "p1/ls-1",
        actor="seed",
        worker_class="coordinator",
        selected_move_id="p1/c-1",
    )
    store.append(
        Proposal(
            proof_id="p1",
            actor="seed",
            worker_class="coordinator",
            base_revision=0,
            lease_id=row.lease_id,
            fencing_token=row.fencing_token,
            selected_move_id=row.selected_move_id,
            ops=(
                UpsertNode(
                    "Claim",
                    "p1/c-1",
                    {"status": "conjectural", "statement": "P"},
                ),
            ),
        ).to_dict()
    )


def test_provider_overlays_unprojected_journal_events():
    view = MemoryView()
    store = JournalStore()
    append_claim(store)

    snapshot = LeaseSnapshotProvider(view, store).for_lease(FakeLease())

    assert snapshot.revision == 1
    assert snapshot.view.snapshot("p1")[0] == 1
    assert snapshot.view.node("p1/c-1").fields["statement"] == "P"
    assert view.node("p1/c-1") is None


def test_provider_accepts_a_real_scheduler_lease():
    view = MemoryView()
    view.add_node("p1/c-1", "Claim", {"status": "provisional"})
    store = JournalStore()
    lease = GlobalScheduler(view, store).lease_next(
        "p1", "critic", actor="critic-1"
    )

    snapshot = LeaseSnapshotProvider(view, store).for_lease(lease)

    assert snapshot.proof_id == lease.proof_id
    assert snapshot.revision == lease.base_revision == 0
    assert snapshot.view.node(lease.selected_move_id).label == "Claim"


def test_provider_uses_an_exact_projected_snapshot_without_an_overlay():
    view = MemoryView()
    store = JournalStore()
    append_claim(store)
    view.apply_journal_event(store, "p1", 1)

    snapshot = LeaseSnapshotProvider(view, store).for_lease(FakeLease())

    assert snapshot.revision == 1
    assert snapshot.view.node("p1/c-1") is not None


def test_returned_snapshot_is_immutable_with_respect_to_later_projection_changes():
    view = MemoryView()
    store = JournalStore()

    class RevisionZeroLease(FakeLease):
        base_revision = 0

    snapshot = LeaseSnapshotProvider(view, store).for_lease(RevisionZeroLease())
    view.add_node("p1/c-9", "Claim", {"status": "conjectural"})

    assert snapshot.view.node("p1/c-9") is None


def test_provider_rejects_a_lease_which_is_already_stale():
    view = MemoryView()
    store = JournalStore()
    append_claim(store)

    class StaleLease(FakeLease):
        base_revision = 0

    with pytest.raises(StaleSnapshotError, match="journal head"):
        LeaseSnapshotProvider(view, store).for_lease(StaleLease())


def test_provider_rejects_a_projection_ahead_of_the_lease():
    view = MemoryView()
    store = JournalStore()
    # Deliberately construct an impossible/corrupt projection checkpoint to
    # verify the provider fails closed instead of serving future state.
    view._revisions["p1"] = 1

    class RevisionZeroLease(FakeLease):
        base_revision = 0

    with pytest.raises(StaleSnapshotError, match="projection is at revision"):
        LeaseSnapshotProvider(view, store).for_lease(RevisionZeroLease())


def test_provider_rejects_invalid_lease_revision():
    view = MemoryView()
    store = JournalStore()

    class InvalidLease(FakeLease):
        base_revision = True

    with pytest.raises(ContextValidationError, match="base_revision"):
        LeaseSnapshotProvider(view, store).for_lease(InvalidLease())
