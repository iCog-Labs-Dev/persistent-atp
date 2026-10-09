"""End-to-end integration tests for the proof pipeline.

Verifies the full cycle: scheduler -> dispatch -> commit gate -> journal.
Tests verify lifecycle completion, stagnation handling, concurrency fencing,
and subgoal conservation invariants.
"""

import unittest

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.vocab import FormalStateStatus, RunDisposition
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.formal_atp import FakeFormalATP
from mathproof.scheduler import GlobalScheduler


def _seed_view(proof_id: str) -> MemoryView:
    view = MemoryView()
    view.add_node(f"{proof_id}/c-1", "Claim", {"status": "provisional"})
    view.add_node(
        f"{proof_id}/fd-1",
        "FormalDeclaration",
        {
            "status": "aligned",
            "lean_name": "even_sum",
            "lean_type": "forall n : N, Even n -> Even (n + n)",
            "lean_value": "by omega",
        },
    )
    view.add_node(f"{proof_id}/al-1", "Alignment", {"lifecycle": "reviewed", "verdict": "aligned"})
    view.add_node(f"{proof_id}/rs-1", "ResearchState", {"status": "open"})
    view.add_edge("ALIGNS_CLAIM", f"{proof_id}/al-1", f"{proof_id}/c-1", f"{proof_id}/al-1-claim")
    view.add_edge("ALIGNS_DECLARATION", f"{proof_id}/al-1", f"{proof_id}/fd-1", f"{proof_id}/al-1-decl")
    return view


def _harness(proof_id: str):
    view = _seed_view(proof_id)
    store = JournalStore(":memory:")
    gate = CommitGate(view, store)
    scheduler = GlobalScheduler(view, store)
    return view, store, gate, scheduler


def _maint(view: MemoryView):
    def _cb(proposal, commit):
        if commit.accepted:
            apply_ops(view, proposal.ops)
    return _cb


def _critic_dispatcher():
    return ScriptedDispatcher(
        {"critic": lambda lease, ctx: {"verdict": "critic-accepted", "actor": "test"}}
    )


def _research_dispatcher(proof_id: str):
    def _handle(lease, ctx):
        return {"parent_state_id": f"{proof_id}/rs-1", "detail": "even-sum by linearity", "actor": "test"}
    return ScriptedDispatcher({"llm-research": _handle})


class TestPocFullLifecycleSuccess(unittest.TestCase):
    def test_poc_full_lifecycle_success(self):
        pid = "even-sum"
        view, store, gate, scheduler = _harness(pid)
        atp = FakeFormalATP(plan={f"{pid}/fr-1": [RunDisposition.PROVED_PENDING_REPLAY.value]})
        maint = _maint(view)

        d = run_cycle(
            pid, view=view, gate=gate, scheduler=scheduler,
            dispatcher=_critic_dispatcher(), worker_class="critic", maintenance=maint,
        )
        self.assertTrue(d.accepted, d.rejections)
        self.assertEqual(view.node(f"{pid}/c-1").fields["status"], "critic-accepted")

        d = run_cycle(
            pid, view=view, gate=gate, scheduler=scheduler,
            dispatcher=_research_dispatcher(pid), worker_class="llm-research", maintenance=maint,
        )
        self.assertTrue(d.accepted, d.rejections)

        d = run_cycle(
            pid, view=view, gate=gate, scheduler=scheduler,
            dispatcher=ScriptedDispatcher({}), worker_class="formal-atp",
            adapters={"formal-atp": atp}, maintenance=maint, auto_replay=True,
        )
        self.assertTrue(d.accepted, d.rejections)
        self.assertIsNotNone(d.routing)
        self.assertEqual(d.routing.action, "hold-for-replay")

        n = store.verify_chain(pid)
        self.assertGreater(n, 0, "expected at least one journalled event")

        run_node = view.node(f"{pid}/fr-1")
        self.assertIsNotNone(run_node, "FormalRun node must be committed")
        self.assertEqual(run_node.fields.get("status"), RunDisposition.PROVED_PENDING_REPLAY.value)

        closed = [
            nid for nid in view.nodes
            if nid.startswith(f"{pid}/fs-")
            and view.node(nid).fields.get("status") == FormalStateStatus.FORMALLY_CLOSED.value
        ]
        self.assertGreater(len(closed), 0, "expected at least one formally-closed FormalState")


class TestPocObstructionAndAdaptiveBridge(unittest.TestCase):
    def test_poc_obstruction_and_adaptive_bridge(self):
        pid = "even-sum-stagnated"
        view, store, gate, scheduler = _harness(pid)
        atp = FakeFormalATP(plan={f"{pid}/fr-1": [RunDisposition.STAGNATED.value]})
        maint = _maint(view)

        run_cycle(pid, view=view, gate=gate, scheduler=scheduler,
                  dispatcher=_critic_dispatcher(), worker_class="critic", maintenance=maint)
        run_cycle(pid, view=view, gate=gate, scheduler=scheduler,
                  dispatcher=_research_dispatcher(pid), worker_class="llm-research", maintenance=maint)
        d = run_cycle(pid, view=view, gate=gate, scheduler=scheduler,
                      dispatcher=ScriptedDispatcher({}), worker_class="formal-atp",
                      adapters={"formal-atp": atp}, maintenance=maint)

        self.assertTrue(d.accepted, d.rejections)
        action = d.routing.action if d.routing else None
        self.assertIn(action, ("retry-with-new-context", "propose-bridge-lemma", None),
                      f"unexpected routing action: {action}")

        n = store.verify_chain(pid)
        self.assertGreater(n, 0)


class TestPocConcurrencyFencingEnforcement(unittest.TestCase):
    def test_poc_concurrency_fencing_enforcement(self):
        pid = "even-sum-fence"
        view = _seed_view(pid)
        store = JournalStore(":memory:")
        gate = CommitGate(view, store)

        token_a = store.acquire_lease(pid, "lease-a")
        first = gate.commit(Proposal(
            proof_id=pid, actor="worker-a", worker_class="llm-research",
            ops=(
                UpsertNode("ResearchMove", f"{pid}/rm-1", {"status": "queued", "detail": "x"}),
                AddEdge("PROPOSES", f"{pid}/rs-1", f"{pid}/rm-1", f"{pid}/rm-1-proposed"),
            ),
            base_revision=0, lease_id="lease-a", fencing_token=token_a,
        ))
        self.assertTrue(first.accepted, first.rejections)

        store.acquire_lease(pid, "lease-a")

        stale = gate.commit(Proposal(
            proof_id=pid, actor="worker-a", worker_class="llm-research",
            ops=(
                UpsertNode("ResearchMove", f"{pid}/rm-2", {"status": "queued", "detail": "y"}),
                AddEdge("PROPOSES", f"{pid}/rs-1", f"{pid}/rm-2", f"{pid}/rm-2-proposed"),
            ),
            base_revision=1, lease_id="lease-a", fencing_token=token_a,
        ))
        self.assertFalse(stale.accepted)
        reasons = [r.reason for r in stale.rejections]
        self.assertIn(Reason.FENCING_TOKEN_SUPERSEDED, reasons,
                      f"expected FENCING_TOKEN_SUPERSEDED, got {reasons}")


class TestPocSubgoalConservationRejection(unittest.TestCase):
    def test_poc_subgoal_conservation_rejection(self):
        pid = "even-sum-subgoal"
        view = _seed_view(pid)
        view.add_node(
            f"{pid}/fs-0",
            "FormalState",
            {
                "kind": "or",
                "goal_text": "Even (n + n)",
                "exact_state_hash": "sha256:" + "aa" * 32,
                "semantic_signature": "sha256:" + "bb" * 32,
                "status": "open",
            },
        )
        store = JournalStore(":memory:")
        gate = CommitGate(view, store)
        token = store.acquire_lease(pid, "lease-sub")

        result = gate.commit(Proposal(
            proof_id=pid, actor="formal-worker", worker_class="formal-atp",
            ops=(
                UpsertNode("TacticApplication", f"{pid}/ta-1",
                           {"tactic_label": "intro", "executor_result": "lean-accepted", "subgoal_count": 1}),
                AddEdge("APPLIED_TO", f"{pid}/fs-0", f"{pid}/ta-1", f"{pid}/ta-1-applied"),
            ),
            base_revision=0, lease_id="lease-sub", fencing_token=token,
        ))
        self.assertFalse(result.accepted)
        reasons = [r.reason for r in result.rejections]
        self.assertIn(Reason.SUBGOAL_COUNT_MISMATCH, reasons,
                      f"expected SUBGOAL_COUNT_MISMATCH, got {reasons}")


if __name__ == "__main__":
    unittest.main()
