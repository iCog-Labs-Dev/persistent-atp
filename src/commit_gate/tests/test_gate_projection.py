"""The commit gate projecting into MORK.

Skipped without a MORK library; point `MORK_LIBRARY` at libmork_ffi.so and
`LD_PRELOAD` at the same path to run it.

MORK holds one space per process with no way to clear it, so each test commits
under its own proof id.
"""

import unittest

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate, ProjectionError
from commit_gate.ops import SetField, UpsertNode, ops_from_dicts
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import EdgeRecord, MemoryView, NodeRecord
from commit_gate.store import JournalStore
from mork.backend import MorkSpace, MorkUnavailable, MorkView

try:
    _SPACE = MorkSpace()
except MorkUnavailable:
    _SPACE = None

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")


class ReadOnlyView:
    """A view with no write methods, to show the gate notices."""

    def node(self, node_id: str) -> NodeRecord | None:
        return None

    def edge(self, edge_id: str) -> EdgeRecord | None:
        return None

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return ()

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return ()


class TestProjectsFlag(unittest.TestCase):
    def test_a_writable_view_is_projected_into(self):
        self.assertTrue(CommitGate(MemoryView(), JournalStore()).projects)

    def test_a_read_only_view_is_not(self):
        self.assertFalse(CommitGate(ReadOnlyView(), JournalStore()).projects)

    def test_a_read_only_view_still_commits(self):
        """Validation and journalling do not depend on being able to project."""
        gate = CommitGate(ReadOnlyView(), JournalStore())
        result = gate.commit(
            Proposal(
                proof_id="p1",
                actor="test",
                worker_class="test",
                ops=(UpsertNode("FormalState", "p1/fs1", {"status": "open"}),),
                base_revision=0,
            )
        )
        self.assertTrue(result.accepted, result.rejections)


class MorkGateCase(unittest.TestCase):
    """A gate reading and writing MORK, under a proof id of this test's own."""

    def setUp(self):
        self.view = self.make_view()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)
        self.proof = f"{self.prefix}-{self._testMethodName}"
        # A status change needs the write lease; these tests hold it throughout.
        self.token = self.store.acquire_lease(self.proof, "lease-1")

    prefix = "g"

    def make_view(self):
        return MorkView(_SPACE)

    def id_for(self, local):
        return f"{self.proof}/{local}"

    def propose(self, *ops, base_revision):
        return Proposal(
            proof_id=self.proof,
            actor="test",
            worker_class="test",
            ops=ops,
            base_revision=base_revision,
            lease_id="lease-1",
            fencing_token=self.token,
        )

    def commit_open_state(self):
        """Revision 1: an open FormalState."""
        result = self.gate.commit(
            self.propose(
                UpsertNode("FormalState", self.id_for("fs1"), {"status": "open"}),
                base_revision=0,
            )
        )
        self.assertTrue(result.accepted, result.rejections)
        return result

    def commit_expanded(self, base_revision, gate=None):
        """`open` -> `expanded`, a legal transition, stating its prior value."""
        return (gate or self.gate).commit(
            self.propose(
                SetField(
                    "FormalState", self.id_for("fs1"), "status", "expanded", "open"
                ),
                base_revision=base_revision,
            )
        )

    def statuses(self):
        """Every atom holding a status for fs1, however many there are."""
        pattern = f'(field "{self.proof}" "fs1" "status" $v)'
        return _SPACE.match(pattern, pattern)

    def committed(self):
        """Every atom under this test's proof."""
        return MorkView(_SPACE).atoms(self.proof)


@needs_mork
class TestGateProjectsToMork(MorkGateCase):
    def test_accepting_a_proposal_writes_it_to_mork(self):
        self.commit_open_state()

        node = self.view.node(self.id_for("fs1"))
        self.assertIsNotNone(node, "the gate accepted but MORK holds nothing")
        self.assertEqual(node.label, "FormalState")
        self.assertEqual(node.fields["status"], "open")

    def test_rejecting_a_proposal_writes_nothing_to_mork(self):
        # Missing the required `subgoal_count` on TacticApplication.
        result = self.gate.commit(
            self.propose(
                UpsertNode(
                    "TacticApplication",
                    self.id_for("ta1"),
                    {"executor_result": "lean-accepted"},
                ),
                base_revision=0,
            )
        )
        self.assertFalse(result.accepted)
        self.assertIsNone(self.view.node(self.id_for("ta1")))
        self.assertEqual(self.committed(), [])

    def test_a_later_commit_sees_the_earlier_one(self):
        self.commit_open_state()
        result = self.commit_expanded(base_revision=1)

        self.assertTrue(result.accepted, result.rejections)
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "expanded")
        self.assertEqual(len(self.statuses()), 1, "the old status survived")

    def test_a_stale_prior_value_is_rejected(self):
        """The gate validates against MORK, not against a stale copy.

        Two workers read `status` as "open" and both propose a change. The
        first commits. The second states a prior value MORK no longer holds,
        and only a gate reading MORK can tell.
        """
        self.commit_open_state()
        self.commit_expanded(base_revision=1)

        loser = self.gate.commit(
            self.propose(
                SetField("FormalState", self.id_for("fs1"), "status", "failed", "open"),
                base_revision=2,
            )
        )
        self.assertFalse(loser.accepted)
        self.assertIn(
            Reason.PRIOR_VALUE_MISMATCH, [r.reason for r in loser.rejections]
        )
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "expanded")
        self.assertEqual(len(self.statuses()), 1)

    def test_the_journal_and_mork_agree(self):
        """Replaying the journal reproduces what MORK holds.

        This is the repair path: if MORK is behind, replaying brings it level,
        and this is the check that replay and projection mean the same thing.
        """
        self.commit_open_state()
        self.commit_expanded(base_revision=1)

        replay = MemoryView()
        for payload in self.store.read_events(self.proof):
            apply_ops(replay, ops_from_dicts(payload.get("ops") or ()))

        expected = replay.node(self.id_for("fs1"))
        actual = self.view.node(self.id_for("fs1"))
        self.assertEqual(actual.label, expected.label)
        self.assertEqual(dict(actual.fields), dict(expected.fields))

    def test_replaying_the_journal_over_mork_changes_nothing(self):
        """What makes the repair safe to run when nothing is actually broken."""
        self.commit_open_state()
        self.commit_expanded(base_revision=1)
        before = self.committed()

        for payload in self.store.read_events(self.proof):
            apply_ops(self.view, ops_from_dicts(payload.get("ops") or ()))

        self.assertEqual(self.committed(), before)


@needs_mork
class TestProjectionFailure(MorkGateCase):
    """A graph write that fails after the journal has already accepted."""

    prefix = "f"

    class Breaks(MorkView):
        def set_field(self, node_id, name, value):
            raise RuntimeError("the whiteboard is unreachable")

    def make_view(self):
        return self.Breaks(_SPACE)

    def test_the_commit_stands_and_the_failure_is_raised(self):
        self.commit_open_state()

        with self.assertRaises(ProjectionError) as caught:
            self.commit_expanded(base_revision=1)

        # The revision is journalled, so it must be named: it is what a repair
        # replays from, and what tells a worker not to retry.
        self.assertEqual(caught.exception.revision, 2)
        self.assertIsNotNone(caught.exception.event_hash)
        self.assertEqual(self.store.head(self.proof)[0], 2)
        self.assertEqual(len(self.store.read_events(self.proof)), 2)

        # MORK is behind, which is the recoverable direction: the journal holds
        # the change, so replaying it later still produces the right state.
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "open")

    def test_the_mark_is_not_advanced_past_a_failed_projection(self):
        self.commit_open_state()
        with self.assertRaises(ProjectionError):
            self.commit_expanded(base_revision=1)

        # Behind the journal, never ahead of the graph. A mark that had moved to
        # 2 here would skip the change permanently.
        self.assertEqual(self.view.projected_revision(self.proof), 1)


@needs_mork
class TestCatchUp(MorkGateCase):
    """Recovering after a crash between the journal write and the graph write."""

    prefix = "c"

    class Breaks(MorkView):
        def set_field(self, node_id, name, value):
            raise RuntimeError("the whiteboard is unreachable")

    def crash_after_journalling(self):
        """Leave the journal at revision 2 and the graph at revision 1.

        The state a process killed between its two writes leaves behind.
        """
        self.commit_open_state()
        broken = CommitGate(self.Breaks(_SPACE), self.store)
        with self.assertRaises(ProjectionError):
            self.commit_expanded(base_revision=1, gate=broken)

        self.assertEqual(self.store.head(self.proof)[0], 2)
        self.assertEqual(self.view.projected_revision(self.proof), 1)
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "open")

    def test_the_mark_advances_with_each_commit(self):
        self.assertEqual(self.view.projected_revision(self.proof), 0)
        self.commit_open_state()
        self.assertEqual(self.view.projected_revision(self.proof), 1)
        self.commit_expanded(base_revision=1)
        self.assertEqual(self.view.projected_revision(self.proof), 2)

    def test_catch_up_finds_nothing_when_the_graph_is_level(self):
        self.commit_open_state()
        self.assertEqual(self.gate.catch_up(), {})

    def test_catch_up_on_a_read_only_view_does_nothing(self):
        self.commit_open_state()
        self.assertEqual(CommitGate(ReadOnlyView(), self.store).catch_up(), {})

    def test_a_stalled_projection_is_repaired_on_restart(self):
        self.crash_after_journalling()

        restarted = CommitGate(self.view, self.store)
        self.assertEqual(restarted.catch_up(), {self.proof: 1})

        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "expanded")
        self.assertEqual(self.view.projected_revision(self.proof), 2)
        self.assertEqual(len(self.statuses()), 1, "the old status survived the replay")

    def test_catching_up_twice_is_harmless(self):
        self.crash_after_journalling()
        restarted = CommitGate(self.view, self.store)
        restarted.catch_up()
        self.assertEqual(restarted.catch_up(), {})
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "expanded")
        self.assertEqual(len(self.statuses()), 1)

    def test_the_next_commit_heals_before_it_validates(self):
        """The reason healing is not left to whoever remembers to call it.

        After the crash the graph still reads "open". A proposal stating that
        prior value would be accepted against the stale graph, changing a field
        on the strength of a value the journal replaced two revisions ago. The
        gate catches up first, so it is rejected.
        """
        self.crash_after_journalling()

        restarted = CommitGate(self.view, self.store)
        result = restarted.commit(
            self.propose(
                SetField("FormalState", self.id_for("fs1"), "status", "failed", "open"),
                base_revision=2,
            )
        )

        self.assertFalse(result.accepted)
        self.assertIn(
            Reason.PRIOR_VALUE_MISMATCH, [r.reason for r in result.rejections]
        )
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "expanded")
        self.assertEqual(self.view.projected_revision(self.proof), 2)

    def test_a_commit_after_the_heal_is_accepted(self):
        """Healing does not leave the gate stuck: correct proposals still land."""
        self.crash_after_journalling()

        restarted = CommitGate(self.view, self.store)
        result = restarted.commit(
            self.propose(
                SetField(
                    "FormalState", self.id_for("fs1"), "status", "failed", "expanded"
                ),
                base_revision=2,
            )
        )

        self.assertTrue(result.accepted, result.rejections)
        self.assertEqual(self.view.node(self.id_for("fs1")).fields["status"], "failed")
        self.assertEqual(self.view.projected_revision(self.proof), 3)
        self.assertEqual(len(self.statuses()), 1)


if __name__ == "__main__":
    unittest.main()
