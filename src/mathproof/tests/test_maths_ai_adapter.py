"""MathsAIFormalATP: hybrid-reasoner results mapped onto the formal seam."""

import unittest

from commit_gate.vocab import RunDisposition
from mathproof.formal_atp import FormalATPAdapter
from mathproof.maths_ai_atp import MathsAIFormalATP
from mathproof.schemas import validate
from mathproof.soundness import validate_formal_search_result


REQUEST = {
    "proof_id": "p1",
    "claim_id": "p1/c-1",
    "formal_declaration_id": "p1/fd-1",
    "run_id": "p1/fr-1",
    "base_revision": 0,
    "lease_id": "lease-p1",
    "fencing_token": 1,
    "lean_source_artifact": "sha256:" + "b2" * 32,
    "environment_id": "lean-pinned",
    "environment_hash": "sha256:" + "c3" * 32,
    "goal_text": "forall (p q : Prop), Or p q -> Or q p",
    "hypotheses": [],
}


class StubGoal:
    def __init__(self, expression, hypotheses=()):
        self.expression = expression
        self.hypotheses = list(hypotheses)


class StubTactic:
    def __init__(self, name, arguments=(), probability=0.9):
        self.tactic_name = name
        self.arguments = list(arguments)
        self.probability = probability


class StubNode:
    def __init__(self, node_id, goal, status, probability=1.0):
        self.id = node_id
        self.goal = goal
        self.status = status
        self.gnn_probability = probability
        self.stv = None
        self.note = None


class StubEdge:
    def __init__(self, source_id, tactic, child_ids, status="pending"):
        self.source_id = source_id
        self.tactic = tactic
        self.child_ids = list(child_ids)
        self.status = status


class StubGraph:
    def __init__(self, nodes, edges, *, solved=False, exhausted=False, trace=None):
        self.nodes = {node.id: node for node in nodes}
        self.edges = {index + 1: edge for index, edge in enumerate(edges)}
        self.root_id = nodes[0].id
        self._solved = solved
        self._exhausted = exhausted
        self._trace = trace

    def is_solved(self):
        return self._solved

    def is_exhausted(self):
        return self._exhausted

    @property
    def root(self):
        return self.nodes[self.root_id]

    def frontier(self):
        return [node for node in self.nodes.values() if node.status == "open"]

    def proof_trace(self):
        return self._trace


class StubReasoner:
    def __init__(self, *graphs):
        self.graphs = list(graphs)
        self.calls = []

    async def prove(self, goal, hypotheses=None):
        self.calls.append((goal, list(hypotheses or [])))
        return self.graphs.pop(0)


def solved_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "solved")
    return StubGraph(
        [root],
        [StubEdge(0, StubTactic("exact", ["Or.comm"], 0.93), [], status="solved")],
        solved=True,
        trace={"goal": REQUEST["goal_text"], "tactic": "exact",
               "arguments": ["Or.comm"], "subgoals": []},
    )


def open_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "expanded")
    left = StubNode(1, StubGoal("q", ["h : p"]), "open", probability=0.8)
    right = StubNode(2, StubGoal("p", ["h : p"]), "open", probability=0.7)
    edge = StubEdge(0, StubTactic("intro", ["h"], 0.85), [1, 2])
    return StubGraph([root, left, right], [edge])


def dead_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "dead")
    root.note = "executor rejected every candidate tactic"
    edge = StubEdge(0, StubTactic("omega"), [], status="dead")
    return StubGraph([root], [edge], exhausted=True)


def pln_solved_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "solved")
    edge = StubEdge(0, StubTactic("PLN_fallback", [], 1.0), [], status="solved")
    return StubGraph(
        [root],
        [edge],
        solved=True,
        trace={"goal": REQUEST["goal_text"], "tactic": "PLN_fallback"},
    )


def mixed_root_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "solved")
    return StubGraph(
        [root],
        [
            StubEdge(0, StubTactic("exact", ["Or.comm"], 0.93), [], status="solved"),
            StubEdge(0, StubTactic("PLN_fallback", [], 1.0), [], status="solved"),
        ],
        solved=True,
        trace={"goal": REQUEST["goal_text"], "tactic": "exact"},
    )


def mid_graph_fallback_branch():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "solved")
    side = StubNode(1, StubGoal("side goal"), "solved")
    return StubGraph(
        [root, side],
        [
            StubEdge(0, StubTactic("exact", ["Or.comm"], 0.93), [], status="solved"),
            StubEdge(0, StubTactic("PLN_fallback", [], 1.0), [1], status="solved"),
        ],
        solved=True,
        trace={"goal": REQUEST["goal_text"], "tactic": "exact"},
    )


class TestMathsAIFormalATP(unittest.TestCase):
    def test_satisfies_the_adapter_protocol(self):
        self.assertIsInstance(MathsAIFormalATP(reasoner=StubReasoner()), FormalATPAdapter)

    def test_construction_defers_the_real_backend(self):
        atp = MathsAIFormalATP()
        self.assertIsNone(atp._reasoner)

    def test_missing_request_fields_is_invalid_request(self):
        atp = MathsAIFormalATP(reasoner=StubReasoner(solved_graph()))
        broken = {k: v for k, v in REQUEST.items() if k != "fencing_token"}
        result = atp.formal_search_start(broken)
        self.assertEqual(result["disposition"], RunDisposition.INVALID_REQUEST.value)
        self.assertIn("fencing_token", result["artifacts"][0]["note"])

    def test_missing_goal_text_is_invalid_request(self):
        atp = MathsAIFormalATP(reasoner=StubReasoner())
        request = {k: v for k, v in REQUEST.items() if k != "goal_text"}
        result = atp.formal_search_start(request)
        self.assertEqual(result["disposition"], RunDisposition.INVALID_REQUEST.value)

    def test_solved_search_emits_proved_pending_replay(self):
        reasoner = StubReasoner(solved_graph())
        result = dict(MathsAIFormalATP(reasoner=reasoner).formal_search_start(REQUEST))
        self.assertEqual(result["disposition"], RunDisposition.PROVED_PENDING_REPLAY.value)
        self.assertEqual(validate("formal-search-result", result), [])
        self.assertEqual(validate_formal_search_result(result), ())
        self.assertEqual(result["certificate"]["status"], "candidate")
        self.assertEqual(result["certificate"]["producer_run_id"], "p1/fr-1")
        self.assertEqual(result["checkpoint"]["frontier_state_ids"], [])
        self.assertEqual(
            [(goal, hyps) for goal, hyps in reasoner.calls],
            [(REQUEST["goal_text"], [])],
        )

    def test_open_frontier_emits_budget_exhausted_with_checkpoint(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(open_graph())).formal_search_start(REQUEST)
        )
        self.assertEqual(result["disposition"], RunDisposition.BUDGET_EXHAUSTED.value)
        self.assertEqual(validate("formal-search-result", result), [])
        self.assertEqual(validate_formal_search_result(result), ())
        self.assertEqual(
            result["checkpoint"]["frontier_state_ids"],
            ["p1/fs-2", "p1/fs-3"],
        )
        multi_child = next(
            edge for edge in result["tactic_edges"] if edge["subgoal_count"] == 2
        )
        self.assertEqual(multi_child["produced_goal_ids"], ["p1/fs-2", "p1/fs-3"])
        self.assertEqual(multi_child["executor_result"], "lean-accepted")

    def test_dead_root_emits_stagnated_with_obstruction(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(dead_graph())).formal_search_start(REQUEST)
        )
        self.assertEqual(result["disposition"], RunDisposition.STAGNATED.value)
        self.assertEqual(validate("formal-search-result", result), [])
        self.assertEqual(validate_formal_search_result(result), ())
        obstruction = result["obstructions"][0]
        self.assertEqual(obstruction["kind"], "search-policy")
        self.assertEqual(obstruction["environment_hash"], REQUEST["environment_hash"])

    def test_pln_only_root_is_downgraded_not_proved(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(pln_solved_graph())).formal_search_start(REQUEST)
        )
        self.assertEqual(result["disposition"], RunDisposition.BUDGET_EXHAUSTED.value)
        self.assertNotIn("certificate", result)
        root = next(s for s in result["states"] if s["state_id"] == "p1/fs-1")
        self.assertEqual(root["status"], "open")
        self.assertIn("p1/fs-1", result["checkpoint"]["frontier_state_ids"])
        self.assertEqual(result["tactic_edges"][0]["executor_result"], "empty-output")

    def test_real_branch_still_proves_alongside_a_fallback_edge(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(mixed_root_graph())).formal_search_start(REQUEST)
        )
        self.assertEqual(result["disposition"], RunDisposition.PROVED_PENDING_REPLAY.value)
        self.assertIn("certificate", result)
        root = next(s for s in result["states"] if s["state_id"] == "p1/fs-1")
        self.assertEqual(root["status"], "formally-closed")
        by_label = {e["tactic_label"]: e for e in result["tactic_edges"]}
        self.assertEqual(by_label["exact"]["executor_result"], "lean-accepted")
        self.assertEqual(by_label["PLN_fallback"]["executor_result"], "empty-output")

    def test_fallback_closed_side_branch_stays_open(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(mid_graph_fallback_branch())).formal_search_start(REQUEST)
        )
        statuses = {s["state_id"]: s["status"] for s in result["states"]}
        self.assertEqual(statuses["p1/fs-1"], "formally-closed")
        self.assertEqual(statuses["p1/fs-2"], "open")
        fallback = next(e for e in result["tactic_edges"] if e["tactic_label"] == "PLN_fallback")
        self.assertEqual(fallback["executor_result"], "empty-output")

    def test_dead_edges_carry_kernel_diagnostics(self):
        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(dead_graph())).formal_search_start(REQUEST)
        )
        dead = result["tactic_edges"][0]
        self.assertEqual(dead["executor_result"], "lean-rejected")
        self.assertRegex(dead["diagnostic_artifact"], r"^sha256:[0-9a-f]{64}$")

    def test_c6_soundness_flags_fallback_claiming_lean_acceptance(self):
        from mathproof.soundness import SoundnessReason

        result = dict(
            MathsAIFormalATP(reasoner=StubReasoner(pln_solved_graph())).formal_search_start(REQUEST)
        )
        result["tactic_edges"][0]["executor_result"] = "lean-accepted"
        reasons = [v.reason for v in validate_formal_search_result(result)]
        self.assertIn(SoundnessReason.NON_KERNEL_CLOSURE, reasons)

    def test_c6_honestly_labelled_fallback_results_stay_clean(self):
        from mathproof.soundness import SoundnessReason

        for graph in (pln_solved_graph(), mixed_root_graph()):
            with self.subTest(graph=type(graph).__name__):
                result = dict(
                    MathsAIFormalATP(reasoner=StubReasoner(graph)).formal_search_start(REQUEST)
                )
                reasons = [v.reason for v in validate_formal_search_result(result)]
                self.assertNotIn(SoundnessReason.NON_KERNEL_CLOSURE, reasons)

    def test_reasoner_failure_emits_internal_error(self):
        class Exploding:
            async def prove(self, goal, hypotheses=None):
                raise RuntimeError("lean backend exploded")

        result = dict(MathsAIFormalATP(reasoner=Exploding()).formal_search_start(REQUEST))
        self.assertEqual(result["disposition"], RunDisposition.INTERNAL_ERROR.value)
        self.assertIn("RuntimeError", result["artifacts"][0]["note"])

    def test_status_does_not_consume_and_resume_reruns_the_search(self):
        reasoner = StubReasoner(open_graph(), solved_graph())
        atp = MathsAIFormalATP(reasoner=reasoner)
        first = atp.formal_search_start(REQUEST)
        self.assertEqual(first["disposition"], RunDisposition.BUDGET_EXHAUSTED.value)
        self.assertEqual(atp.formal_search_status("p1/fr-1"), first)
        resumed = atp.formal_search_resume("p1/fr-1")
        self.assertEqual(resumed["disposition"], RunDisposition.PROVED_PENDING_REPLAY.value)
        self.assertEqual(len(reasoner.calls), 2)

    def test_resume_of_unknown_run_is_invalid_request(self):
        atp = MathsAIFormalATP(reasoner=StubReasoner())
        self.assertEqual(
            atp.formal_search_resume("p1/fr-404")["disposition"],
            RunDisposition.INVALID_REQUEST.value,
        )

    def test_cancel_stops_the_run(self):
        atp = MathsAIFormalATP(reasoner=StubReasoner(solved_graph()))
        atp.formal_search_cancel("p1/fr-1")
        self.assertEqual(
            atp.formal_search_resume("p1/fr-1")["disposition"], "cancelled"
        )

    def test_replay_delegates_to_the_injected_verdict(self):
        def reject(certificate, environment_hash):
            return {"status": "rejected", "rejection_reason": "no-lean"}

        atp = MathsAIFormalATP(reasoner=StubReasoner(), replay_fn=reject)
        verdict = atp.formal_replay({"artifact_hash": "x"}, REQUEST["environment_hash"])
        self.assertEqual(verdict, {"status": "rejected", "rejection_reason": "no-lean"})


if __name__ == "__main__":
    unittest.main()
