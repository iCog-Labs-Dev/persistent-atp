"""Synthetic adapter regressions and production replay/resume boundaries."""
from mathproof.formal_atp import stub_replay
from mathproof.maths_ai_atp import MathsAIFormalATP
from mathproof.soundness import validate_formal_search_result
from mathproof.tests.test_maths_ai_adapter import (
    REQUEST, StubEdge, StubGoal, StubGraph, StubNode, StubReasoner, StubTactic,
)


def test_o24_default_replay_never_verifies():
    atp = MathsAIFormalATP(reasoner=object(), production=True)
    result = atp.formal_replay({"artifact_hash": "sha256:" + "a" * 64}, "sha256:" + "b" * 64)
    assert result["status"] == "rejected"
    assert result["rejection_reason"] == "independent-replay-required"
    stubbed = MathsAIFormalATP(reasoner=object(), replay_fn=stub_replay("verified"), production=True)
    assert stubbed.formal_replay({}, "sha256:" + "b" * 64)["status"] == "rejected"
    arbitrary = MathsAIFormalATP(reasoner=object(),
        replay_fn=lambda _certificate, _environment: {"status": "verified"}, production=True)
    assert arbitrary.formal_replay({}, "sha256:" + "b" * 64)["status"] == "rejected"


def test_o25_resume_cannot_restart_search():
    class NoContinuation:
        def __init__(self): self.calls = 0
        async def prove(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("backend failed")

    backend = NoContinuation()
    atp = MathsAIFormalATP(reasoner=backend)
    request = {"run_id": "p1/fr-1", "proof_id": "p1", "claim_id": "p1/c-1",
        "formal_declaration_id": "p1/fd-1", "base_revision": 0,
        "lease_id": "p1/lease-1", "fencing_token": 1,
        "lean_source_artifact": "sha256:" + "a" * 64,
        "environment_id": "lean-pinned", "environment_hash": "sha256:" + "b" * 64,
        "goal_text": "True"}
    atp.formal_search_start(request)
    assert backend.calls == 1
    response = atp.formal_search_resume("p1/fr-1")
    assert response["disposition"] == "invalid-request"
    assert "unsupported-checkpoint-resume" in response["artifacts"][0]["note"]
    assert backend.calls == 1


def test_o09_adapter_preserves_five_children_from_stub_graph():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "expanded")
    children = [StubNode(index, StubGoal(f"child {index}"), "open") for index in range(1, 6)]
    reasoner = StubReasoner(StubGraph([root, *children], [
        StubEdge(0, StubTactic("constructor"), [1, 2, 3, 4, 5])]))
    result = MathsAIFormalATP(reasoner=reasoner).formal_search_start(REQUEST)
    assert result["disposition"] == "budget-exhausted"
    edge = result["tactic_edges"][0]
    assert edge["subgoal_count"] == len(edge["produced_goal_ids"]) == 5
    assert set(edge["produced_goal_ids"]) == {state["state_id"] for state in result["states"][1:]}
    assert len(result["checkpoint"]["frontier_state_ids"]) == 5
    assert validate_formal_search_result(result) == ()


def test_o10_heuristic_score_cannot_turn_open_goal_into_verified_result():
    root = StubNode(0, StubGoal(REQUEST["goal_text"]), "solved", probability=1.0)
    root.stv = type("TruthValue", (), {"strength": 1.0, "confidence": 1.0})()
    graph = StubGraph([root], [StubEdge(0, StubTactic("PLN_fallback", probability=1.0), [], status="solved")], solved=True)
    result = MathsAIFormalATP(reasoner=StubReasoner(graph)).formal_search_start(REQUEST)
    assert result["disposition"] == "budget-exhausted"
    assert result["states"][0]["status"] == "open"
    assert result["states"][0]["annotations"]["pln_strength"] == 1.0
    assert "certificate" not in result


def test_production_request_requires_authoritative_compilation_and_bounded_backend():
    from copy import deepcopy
    from mathproof.tests.test_maths_ai_adapter import open_graph
    from mathproof.schemas import validate
    request = {key: value for key, value in REQUEST.items() if key != 'hypotheses'}
    request['budget'] = {'max_steps': 10, 'wall_clock_ms': 1000}
    authoritative = deepcopy(request)
    class Bounded:
        calls = []
        async def prove_bounded(self, goal, *, budget):
            self.calls.append((goal, budget))
            return open_graph()
    backend = Bounded()
    assert validate('formal-search-request', request) == []
    atp = MathsAIFormalATP(reasoner=backend, production=True,
                          request_compiler=lambda _: deepcopy(authoritative))
    assert atp.formal_search_start(request)['disposition'] == 'budget-exhausted'
    assert backend.calls == [(request['goal_text'], request['budget'])]
    changed = {**request, 'goal_text': 'False'}
    result = atp.formal_search_start(changed)
    assert result['disposition'] == 'invalid-request'
    assert result['artifacts'][0]['note'] == 'request-does-not-match-authoritative-context'
    assert len(backend.calls) == 1
    unbounded = MathsAIFormalATP(reasoner=StubReasoner(open_graph()), production=True,
                               request_compiler=lambda _: authoritative)
    assert unbounded.formal_search_start(request)['artifacts'][0]['note'] == 'backend-does-not-support-bounded-execution'


def test_production_search_timeout_does_not_create_a_closure():
    import asyncio
    request = {key: value for key, value in REQUEST.items() if key != 'hypotheses'}
    request['budget'] = {'max_steps': 1, 'wall_clock_ms': 10}
    class Hanging:
        stopped = False
        async def prove_bounded(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                self.stopped = True
    backend = Hanging()
    result = MathsAIFormalATP(reasoner=backend, production=True,
        request_compiler=lambda _: request).formal_search_start(request)
    assert backend.stopped
    assert result['disposition'] == 'internal-error' and result['states'] == []
    assert 'certificate' not in result
