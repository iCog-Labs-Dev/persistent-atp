"""Opt-in runtime logs expose agent semantics, never their private inputs."""

import json

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from coordinator.cli import initialize
from coordinator.portfolio import Slot, run_portfolio
from coordinator.workspace import ProofWorkspace, project
from mathproof.dispatch import ScriptedDispatcher
from mathproof.workers import ResearchWorker
from shared.llm_client import MockLLMClient


def _candidate(proof_id):
    return {
        "moves": [],
        "candidate_argument": {
            "claim_id": f"{proof_id}/theorem",
            "argument": "Zero is the additive identity for natural numbers.",
            "prerequisite_claim_ids": [],
        },
        "reasoning": "The target has a direct argument.",
    }


def _run_research(journal, proof_id, llm, events, *, enabled):
    store = JournalStore(str(journal))
    view = MemoryView()
    project(view, store, proof_id)
    workspace = ProofWorkspace(
        proof_id=proof_id,
        open_store=lambda: JournalStore(str(journal)),
        dispatcher=ScriptedDispatcher({"llm-research": ResearchWorker(llm).run}),
        view=view,
        event_sink=lambda event, fields: events.append({"event": event, **fields}),
        log_agent_responses=enabled,
    )
    return run_portfolio(
        workspace, store,
        slots=(Slot("explore", "research-move", "llm-research"),),
        round_number=1,
    )


def test_agent_responses_are_absent_by_default_and_safe_when_enabled(tmp_path):
    for enabled in (False, True):
        proof_id = f"logging-{enabled}"
        journal = tmp_path / f"{proof_id}.sqlite"
        initialize(journal, proof_id, "For every natural n, n + 0 = n", "Prove it directly")
        events = []

        _run_research(
            journal, proof_id, MockLLMClient(default_json=_candidate(proof_id)),
            events, enabled=enabled,
        )

        responses = [event for event in events if event["event"] == "agent_response"]
        if not enabled:
            assert responses == []
            continue
        assert len(responses) == 1
        response = responses[0]
        assert response["proof_id"] == proof_id
        assert response["round"] == 1
        assert response["slot"] == "explore"
        assert response["worker_class"] == "llm-research"
        assert response["validation_status"] == "valid"
        serialized = json.dumps(response).lower()
        for forbidden in (
            "system_prompt", "user_prompt", "context_packet", "context_text",
            "api_key", "http", "manifest_digest", "model_output",
        ):
            assert forbidden not in serialized


def test_invalid_research_response_is_logged_with_validation_error(tmp_path):
    proof_id = "invalid-logging"
    journal = tmp_path / "invalid.sqlite"
    initialize(journal, proof_id, "For every natural n, n + 0 = n", "Prove it directly")
    duplicate = {
        "moves": [
            {
                "detail": "Try induction", "why_it_helps": "Expose structure",
                "first_test": "Check zero", "technique_tag": "induction",
                "prerequisite_claim_ids": [],
            },
            {
                "detail": "Try induction again", "why_it_helps": "Use recursion",
                "first_test": "Check successor", "technique_tag": "induction",
                "prerequisite_claim_ids": [],
            },
        ],
        "reasoning": "Two induction routes",
    }
    events = []

    _run_research(
        journal, proof_id, MockLLMClient(responses=[duplicate, duplicate]),
        events, enabled=True,
    )

    (response,) = [event for event in events if event["event"] == "agent_response"]
    assert response["validation_status"] == "invalid"
    assert "distinct technique tags" in response["validation_error"]
    assert len(response["response"]["moves"]) == 2
    serialized = json.dumps(response).lower()
    assert "context_text" not in serialized
    assert "system_prompt" not in serialized


def test_cli_flag_enables_agent_response_logging(tmp_path, monkeypatch, capsys):
    from coordinator import cli

    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return {"outcome": "test"}

    monkeypatch.setattr(cli, "run_proof", fake_run)

    assert cli.main([
        "run", "--journal", str(tmp_path / "proof.sqlite"),
        "--proof-id", "p1", "--log-agent-responses",
    ]) == 0
    assert captured["log_agent_responses"] is True
    assert json.loads(capsys.readouterr().out)["outcome"] == "test"
