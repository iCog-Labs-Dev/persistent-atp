"""Coordinator rounds against the real gate and a journal on disk (Sections 10.2, 12.1).

These drive `work` and `assess` directly, without LangGraph: the decisions
and writes live here, and the graph only schedules them.
"""

import unittest

from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal

from coordinator.escalate import COORDINATOR_EDGES, COORDINATOR_LABELS
from coordinator.policy import BRIDGE_LEMMA, FIVE_MECHANISMS, GLOBAL_AUDIT, REPAIR
from coordinator.rounds import ACTIVE, PARKED, SOLVED, STALLED, assess, new_track, work

from .fixtures import JournalCase, aligned_claim, critic, declaration, explorer


def candidate_certificate(proof_id: str) -> list:
    """A certificate bound to its run, declaration and pinned environment."""
    env, run, cert, fd = (f"{proof_id}/{local}" for local in ("env-1", "fr-1", "cert-1", "fd-1"))
    env_hash = "sha256:" + "e" * 64
    return [
        UpsertNode("Environment", env, {"environment_hash": env_hash}),
        AddEdge("PINNED_ENVIRONMENT", fd, env, f"{fd}-pinned"),
        UpsertNode("FormalRun", run, {"status": "proved-pending-replay", "environment_hash": env_hash}),
        AddEdge("SEARCHES", run, fd, f"{run}-searches"),
        AddEdge("RAN_UNDER", run, env, f"{run}-env"),
        UpsertNode(
            "Certificate",
            cert,
            {
                "status": "candidate",
                "artifact_hash": "sha256:" + "c" * 64,
                "environment_hash": env_hash,
                "producer_run_id": run,
            },
        ),
        AddEdge("CERTIFIES", cert, fd, f"{cert}-certifies"),
        AddEdge("PRODUCED_CERTIFICATE", run, cert, f"{cert}-produced"),
        AddEdge("CERTIFICATE_ENVIRONMENT", cert, env, f"{cert}-env"),
    ]


class RoundCase(JournalCase):
    def run_rounds(self, workspace, count, track=None):
        """`count` rounds on one proof; the final track and every round's outcome."""
        track = track or new_track()
        history = []
        for round_number in range(1, count + 1):
            report = work(workspace, round_number)
            track, raised, milestones = assess(workspace, track, report)
            history.append((report, raised, milestones))
            if track["status"] != ACTIVE:
                break
        return track, history


class TestPortfolio(RoundCase):
    def test_each_slot_leases_only_its_own_category(self):
        self.seed("p1", declaration("p1"), *aligned_claim("p1"))
        workspace = self.workspace(
            "p1", handlers={"critic": critic()}, plan={"p1/fr-1": ["budget-exhausted"]}
        )

        report = work(workspace, 1)

        self.assertEqual(
            [(slot["slot"], slot["worker_class"], slot["move_id"], slot["accepted"]) for slot in report["slots"]],
            [
                ("exploit", "formal-atp", "p1/fd-1", True),
                ("critic", "critic", "p1/c-1", True),
            ],
        )
        self.assertEqual(self.view_of("p1").node("p1/c-1").fields["status"], "critic-accepted")

    def test_verified_gain_resets_the_stall_and_is_announced(self):
        self.seed("p1", declaration("p1"), *aligned_claim("p1"))
        workspace = self.workspace(
            "p1", handlers={"critic": critic()}, plan={"p1/fr-1": ["budget-exhausted"]}
        )
        track, raised, milestones = assess(
            workspace, {**new_track(), "stalled_rounds": 1}, work(workspace, 1)
        )
        self.assertEqual(track["stalled_rounds"], 0)
        self.assertEqual(track["progress"], [1, 0, 0])
        self.assertIn("p1: 1 claims settled", milestones)
        # No stall rule fires after verified gain; the audit is only the
        # empty frontier's cue to expand (the run ended budget-exhausted).
        self.assertEqual([e["rule"] for e in raised], [GLOBAL_AUDIT])


class TestEscalation(RoundCase):
    def test_a_stagnated_search_queues_a_bridge_lemma_through_the_gate(self):
        self.seed("p1", declaration("p1"))
        workspace = self.workspace("p1", plan={"p1/fr-1": ["stagnated"]})

        track, raised, milestones = assess(workspace, new_track(), work(workspace, 1))

        self.assertEqual(
            [(e["rule"], e["target_id"]) for e in raised],
            [(BRIDGE_LEMMA, "p1/fs-2"), (GLOBAL_AUDIT, "p1")],
        )
        self.assertEqual(track["status"], ACTIVE)
        (event,) = self.coordinator_events("p1")
        self.assertEqual(event["actor"], "coordinator")
        (bridge,) = self.moves("p1", BRIDGE_LEMMA)
        self.assertEqual(bridge.fields["status"], "queued")
        self.assertEqual(bridge.fields["target_id"], "p1/fs-2")
        self.assertTrue(any("bridge-lemma" in message for message in milestones))

    def test_the_next_round_works_the_escalation_and_retires_it(self):
        self.seed("p1", declaration("p1"))
        worked = []
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer(worked=worked)},
            plan={"p1/fr-1": ["stagnated"]},
        )
        self.run_rounds(workspace, 2)

        (bridge,) = self.moves("p1", BRIDGE_LEMMA)
        self.assertIn(bridge.node_id, worked)
        self.assertEqual(bridge.fields["status"], "exhausted")

    def test_two_rounds_without_progress_ask_for_five_mechanisms_and_queue_them(self):
        self.seed("p1", declaration("p1"))
        answers = {
            FIVE_MECHANISMS: [
                {"detail": f"try {tag}", "technique_tags": [tag]}
                for tag in ("induction", "extremal", "algebraic", "analytic", "probabilistic")
            ]
        }
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer(answers)},
            plan={"p1/fr-1": ["stagnated"]},
        )
        track, history = self.run_rounds(workspace, 4)

        (request,) = self.moves("p1", FIVE_MECHANISMS)
        self.assertEqual(request.fields["detail"].split(";")[0], "no verified progress in 2 rounds")
        self.assertEqual(request.fields["status"], "exhausted")
        proposed = [
            move for move in self.moves("p1")
            if (move.fields.get("technique_tags") or [None])[0] is not None
        ]
        self.assertEqual(len(proposed), 5)
        self.assertEqual(track["status"], ACTIVE)

    def test_a_refuted_claim_is_sent_back_for_repair(self):
        self.seed("p1", *aligned_claim("p1"))
        worked = []
        verdicts = iter(
            [critic("refuted", earliest_gap="the base case"), critic("critic-accepted")]
        )
        workspace = self.workspace(
            "p1",
            handlers={
                "critic": lambda lease, packet: next(verdicts)(lease, packet),
                "llm-research": explorer(worked=worked),
            },
        )
        track, history = self.run_rounds(workspace, 2)

        (repair,) = self.moves("p1", REPAIR)
        self.assertIn("the base case", repair.fields["detail"])
        view = self.view_of("p1")
        (target,) = view.edges_from(repair.node_id, "MOVE_TARGETS")
        self.assertEqual(target.dst_id, "p1/c-1")
        self.assertEqual(worked, [repair.node_id])

    def test_a_proof_with_nothing_left_halts_for_a_human(self):
        self.seed("p1", declaration("p1"))
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer()},
            plan={"p1/fr-1": ["stagnated"]},
        )
        track, history = self.run_rounds(workspace, 10)

        self.assertEqual(track["status"], STALLED)
        self.assertLess(len(history), 10)
        self.assertIn("needs a human decision", history[-1][2][-1])


class TestAuthority(RoundCase):
    def test_the_coordinator_writes_only_queued_research_moves(self):
        self.seed("p1", declaration("p1"), *aligned_claim("p1"))
        workspace = self.workspace(
            "p1",
            handlers={"llm-research": explorer(), "critic": critic("refuted")},
            plan={"p1/fr-1": ["stagnated"]},
        )
        self.run_rounds(workspace, 10)

        events = self.coordinator_events("p1")
        self.assertGreater(len(events), 1)
        for event in events:
            for op in Proposal.from_dict(event).ops:
                self.assertNotIsInstance(op, (SetField, RemoveEdge))
                if isinstance(op, UpsertNode):
                    self.assertIn(op.label, COORDINATOR_LABELS)
                    self.assertIn(op.fields["status"], ("open", "queued"))
                else:
                    self.assertIsInstance(op, AddEdge)
                    self.assertIn(op.rel_type, COORDINATOR_EDGES)


class TestJournalAuthority(RoundCase):
    def test_a_restarted_coordinator_raises_nothing_already_on_record(self):
        self.seed("p1", declaration("p1"))
        first = self.workspace("p1", plan={"p1/fr-1": ["stagnated"]})
        track, _, _ = assess(first, new_track(), work(first, 1))
        committed = len(self.coordinator_events("p1"))

        # A new coordinator: empty view, no cached track, same journal.
        again = self.workspace("p1")
        restart = {"proof_id": "p1", "round": 1, "before": [0, 0, 0], "slots": []}
        _, raised, _ = assess(again, new_track(), restart)

        self.assertEqual(raised, [])
        self.assertEqual(len(self.coordinator_events("p1")), committed)
        self.assertEqual(again.view.nodes.keys(), self.view_of("p1").nodes.keys())


class TestEnding(RoundCase):
    def test_a_replayed_declaration_solves_the_proof(self):
        self.seed("p1", declaration("p1", status="replay-accepted"), *aligned_claim("p1"))
        workspace = self.workspace("p1", handlers={"critic": critic()})
        track, raised, milestones = assess(workspace, new_track(), work(workspace, 1))
        self.assertEqual(track["status"], SOLVED)
        self.assertIn("p1: solved; every declaration replayed", milestones)

    def test_a_certificate_awaiting_replay_parks_the_proof(self):
        # The search is over and its certificate waits for the replayer.
        self.seed("p1", declaration("p1", status="certificate-produced"), *candidate_certificate("p1"))
        workspace = self.workspace("p1")
        track, raised, milestones = assess(
            workspace, {**new_track(), "stalled_rounds": 5}, work(workspace, 1)
        )

        self.assertEqual(raised, [])
        self.assertEqual(track["status"], PARKED)
        self.assertIn("p1: a certificate awaits replay; formal search pauses", milestones)


if __name__ == "__main__":
    unittest.main()
