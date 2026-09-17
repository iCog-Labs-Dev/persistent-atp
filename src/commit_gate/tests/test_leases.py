"""The v2 lease subsystem: per-move dispatch rows with TTL and audit trail.

Leases are scheduling state beside the journal -- never hash-chain events --
so issuing one does not advance a proof's revision. These tests pin what the
scheduler relies on: concurrent dispatch of distinct moves, per-move mutual
exclusion, expiry killing a token forever, and every transition audited.
"""

import os
import tempfile
import unittest

from commit_gate.ops import SetField
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.gate import CommitGate
from commit_gate.store import JournalStore


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def claim_status_proposal(claim_id: str, **overrides) -> Proposal:
    kwargs = dict(
        proof_id="p1",
        actor="worker",
        worker_class="critic",
        ops=(SetField("Claim", claim_id, "status", "critic-accepted"),),
        base_revision=0,
    )
    kwargs.update(overrides)
    return Proposal(**kwargs)


class TestDispatchLeases(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "journal.db")
        self.clock = FakeClock()
        self.store = JournalStore(self.path, clock=self.clock)

    def issue(self, lease_id: str, move: str | None, worker_class="critic", ttl=600.0):
        return self.store.issue_lease(
            "p1",
            lease_id,
            worker_class=worker_class,
            selected_move_id=move,
            ttl_seconds=ttl,
        )

    def test_issue_returns_a_live_row_bound_to_the_move(self):
        lease = self.issue("ls-1", "p1/rm-1")
        self.assertEqual(lease.status, "active")
        self.assertEqual(lease.fencing_token, 1)
        self.assertEqual(lease.selected_move_id, "p1/rm-1")
        self.assertEqual(lease.base_revision, 0)
        self.assertEqual(lease.expires_at, self.clock.now + 600.0)

    def test_distinct_moves_hold_concurrent_leases_and_both_commit(self):
        """The capability this whole redesign exists for."""
        first = self.issue("ls-1", "p1/rm-1")
        second = self.issue("ls-2", "p1/rm-2")

        gate = CommitGate(MemoryView(), self.store)
        view = MemoryView()
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        view.add_node("p1/c-2", "Claim", {"status": "provisional"})
        gate_view = gate._view
        gate_view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        gate_view.add_node("p1/c-2", "Claim", {"status": "provisional"})

        result_one = gate.commit(
            Proposal(
                proof_id="p1",
                actor="critic-a",
                worker_class="critic",
                ops=(
                    SetField(
                        "Claim", "p1/c-1", "status", "refuted", prior="provisional"
                    ),
                ),
                base_revision=first.base_revision,
                lease_id=first.lease_id,
                fencing_token=first.fencing_token,
            )
        )
        result_two = gate.commit(
            Proposal(
                proof_id="p1",
                actor="critic-b",
                worker_class="critic",
                ops=(
                    SetField(
                        "Claim", "p1/c-2", "status", "tainted", prior="provisional"
                    ),
                ),
                base_revision=result_one.revision,
                lease_id=second.lease_id,
                fencing_token=second.fencing_token,
            )
        )

        self.assertTrue(result_one.accepted, result_one.rejections)
        self.assertTrue(result_two.accepted, result_two.rejections)

    def test_reissuing_a_move_supersedes_the_old_lease(self):
        stale = self.issue("ls-1", "p1/rm-1")
        fresh = self.issue("ls-2", "p1/rm-1")
        self.assertGreater(fresh.fencing_token, stale.fencing_token)

        active_ids = {row.lease_id for row in self.store.active_leases("p1")}
        self.assertEqual(active_ids, {"ls-2"})
        self.assertIn(
            "superseded",
            [e["event"] for e in self.store.read_lease_events("p1")],
        )

    def test_expired_ttl_kills_the_token_even_for_its_own_holder(self):
        from commit_gate.store import ConcurrencyError

        lease = self.issue("ls-1", "p1/rm-1", ttl=60.0)
        self.clock.advance(61.0)

        with self.assertRaises(ConcurrencyError) as caught:
            self.store.append(
                {
                    "proof_id": "p1",
                    "actor": "critic",
                    "worker_class": "critic",
                    "base_revision": 0,
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                    "ops": [],
                }
            )
        self.assertEqual(caught.exception.reason, Reason.LEASE_NOT_HELD)
        self.assertIn(
            "expired", [e["event"] for e in self.store.read_lease_events("p1")]
        )

    def test_released_lease_cannot_commit(self):
        lease = self.issue("ls-1", "p1/rm-1")
        self.assertTrue(self.store.release_lease("p1", "ls-1"))
        gate = CommitGate(self._seeded_view(), self.store)
        result = gate.commit(claim_status_proposal("p1/c-1"))
        self.assertFalse(result.accepted)
        del lease

    def test_the_exclusive_write_lock_supersedes_dispatch_leases(self):
        dispatch = self.issue("ls-1", "p1/rm-1")
        lock = self.store.acquire_lease("p1", "maintenance-lock")
        self.assertGreater(lock, dispatch.fencing_token)

        live = {row.lease_id for row in self.store.active_leases("p1")}
        self.assertEqual(live, {"maintenance-lock"})

    def test_tokens_never_repeat_across_mixed_acquisitions(self):
        tokens = [
            self.issue("ls-1", "p1/rm-1").fencing_token,
            self.issue("ls-2", "p1/rm-2").fencing_token,
            self.store.acquire_lease("p1", "lock"),
            self.issue("ls-3", "p1/rm-3").fencing_token,
        ]
        self.assertEqual(sorted(tokens), [1, 2, 3, 4])

    def test_score_snapshot_is_journalled_on_the_issued_event(self):
        self.store.issue_lease(
            "p1",
            "ls-1",
            worker_class="llm-research",
            selected_move_id="p1/rm-1",
            score_snapshot={"expected_information_gain": 0.8},
        )
        events = self.store.read_lease_events("p1")
        self.assertEqual(events[0]["event"], "issued")
        self.assertEqual(
            events[0]["score_snapshot"], {"expected_information_gain": 0.8}
        )

    def test_active_leases_lapses_due_rows_on_read(self):
        self.issue("ls-1", "p1/rm-1", ttl=10.0)
        self.issue("ls-2", "p1/rm-2", ttl=100.0)
        self.clock.advance(50.0)
        live = {row.lease_id for row in self.store.active_leases("p1")}
        self.assertEqual(live, {"ls-2"})

    def _seeded_view(self) -> MemoryView:
        view = MemoryView()
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        return view


if __name__ == "__main__":
    unittest.main()
