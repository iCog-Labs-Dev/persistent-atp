"""The coordinator as a LangGraph StateGraph, end to end (Sections 10.2, 16.9).

Skipped when the optional `coordinator` extra (LangGraph) is not installed.
"""

import json
import threading
import unittest

try:
    from langgraph.checkpoint.memory import InMemorySaver
except ImportError:  # pragma: no cover - exercised only without the extra
    InMemorySaver = None

from coordinator.policy import BRIDGE_LEMMA, FIVE_MECHANISMS, GLOBAL_AUDIT, StagnationPolicy

from .fixtures import JournalCase, aligned_claim, critic, declaration, explorer

if InMemorySaver is not None:
    from coordinator.graph import BUDGET_EXHAUSTED, HALTED, Coordinator


@unittest.skipIf(InMemorySaver is None, "langgraph is not installed")
class TestDryRun(JournalCase):
    def test_a_stagnated_search_escalates_until_a_human_must_decide(self):
        """The Section 16.9 dry run: the fake ATP stagnates and escalation fires."""
        self.seed("p1", declaration("p1"))
        worked = []
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer(worked=worked)},
            plan={"p1/fr-1": ["stagnated"]},
        )

        final = Coordinator([workspace], max_rounds=8).run()

        self.assertEqual(final["outcome"], HALTED)
        self.assertEqual(final["tracks"]["p1"]["status"], "stalled")
        self.assertEqual(
            [(e["round"], e["rule"]) for e in final["escalations"]],
            [(1, BRIDGE_LEMMA), (1, GLOBAL_AUDIT), (2, FIVE_MECHANISMS)],
        )
        # Every escalation was leased by the explorer in a later round.
        self.assertEqual(sorted(worked), sorted(move.node_id for move in self.moves("p1")))
        self.assertEqual(final["milestones"][-1], f"coordinator: halted after {final['round']} rounds")

    def test_the_budget_ends_a_run_that_is_still_moving(self):
        self.seed("p1", declaration("p1"))
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer()},
            plan={"p1/fr-1": ["stagnated"]},
        )
        final = Coordinator([workspace], max_rounds=2).run()
        self.assertEqual(final["outcome"], BUDGET_EXHAUSTED)
        self.assertEqual(final["round"], 2)

    def test_policy_thresholds_reach_the_graph(self):
        self.seed("p1", declaration("p1"))
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer()},
            plan={"p1/fr-1": ["stagnated"]},
        )
        final = Coordinator(
            [workspace], max_rounds=8, policy=StagnationPolicy(non_progress_rounds=1)
        ).run()
        self.assertEqual(final["escalations"][0]["rule"], FIVE_MECHANISMS)
        self.assertEqual(final["escalations"][0]["round"], 1)


@unittest.skipIf(InMemorySaver is None, "langgraph is not installed")
class TestParallelProofs(JournalCase):
    def test_proofs_are_worked_at_the_same_time_and_end_on_their_own(self):
        both_in_flight = threading.Barrier(2, timeout=10)

        def meeting_critic(lease, packet):
            both_in_flight.wait()  # times out unless the other proof is running too
            return critic()(lease, packet)

        self.seed("p1", declaration("p1"), *aligned_claim("p1"))
        self.seed("p2", declaration("p2", status="replay-accepted"), *aligned_claim("p2"))
        p1 = self.workspace(
            "p1",
            handlers={"critic": meeting_critic, "llm-research": explorer()},
            plan={"p1/fr-1": ["stagnated"]},
        )
        p2 = self.workspace("p2", handlers={"critic": meeting_critic})

        final = Coordinator([p1, p2], max_rounds=8).run()

        self.assertEqual(final["tracks"]["p2"]["status"], "solved")
        self.assertEqual(final["tracks"]["p1"]["status"], "stalled")
        self.assertEqual({r["proof_id"] for r in final["reports"] if r["round"] > 1}, {"p1"})
        for proof_id in ("p1", "p2"):
            self.assertEqual(
                self.view_of(proof_id).node(f"{proof_id}/c-1").fields["status"], "critic-accepted"
            )
        self.assertEqual(self.open_store().verify_chain("p1"), self.open_store().head("p1")[0])


@unittest.skipIf(InMemorySaver is None, "langgraph is not installed")
class TestCheckpoints(JournalCase):
    def test_the_checkpoint_is_plain_data_and_no_proof_state(self):
        self.seed("p1", declaration("p1"))
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer()},
            plan={"p1/fr-1": ["stagnated"]},
        )
        coordinator = Coordinator([workspace], max_rounds=3)
        app = coordinator.build(InMemorySaver())
        config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 20}
        app.invoke(coordinator.initial_state(), config)

        values = app.get_state(config).values
        self.assertEqual(values["round"], 3)
        # Round counters, stall counts, reports and messages -- nothing a
        # fresh coordinator could not rebuild from the journal.
        self.assertEqual(
            set(values), {"round", "tracks", "reports", "escalations", "milestones", "outcome"}
        )
        json.dumps(values)
        self.assertNotIn("p1/fs-2", json.dumps(values["tracks"]))


if __name__ == "__main__":
    unittest.main()
