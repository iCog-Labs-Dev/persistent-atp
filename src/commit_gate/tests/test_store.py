import os
import sqlite3
import tempfile
import unittest

from commit_gate.canon import GENESIS_HASH, canonical_json
from commit_gate.reasons import Reason
from commit_gate.store import ConcurrencyError, HashChainError, JournalStore


def payload(**overrides):
    """A minimal well-formed event payload, as `Proposal.to_dict` produces."""
    base = {"proof_id": "p1", "actor": "test", "worker_class": "test"}
    base.update(overrides)
    return base


class TestJournalStore(unittest.TestCase):
    def test_head_on_empty_journal(self):
        store = JournalStore()
        self.assertEqual(store.head("p1"), (0, GENESIS_HASH))

    def test_append_and_read(self):
        store = JournalStore()
        revision, event_hash = store.append(payload())

        self.assertEqual(revision, 1)
        self.assertEqual(store.head("p1"), (1, event_hash))

        events = store.read_events("p1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "test")

    def test_append_chains_onto_the_head(self):
        store = JournalStore()
        _, first = store.append(payload())
        _, second = store.append(payload())

        self.assertEqual(
            store.read_chain("p1"),
            [(1, first, GENESIS_HASH), (2, second, first)],
        )
    def test_read_events_since_returns_only_the_later_revisions(self):     
        store = JournalStore()
        store.append(payload(actor="first"))
        store.append(payload(actor="second"))
        store.append(payload(actor="third"))

        events = store.read_events_since("p1", after_revision=1)

        self.assertEqual([rev for rev, _ in events], [2, 3])
        self.assertEqual([p["actor"] for _, p in events], ["second", "third"])

    def test_read_events_since_zero_returns_everything(self):
        store = JournalStore()
        store.append(payload())
        store.append(payload())

        events = store.read_events_since("p1", after_revision=0)

        self.assertEqual(len(events), 2)

    def test_read_events_since_the_current_head_returns_nothing(self):
        store = JournalStore()
        revision, _ = store.append(payload())

        self.assertEqual(store.read_events_since("p1", after_revision=revision), [])

    def test_read_events_since_is_scoped_to_the_proof(self):
        store = JournalStore()
        store.append(payload(proof_id="p1"))
        store.append(payload(proof_id="p2"))

        events = store.read_events_since("p1", after_revision=0)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["proof_id"], "p1")
              
    def test_revisions_are_numbered_per_proof(self):
        store = JournalStore()
        store.append(payload(proof_id="p1"))
        revision, _ = store.append(payload(proof_id="p2"))
        self.assertEqual(revision, 1)

    def test_base_revision_mismatch(self):
        store = JournalStore()
        with self.assertRaises(ConcurrencyError) as caught:
            store.append(payload(base_revision=99))
        self.assertEqual(caught.exception.reason, Reason.STALE_BASE_REVISION)

    def test_failed_append_leaves_the_journal_untouched(self):
        store = JournalStore()
        _, first = store.append(payload())

        with self.assertRaises(ConcurrencyError):
            store.append(payload(base_revision=99))

        self.assertEqual(store.head("p1"), (1, first))
        self.assertEqual(len(store.read_events("p1")), 1)

    def test_lease_fencing(self):
        store = JournalStore()
        token = store.acquire_lease("p1", "lease1")

        store.append(payload(lease_id="lease1", fencing_token=token))

        with self.assertRaises(ConcurrencyError) as caught:
            store.append(payload(lease_id="lease1", fencing_token=0))
        self.assertEqual(caught.exception.reason, Reason.FENCING_TOKEN_SUPERSEDED)

    def test_reacquiring_the_lease_locks_out_the_old_holder(self):
        store = JournalStore()
        old = store.acquire_lease("p1", "lease1")
        new = store.acquire_lease("p1", "lease2")
        self.assertGreater(new, old)

        with self.assertRaises(ConcurrencyError) as caught:
            store.append(payload(lease_id="lease1", fencing_token=old))
        self.assertEqual(caught.exception.reason, Reason.LEASE_NOT_HELD)

    def test_lease_claimed_but_never_acquired(self):
        store = JournalStore()
        with self.assertRaises(ConcurrencyError) as caught:
            store.append(payload(lease_id="lease1", fencing_token=1))
        self.assertEqual(caught.exception.reason, Reason.LEASE_NOT_HELD)


class TestHashChain(unittest.TestCase):
    """`verify_chain` recomputes what the journal claims about itself."""

    @staticmethod
    def _tamper(store: JournalStore, revision: int, column: str, value: str) -> None:
        """Edit a committed row behind the store's back, as corruption would."""
        store._conn.execute(
            f"UPDATE journal SET {column} = ? WHERE proof_id = 'p1' AND revision = ?",
            (value, revision),
        )

    def test_empty_journal_verifies(self):
        self.assertEqual(JournalStore().verify_chain("p1"), 0)

    def test_intact_journal_verifies(self):
        store = JournalStore()
        store.append(payload())
        store.append(payload())
        self.assertEqual(store.verify_chain("p1"), 2)

    def test_edited_payload_is_caught(self):
        store = JournalStore()
        store.append(payload())
        store.append(payload())
        self._tamper(
            store, 1, "payload", canonical_json(payload(actor="attacker")).decode("utf-8")
        )

        with self.assertRaisesRegex(HashChainError, "chains to"):
            store.verify_chain("p1")

    def test_broken_link_is_caught(self):
        store = JournalStore()
        store.append(payload())
        store.append(payload())
        self._tamper(store, 2, "prev_hash", GENESIS_HASH)

        with self.assertRaisesRegex(HashChainError, "follows"):
            store.verify_chain("p1")

    def test_append_refuses_to_chain_onto_a_corrupt_head(self):
        """A bad hash must not get a good link built on top of it."""
        store = JournalStore()
        store.append(payload())
        self._tamper(
            store, 1, "payload", canonical_json(payload(actor="attacker")).decode("utf-8")
        )

        with self.assertRaises(HashChainError):
            store.append(payload())

        self.assertEqual(len(store.read_events("p1")), 1)


class TestConcurrentWriters(unittest.TestCase):
    """Two `JournalStore` connections on one database file."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "journal.db")

    def test_second_writer_at_the_same_base_revision_loses(self):
        first, second = JournalStore(self.path), JournalStore(self.path)

        first.append(payload(base_revision=0))
        with self.assertRaises(ConcurrencyError) as caught:
            second.append(payload(base_revision=0))

        self.assertEqual(caught.exception.reason, Reason.STALE_BASE_REVISION)
        self.assertEqual(second.head("p1")[0], 1)
        self.assertEqual(second.verify_chain("p1"), 1)

    def test_a_held_write_lock_is_reported_not_raised_raw(self):
        holder = JournalStore(self.path)
        waiter = JournalStore(self.path, busy_timeout_ms=1)

        holder._conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(ConcurrencyError) as caught:
                waiter.append(payload(base_revision=0))
        finally:
            holder._conn.execute("ROLLBACK")

        self.assertEqual(caught.exception.reason, Reason.JOURNAL_BUSY)
        self.assertEqual(waiter.head("p1"), (0, GENESIS_HASH))

    def test_file_journal_runs_in_wal_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(os.path.join(directory, "journal.db"))
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = store._conn.execute("PRAGMA synchronous").fetchone()[0]

        self.assertEqual(mode, "wal")
        self.assertEqual(synchronous, 2)  # FULL: a committed event is on disk.

    def test_a_reader_does_not_block_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "journal.db")
            writer = JournalStore(path)
            writer.append(payload())

            reader = JournalStore(path)
            reader._conn.execute("BEGIN")
            reader._conn.execute("SELECT * FROM journal").fetchall()
            try:
                # Under the default DELETE mode this raises JOURNAL_BUSY once
                # busy_timeout expires; under WAL it commits.
                revision, _ = writer.append(payload(base_revision=1))
            finally:
                reader._conn.execute("ROLLBACK")

        self.assertEqual(revision, 2)

    def test_a_commit_blocked_by_a_reader_is_a_rejection_not_a_crash(self):
        # Forced into DELETE mode to reproduce the contention WAL removes.
        # `BEGIN IMMEDIATE` succeeds there (RESERVED is compatible with the
        # reader's SHARED lock), so the busy lands on COMMIT instead.
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "journal.db")
            writer = JournalStore(path, busy_timeout_ms=100)
            writer._conn.execute("PRAGMA journal_mode=DELETE")
            writer.append(payload())

            reader = sqlite3.connect(path, isolation_level=None, timeout=0.2)
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM journal").fetchall()
            try:
                with self.assertRaises(ConcurrencyError) as caught:
                    writer.append(payload(base_revision=1))
            finally:
                reader.execute("ROLLBACK")
                reader.close()

        self.assertEqual(caught.exception.reason, Reason.JOURNAL_BUSY)


if __name__ == "__main__":
    unittest.main()
