"""Hardening tests under genuine concurrency (scheduler prerequisite 1.1/1.3).

Every test runs real threads against separate `JournalStore` connections on one
database file -- the shape contention takes in production. Sequential calls
cannot prove these properties: `BEGIN IMMEDIATE` serialises writers only when
writers actually overlap.
"""

import os
import tempfile
import threading
import unittest

from commit_gate.canon import GENESIS_HASH
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import ConcurrencyError, JournalStore

THREADS = 8


def payload(**overrides):
    """A minimal well-formed event payload, as `Proposal.to_dict` produces."""
    base = {"proof_id": "p1", "actor": "test", "worker_class": "test"}
    base.update(overrides)
    return base


def proposal(node_serial: int, **overrides) -> Proposal:
    """A structural proposal creating one uniquely-named node."""
    kwargs = dict(
        proof_id="p1",
        actor=f"worker-{node_serial}",
        worker_class="formal-atp",
        ops=(
            UpsertNode(
                "FormalState",
                f"p1/fs-{node_serial}",
                {"goal_text": f"goal {node_serial}", "status": "open"},
            ),
        ),
        base_revision=0,
    )
    kwargs.update(overrides)
    return Proposal(**kwargs)


class ContentionCase(unittest.TestCase):
    """One database file, one connection per thread."""

    THREADS = THREADS

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "journal.db")

    def store(self) -> JournalStore:
        return JournalStore(self.path)

    def run_threads(self, target) -> list:
        """Run `target(index)` in `self.THREADS` threads from a barrier."""
        barrier = threading.Barrier(self.THREADS)
        results: list = [None] * self.THREADS
        errors: list[BaseException] = [None] * self.THREADS

        def worker(index: int) -> None:
            try:
                barrier.wait()
                results[index] = target(index)
            except BaseException as exc:  # noqa: BLE001 - reported per-thread
                errors[index] = exc

        threads = [
            threading.Thread(target=worker, args=(i,), name=f"w{i}")
            for i in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for index, exc in enumerate(errors):
            if exc is not None:
                raise AssertionError(f"thread {index} raised") from exc
        return results


class TestContendedAppends(ContentionCase):
    def test_concurrent_appends_produce_one_winner_per_revision(self):
        """N threads racing at base_revision=0: exactly one append lands."""

        def attempt(_index: int):
            store = self.store()
            try:
                return ("ok", store.append(payload(base_revision=0)))
            except ConcurrencyError as exc:
                return ("lost", exc)

        results = self.run_threads(attempt)
        winners = [r for r in results if r[0] == "ok"]
        losers = [r for r in results if r[0] == "lost"]

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), self.THREADS - 1)
        for _, exc in losers:
            self.assertIn(
                exc.reason,
                {Reason.STALE_BASE_REVISION, Reason.JOURNAL_BUSY},
            )

        store = self.store()
        self.assertEqual(store.head("p1")[0], 1)
        self.assertEqual(store.verify_chain("p1"), 1)

    def test_contending_committers_all_make_progress(self):
        """Retrying on a lost race converges: no lost updates, no gaps.

        Each thread commits several events by reading the head and appending;
        contention forces retries. Every attempt eventually succeeds, and the
        chain ends contiguous.
        """

        def work(index: int):
            store = self.store()
            committed = []
            for round_index in range(4):
                while True:
                    revision, _ = store.head("p1")
                    try:
                        committed.append(
                            store.append(
                                payload(
                                    actor=f"worker-{index}",
                                    base_revision=revision,
                                )
                            )
                        )
                        break
                    except ConcurrencyError as exc:
                        if exc.reason not in (
                            Reason.STALE_BASE_REVISION,
                            Reason.JOURNAL_BUSY,
                        ):
                            raise

            return committed

        results = self.run_threads(work)
        total = sum(len(committed) for committed in results)
        self.assertEqual(total, self.THREADS * 4)

        store = self.store()
        self.assertEqual(store.head("p1")[0], total)
        self.assertEqual(store.verify_chain("p1"), total)
        revisions = [row[0] for row in store.read_chain("p1")]
        self.assertEqual(revisions, list(range(1, total + 1)))

    def test_the_empty_head_race_starts_from_genesis(self):
        """Two fresh stores agree the journal is empty before anyone writes."""
        first, second = self.store(), self.store()
        self.assertEqual(first.head("p1"), second.head("p1"))
        self.assertEqual(first.head("p1"), (0, GENESIS_HASH))


class TestContendedLeases(ContentionCase):
    def test_fencing_tokens_are_unique_and_strictly_increasing(self):
        """Simultaneous acquires never share or reuse a fencing token."""

        def acquire(index: int):
            store = self.store()
            token = store.acquire_lease("p1", f"lease-{index}")
            return token

        tokens = sorted(self.run_threads(acquire))

        self.assertEqual(len(set(tokens)), self.THREADS)
        self.assertEqual(tokens, list(range(1, self.THREADS + 1)))
        # The highest token ever issued is never reissued or forgotten.
        row = self.store()._conn.execute(
            "SELECT MAX(fencing_token) AS top FROM leases WHERE proof_id = 'p1'"
        ).fetchone()
        self.assertEqual(row["top"], max(tokens))

    def test_superseded_token_of_the_same_lease_cannot_commit_and_is_journalled(self):
        """Invariant 10: the stale write is refused AND stays auditable."""
        writer = self.store()
        auditor = self.store()
        old_token = writer.acquire_lease("p1", "lease-a")
        new_token = writer.acquire_lease("p1", "lease-a")
        self.assertGreater(new_token, old_token)

        gate_view = MemoryView()
        gate = CommitGate(gate_view, writer)
        stale = Proposal(
            proof_id="p1",
            actor="worker",
            worker_class="formal-atp",
            base_revision=0,
            lease_id="lease-a",
            fencing_token=old_token,
        )
        result = gate.commit(stale)

        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections],
            [Reason.FENCING_TOKEN_SUPERSEDED],
        )
        rejections = auditor.read_rejections("p1")
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], str(Reason.FENCING_TOKEN_SUPERSEDED))
        self.assertEqual(auditor.read_events("p1"), [])


class TestContendedGate(ContentionCase):
    def test_simultaneous_gate_commits_accept_exactly_one_proposal(self):
        """The full validate-then-append path holds up under a race."""
        views = {}

        def attempt(index: int):
            view = MemoryView()
            views[index] = view
            store = self.store()
            gate = CommitGate(view, store)
            return gate.commit(proposal(index))

        results = self.run_threads(attempt)
        accepted = [r for r in results if r.accepted]
        rejected = [r for r in results if not r.accepted]

        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), self.THREADS - 1)
        for r in rejected:
            self.assertTrue(r.rejections)
            self.assertIsNone(r.event_hash)

        store = self.store()
        self.assertEqual(store.verify_chain("p1"), len(accepted))
        journalled_rejections = store.read_rejections("p1")
        self.assertGreaterEqual(len(journalled_rejections), len(rejected))


if __name__ == "__main__":
    unittest.main()
