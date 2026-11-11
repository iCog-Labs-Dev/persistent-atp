"""The v2 lease subsystem: per-move dispatch rows with TTL and audit trail.

Leases are scheduling state beside the journal -- never hash-chain events --
so issuing one does not advance a proof's revision. These tests pin what the
scheduler relies on: concurrent dispatch of distinct moves, per-move mutual
exclusion, expiry killing a token forever, and every transition audited.
"""

import os
import sqlite3
import tempfile
import unittest

from commit_gate.canon import GENESIS_HASH, canonical_json, chain_hash
from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.gate import CommitGate
from commit_gate.store import ConcurrencyError, JournalStore


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
        ops=(SetField("Claim", claim_id, "status", "refuted", prior="provisional"),),
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
        self.store = JournalStore(self.path, clock_ns=lambda: int(self.clock() * 1_000_000_000))

    def issue(self, lease_id: str, move: str, worker_class="critic", ttl=600.0, actor="worker"):
        return self.store.issue_lease(
            "p1",
            lease_id,
            actor=actor,
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
        first = self.issue("ls-1", "p1/rm-1", actor="critic-a")
        second = self.issue("ls-2", "p1/rm-2", actor="critic-b")

        view = MemoryView()
        view.add_node("p1/c-1", "Claim", {"status": "provisional"})
        view.add_node("p1/c-2", "Claim", {"status": "provisional"})
        gate = CommitGate(view, self.store)

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
                selected_move_id=first.selected_move_id,
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
                selected_move_id=second.selected_move_id,
            )
        )

        self.assertTrue(result_one.accepted, result_one.rejections)
        self.assertTrue(result_two.accepted, result_two.rejections)
        self.assertEqual(self.store.verify_chain("p1"), 2)

    def test_dispatch_proposal_round_trips_its_move(self):
        lease = self.issue("ls-1", "p1/rm-1")
        proposal = self.proposal(lease)
        self.assertEqual(Proposal.from_dict(proposal.to_dict()), proposal)

    def test_dispatch_token_is_bound_to_actor_class_and_move(self):
        lease = self.issue("ls-1", "p1/rm-1")
        gate = CommitGate(self._seeded_view(), self.store)
        cases = (
            ({"actor": "another-worker"}, Reason.LEASE_IDENTITY_MISMATCH),
            ({"worker_class": "coordinator"}, Reason.LEASE_IDENTITY_MISMATCH),
            ({"selected_move_id": None}, Reason.LEASE_MOVE_MISMATCH),
            ({"selected_move_id": "p1/rm-2"}, Reason.LEASE_MOVE_MISMATCH),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                result = gate.commit(self.proposal(lease, **overrides))
                self.assertFalse(result.accepted)
                self.assertEqual(result.rejections[0].reason, expected)
                self.assertEqual(self.store.head("p1"), (0, GENESIS_HASH))
        self.assertTrue(gate.commit(self.proposal(lease)).accepted)
        self.assertEqual(self.store.read_events("p1")[0]["selected_move_id"], "p1/rm-1")

    def test_live_lease_does_not_bypass_revision_check(self):
        first = self.issue("ls-1", "p1/rm-1")
        second = self.issue("ls-2", "p1/rm-2")
        gate = CommitGate(self._seeded_view(), self.store)
        self.assertTrue(gate.commit(self.proposal(first)).accepted)
        result = gate.commit(self.proposal(second))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejections[0].reason, Reason.STALE_BASE_REVISION)
        self.assertTrue(second.is_live(self.clock()))
        self.assertEqual(self.store.verify_chain("p1"), 1)

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
        gate = CommitGate(self._seeded_view(), self.store)
        self.assertFalse(gate.commit(self.proposal(stale)).accepted)
        self.assertTrue(gate.commit(self.proposal(fresh)).accepted)

    def test_late_release_cannot_cancel_a_renewed_lease(self):
        old = self.issue("ls-1", "p1/rm-1")
        renewed = self.issue("ls-1", "p1/rm-1")
        self.assertFalse(self.store.release_lease(
            "p1", old.lease_id, fencing_token=old.fencing_token
        ))
        gate = CommitGate(self._seeded_view(), self.store)
        self.assertTrue(gate.commit(self.proposal(renewed)).accepted)
        with self.assertRaises(ValueError):
            self.store.release_lease("p1", renewed.lease_id, fencing_token=True)

    def test_supersession_is_audited_once_with_the_previous_holder(self):
        leases = [self.issue(f"ls-{i}", "p1/rm-1", actor=f"worker-{i}") for i in range(3)]
        events = self.store.read_lease_events("p1")
        superseded = [event for event in events if event["event"] == "superseded"]
        self.assertEqual(len(superseded), 2)
        self.assertEqual(
            [(event["actor"], event["fencing_token"], event["selected_move_id"])
             for event in superseded],
            [(lease.actor, lease.fencing_token, lease.selected_move_id) for lease in leases[:2]],
        )
        self.assertEqual(self.store.head("p1"), (0, GENESIS_HASH))

    def test_invalid_dispatch_inputs_leave_no_lease_or_audit(self):
        for ttl in (0, -1, True, float("nan"), float("inf"), 86_401):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.issue("ls-1", "p1/rm-1", ttl=ttl)
        for move in (None, "", "p2/rm-1"):
            with self.subTest(move=move), self.assertRaises(ValueError):
                self.issue("ls-1", move)
        with self.assertRaises(ValueError):
            self.issue("ls-1", "p1/rm-1", actor="")
        with self.assertRaises(ValueError):
            self.issue("ls-1", "p1/rm-1", worker_class="unregistered")
        self.assertEqual(self.store.active_leases("p1"), [])
        self.assertEqual(self.store.read_lease_events("p1"), [])

    def test_dispatch_rejects_an_old_requested_revision(self):
        with self.assertRaises(ConcurrencyError) as caught:
            self.store.issue_lease(
                "p1", "ls-1", actor="worker", worker_class="critic",
                selected_move_id="p1/rm-1", base_revision=9,
            )
        self.assertEqual(caught.exception.reason, Reason.STALE_BASE_REVISION)
        self.assertEqual(self.store.active_leases("p1"), [])

    def test_expired_ttl_kills_the_token_even_for_its_own_holder(self):
        lease = self.issue("ls-1", "p1/rm-1", ttl=60.0)
        self.clock.advance(61.0)

        with self.assertRaises(ConcurrencyError) as caught:
            self.store.append(
                {
                    "proof_id": "p1",
                    "actor": "worker",
                    "worker_class": "critic",
                    "base_revision": 0,
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                    "selected_move_id": lease.selected_move_id,
                    "ops": [UpsertNode("Attempt", "p1/at-1", {}).to_dict()],
                }
            )
        self.assertEqual(caught.exception.reason, Reason.LEASE_EXPIRED)
        self.assertIn(
            "expired", [e["event"] for e in self.store.read_lease_events("p1")]
        )
        self.store.active_leases("p1")
        self.store.active_leases("p1")
        self.assertEqual(
            [event["event"] for event in self.store.read_lease_events("p1")],
            ["issued", "expired"],
        )
        self.assertEqual(self.store.head("p1"), (0, GENESIS_HASH))

    def test_released_lease_cannot_commit(self):
        lease = self.issue("ls-1", "p1/rm-1")
        self.assertTrue(self.store.release_lease("p1", "ls-1", fencing_token=lease.fencing_token))
        gate = CommitGate(self._seeded_view(), self.store)
        result = gate.commit(claim_status_proposal(
            "p1/c-1", lease_id=lease.lease_id, fencing_token=lease.fencing_token,
            selected_move_id=lease.selected_move_id,
        ))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejections[0].reason, Reason.LEASE_NOT_HELD)

    def test_the_exclusive_write_lock_supersedes_dispatch_leases(self):
        dispatch = self.issue("ls-1", "p1/rm-1")
        lock = self.store.acquire_lease(
            "p1", "maintenance-lock", actor="maintenance", worker_class="maintenance"
        )
        self.assertGreater(lock, dispatch.fencing_token)

        live = {row.lease_id for row in self.store.active_leases("p1")}
        self.assertEqual(live, {"maintenance-lock"})
        with self.assertRaises(ConcurrencyError):
            self.issue("ls-2", "p1/rm-2")
        self.assertEqual(len(self.store.active_leases("p1")), 1)

    def test_tokens_never_repeat_across_mixed_acquisitions(self):
        tokens = [
            self.issue("ls-1", "p1/rm-1").fencing_token,
            self.issue("ls-2", "p1/rm-2").fencing_token,
        ]
        tokens.append(self.store.acquire_lease(
            "p1", "lock", actor="maintenance", worker_class="maintenance"
        ))
        self.store.release_lease("p1", "lock", fencing_token=tokens[-1])
        tokens.append(self.issue("ls-3", "p1/rm-3").fencing_token)
        self.assertEqual(sorted(tokens), [1, 2, 3, 4])

    def test_score_snapshot_is_journalled_on_the_issued_event(self):
        self.store.issue_lease(
            "p1",
            "ls-1",
            actor="researcher",
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

    @staticmethod
    def proposal(lease, **overrides):
        fields = dict(
            actor=lease.actor, worker_class=lease.worker_class,
            lease_id=lease.lease_id, fencing_token=lease.fencing_token,
            base_revision=lease.base_revision, selected_move_id=lease.selected_move_id,
        )
        fields.update(overrides)
        return claim_status_proposal("p1/c-1", **fields)


class TestLeaseMigration(unittest.TestCase):
    def test_failed_migration_rolls_back_without_losing_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "journal.db")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE leases (proof_id TEXT PRIMARY KEY, lease_id TEXT, fencing_token INTEGER)")
                conn.execute("INSERT INTO leases VALUES ('p1', 'corrupt', NULL)")
            with self.assertRaises(sqlite3.IntegrityError):
                JournalStore(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(
                    conn.execute("SELECT * FROM leases").fetchall(), [("p1", "corrupt", None)]
                )
                self.assertNotIn("status", [row[1] for row in conn.execute("PRAGMA table_info(leases)")])

    def test_upgrade_preserves_holder_token_journal_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "journal.db")
            event = {"proof_id": "p1", "actor": "worker", "worker_class": "coordinator",
                     "ops": [UpsertNode("Claim", "p1/c-1", {}).to_dict()]}
            event_hash = chain_hash(GENESIS_HASH, event)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE leases (proof_id TEXT PRIMARY KEY, lease_id TEXT, "
                    "fencing_token INTEGER, actor TEXT, worker_class TEXT, expires_at_ns INTEGER)"
                )
                conn.execute("INSERT INTO leases VALUES ('p1', 'old', 7, 'worker', 'coordinator', 2000000000000)")
                conn.execute(
                    "CREATE TABLE lease_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, proof_id TEXT, "
                    "lease_id TEXT, fencing_token INTEGER, actor TEXT, worker_class TEXT, "
                    "event_type TEXT, occurred_at_ns INTEGER, expires_at_ns INTEGER)"
                )
                conn.execute(
                    "INSERT INTO lease_audit VALUES (1, 'p1', 'old', 7, 'worker', 'coordinator', "
                    "'acquired', 1000000000000, 2000000000000)"
                )
                conn.execute(
                    "CREATE TABLE journal (proof_id TEXT, revision INTEGER, event_hash TEXT, "
                    "prev_hash TEXT, actor TEXT, worker_class TEXT, payload TEXT, "
                    "committed_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (proof_id, revision))"
                )
                conn.execute(
                    "INSERT INTO journal (proof_id, revision, event_hash, prev_hash, actor, worker_class, payload) "
                    "VALUES ('p1', 1, ?, ?, 'worker', 'coordinator', ?)",
                    (event_hash, GENESIS_HASH, canonical_json(event).decode()),
                )
            store = JournalStore(path, clock_ns=lambda: 1000000000000)
            self.assertEqual(store.head("p1"), (1, event_hash))
            self.assertEqual(store.verify_chain("p1"), 1)
            self.assertEqual(store.active_leases("p1")[0].actor, "worker")
            self.assertEqual(store.read_lease_events("p1")[0]["fencing_token"], 7)
            self.assertTrue(store.release_lease("p1", "old", fencing_token=7))
            fresh = store.issue_lease(
                "p1", "new", actor="worker", worker_class="coordinator", selected_move_id="p1/rm-1"
            )
            self.assertEqual(fresh.fencing_token, 8)
            reopened = JournalStore(path, clock_ns=lambda: 1000000000000)
            self.assertEqual(reopened.head("p1"), (1, event_hash))
            self.assertEqual(reopened.active_leases("p1")[0], fresh)

    def test_unbound_legacy_token_is_retained_but_never_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "journal.db")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE leases (proof_id TEXT PRIMARY KEY, lease_id TEXT, fencing_token INTEGER)")
                conn.execute("INSERT INTO leases VALUES ('p1', 'old', 21)")
            store = JournalStore(path)
            self.assertEqual(store.active_leases("p1"), [])
            self.assertEqual(store.acquire_lease(
                "p1", "new", actor="worker", worker_class="coordinator"
            ), 22)


if __name__ == "__main__":
    unittest.main()
