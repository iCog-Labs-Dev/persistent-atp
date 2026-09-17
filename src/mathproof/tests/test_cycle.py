"""End-to-end cycles: lease -> dispatch -> proposal -> commit (Section 6).

Each test drives `run_cycle` against the real gate and journal, with scripted
workers behind the dispatcher seam -- the Phase 3 harness shape. What must
hold: worker-class routing picks the right path, results become proposals the
gate accepts, outcomes feed scheduler statistics, and nothing writes status
except through a proposal.
"""

import unittest

from commit_gate.apply import apply_ops
from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.cycle import run_cycle
from mathproof.dispatch import ScriptedDispatcher
from mathproof.scheduler import GlobalScheduler


def seeded_view() -> MemoryView:
    """A proof with one provisional claim and an aligned declaration."""
    v = MemoryView()
    v.add_node("p1/c-1", "Claim", {"status": "provisional"})
    v.add_node(
        "p1/fd-1",
        "FormalDeclaration",
        {
            "status": "aligned",
            "lean_name": "trivial",
            "lean_type": "True",
            "lean_value": "trivial : True := trivial",
        },
    )
    return v


class Harness(unittest.TestCase):
    def setUp(self):
        self.view = seeded_view()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)
        self.scheduler = GlobalScheduler(self.view, self.store)

    def cycle(self, dispatcher: ScriptedDispatcher, worker_class: str, **kwargs):
        return run_cycle(
            "p1",
            view=self.view,
            gate=self.gate,
            scheduler=self.scheduler,
            dispatcher=dispatcher,
            worker_class=worker_class,
            maintenance=lambda proposal, commit: apply_ops(self.view, proposal.ops),
            **kwargs,
        )


class TestCriticCycle(Harness):
    def setUp(self):
        super().setUp()
        self.view.add_node(
            "p1/al-1",
            "Alignment",
            {"lifecycle": "reviewed", "verdict": "aligned"},
        )
        self.view.add_edge("ALIGNS_CLAIM", "p1/al-1", "p1/c-1", "p1/al-1-aligns")

    def test_a_critic_lease_promotes_the_claim_through_the_gate(self):
        def critic_handler(lease, context):
            self.assertEqual(context["role"], "critic")
            self.assertEqual(context["claim"]["id"], "p1/c-1")
            return {"verdict": "critic-accepted", "actor": "critic-1"}

        digest = self.cycle(
            ScriptedDispatcher({"critic": critic_handler}), worker_class="critic"
        )

        self.assertTrue(digest.accepted, digest.rejections)
        self.assertEqual(digest.selected_move_id, "p1/c-1")
        claim = self.view.node("p1/c-1")
        self.assertEqual(claim.fields["status"], "critic-accepted")
        attempts = [
            node_id
            for node_id in self.view.nodes
            if node_id.rsplit("/", 1)[-1].startswith("at-")
        ]
        self.assertEqual(len(attempts), 1)
        reviewer = self.view.node(attempts[0])
        self.assertEqual(reviewer.fields["worker_class"], "critic")

    def test_outcomes_feed_scheduler_statistics(self):
        self.cycle(ScriptedDispatcher({"critic": lambda l, c: {"verdict": "supported"}}), "critic")
        self.assertEqual(self.scheduler.statistics.attempts("critic-task"), 1)


class TestResearchCycle(Harness):
    def test_an_explorer_lease_proposes_a_new_research_move(self):
        self.view.add_node("p1/rs-1", "ResearchState", {"status": "open"})
        # The claim is the only frontier item until we want the explorer;
        # lease it away first by promoting it via a critic cycle.
        self.cycle(
            ScriptedDispatcher({"critic": lambda l, c: {"verdict": "supported"}}),
            "critic",
        )

        def explorer_handler(lease, context):
            self.assertEqual(context["role"], "research")
            return {
                "parent_state_id": "p1/rs-1",
                "detail": "bridge lemma candidate",
                "actor": "explorer-1",
            }

        digest = self.cycle(
            ScriptedDispatcher({"llm-research": explorer_handler}),
            worker_class="llm-research",
        )
        self.assertTrue(digest.accepted, digest.rejections)
        moves = [
            node_id
            for node_id in self.view.nodes
            if node_id.rsplit("/", 1)[-1].startswith("rm-")
        ]
        self.assertEqual(len(moves), 1)


class TestFormalCycle(Harness):
    def test_a_formal_lease_runs_through_the_adapter_and_commits_states(self):
        from mathproof.formal_atp import FakeFormalATP

        plan_result = {
            "run_id": "p1/fr-1",
            "disposition": "budget-exhausted",
            "root_state_id": "p1/fs-1",
            "states": [
                {
                    "state_id": "p1/fs-1",
                    "kind": "or",
                    "goal_text": "True",
                    "exact_state_hash": "sha256:" + "11" * 32,
                    "semantic_signature": "sha256:" + "22" * 32,
                    "status": "expanded",
                },
                {
                    "state_id": "p1/fs-2",
                    "kind": "or",
                    "goal_text": "True (child)",
                    "exact_state_hash": "sha256:" + "33" * 32,
                    "semantic_signature": "sha256:" + "44" * 32,
                    "status": "open",
                },
            ],
            "tactic_edges": [
                {
                    "tactic_id": "p1/ta-1",
                    "source_state_id": "p1/fs-1",
                    "tactic_label": "intro",
                    "executor_result": "lean-accepted",
                    "subgoal_count": 1,
                    "produced_goal_ids": ["p1/fs-2"],
                }
            ],
            "checkpoint": {"epoch_ms": 5, "frontier_state_ids": ["p1/fs-2"]},
            "obstructions": [],
            "artifacts": [],
        }
        adapter = FakeFormalATP(plan={"p1/fr-1": [plan_result]})

        digest = self.cycle(
            ScriptedDispatcher({}),
            worker_class="formal-atp",
            adapters={"formal-atp": adapter},
        )

        self.assertTrue(digest.accepted, [r.to_dict() for r in digest.rejections])
        self.assertEqual(digest.selected_move_id, "p1/fd-1")
        self.assertIsNotNone(self.view.node("p1/fs-1"))
        self.assertIsNotNone(self.view.node("p1/ta-1"))
        self.assertEqual(
            len(self.view.edges_from("p1/ta-1", "FORMAL_REQUIRES")), 1
        )
        self.assertEqual(digest.worker_class, "formal-atp")


class TestEmptyFrontier(Harness):
    def test_no_eligible_moves_routes_to_audit(self):
        empty_view = MemoryView()
        gate = CommitGate(empty_view, JournalStore())
        digest = run_cycle(
            "p9",
            view=empty_view,
            gate=gate,
            scheduler=GlobalScheduler(empty_view, JournalStore()),
            dispatcher=ScriptedDispatcher({}),
        )
        self.assertFalse(digest.lease_issued)
        self.assertTrue(digest.audit_recommended)


if __name__ == "__main__":
    unittest.main()
