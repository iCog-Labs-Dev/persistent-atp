import unittest

from commit_gate.canon import GENESIS_HASH
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore


class TestCommitGate(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)

    @staticmethod
    def _valid(node_id="p1/fs1", **overrides):
        # `base_revision` is mandatory (`check_concurrency_tokens`); default to
        # the empty journal's head so single-commit tests need not name it.
        overrides.setdefault("base_revision", 0)
        return Proposal(
            proof_id="p1",
            actor="test",
            worker_class="test",
            ops=(UpsertNode("FormalState", node_id, {"status": "open"}),),
            **overrides,
        )

    @staticmethod
    def _invalid():
        # Missing required field `subgoal_count` on TacticApplication.
        return Proposal(
            proof_id="p1",
            actor="test",
            worker_class="test",
            ops=(UpsertNode("TacticApplication", "p1/ta1", {"executor_result": "lean-accepted"}),),
            base_revision=0,
        )

    def test_gate_accepts_valid_proposal(self):
        result = self.gate.commit(self._valid())
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.event_hash)
        self.assertEqual(result.revision, 1)

    def test_gate_rejects_invalid_proposal(self):
        result = self.gate.commit(self._invalid())
        self.assertFalse(result.accepted)
        self.assertIsNone(result.event_hash)
        self.assertIsNone(result.revision)
        self.assertGreater(len(result.rejections), 0)

    def test_gate_journals_what_it_accepts(self):
        """No caller step is required: accepting is writing."""
        result = self.gate.commit(self._valid())

        self.assertEqual(self.store.head("p1"), (1, result.event_hash))
        events = self.store.read_events("p1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ops"][0]["id"], "p1/fs1")

    def test_rejected_proposal_writes_nothing(self):
        self.gate.commit(self._invalid())

        self.assertEqual(self.store.head("p1"), (0, GENESIS_HASH))
        self.assertEqual(self.store.read_events("p1"), [])

    def test_consecutive_commits_chain(self):
        first = self.gate.commit(self._valid("p1/fs1"))
        second = self.gate.commit(self._valid("p1/fs2", base_revision=1))

        self.assertEqual(
            self.store.read_chain("p1"),
            [
                (1, first.event_hash, GENESIS_HASH),
                (2, second.event_hash, first.event_hash),
            ],
        )

    def test_stale_base_revision_is_a_rejection_not_an_exception(self):
        self.gate.commit(self._valid("p1/fs1"))

        result = self.gate.commit(self._valid("p1/fs2", base_revision=0))

        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections], [Reason.STALE_BASE_REVISION]
        )
        self.assertEqual(self.store.head("p1")[0], 1)

    def test_fresh_base_revision_is_accepted(self):
        self.gate.commit(self._valid("p1/fs1"))

        result = self.gate.commit(self._valid("p1/fs2", base_revision=1))

        self.assertTrue(result.accepted)
        self.assertEqual(result.revision, 2)

    def test_a_proposal_with_no_concurrency_tokens_is_refused(self):
        """The gate never falls back to an unchecked write."""
        result = self.gate.commit(self._valid(base_revision=None))

        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections], [Reason.MISSING_CONCURRENCY_TOKEN]
        )
        self.assertEqual(self.store.head("p1"), (0, GENESIS_HASH))

    def test_the_gate_leaves_a_verifiable_chain(self):
        self.gate.commit(self._valid("p1/fs1"))
        self.gate.commit(self._valid("p1/fs2", base_revision=1))

        self.assertEqual(self.store.verify_chain("p1"), 2)


if __name__ == "__main__":
    unittest.main()


class TestRejectionJournal(unittest.TestCase):
    """A refused write is not merged, and not forgotten either."""

    def setUp(self):
        self.view = MemoryView()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)

    def _proposal(self, token, lease="lease-a", node_id="p1/fs1", base=0):
        return Proposal(
            proof_id="p1",
            actor="worker-1",
            worker_class="test",
            ops=(UpsertNode("FormalState", node_id, {"status": "open"}),),
            base_revision=base,
            lease_id=lease,
            fencing_token=token,
        )

    def _invalid(self):
        return Proposal(
            proof_id="p1",
            actor="test",
            worker_class="test",
            ops=(UpsertNode("TacticApplication", "p1/ta1", {"executor_result": "lean-accepted"}),),
            base_revision=0,
        )

    def test_superseded_fencing_token_is_rejected_not_merged(self):
        token_a = self.store.acquire_lease("p1", "lease-a")
        first = self.gate.commit(self._proposal(token_a))
        self.assertTrue(first.accepted)

        self.store.acquire_lease("p1", "lease-a")

        stale = self._proposal(token_a, node_id="p1/fs2", base=1)
        result = self.gate.commit(stale)
        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections],
            [Reason.FENCING_TOKEN_SUPERSEDED],
        )
        self.assertEqual(len(self.store.read_events("p1")), 1)
        self.assertEqual(self.store.head("p1"), (1, first.event_hash))

    def test_rejection_lands_on_the_append_only_trail(self):
        result = self.gate.commit(self._invalid())
        self.assertFalse(result.accepted)

        trail = self.store.read_rejections("p1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["payload"]["actor"], "test")
        self.assertEqual(trail[0]["reason"], "missing-required-field")

        stale = Proposal(
            proof_id="p1",
            actor="worker-2",
            worker_class="test",
            ops=(UpsertNode("FormalState", "p1/fs9", {"status": "open"}),),
            base_revision=0,
            lease_id="nope",
            fencing_token=99,
        )
        self.gate.commit(stale)
        trail_after = self.store.read_rejections("p1")
        self.assertEqual(len(trail_after), 2)
        self.assertEqual(trail_after[0], trail[0])

    def test_accepted_commits_write_no_rejection_rows(self):
        token = self.store.acquire_lease("p1", "lease-a")
        self.gate.commit(self._proposal(token))
        self.assertEqual(self.store.read_rejections("p1"), [])
        self.assertEqual(self.store.verify_chain("p1"), 1)


if __name__ == "__main__":
    unittest.main()
