"""Runtime logs expose execution without becoming proof state."""

import io
import json

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from coordinator import cli
from coordinator.cli import initialize
from coordinator.portfolio import Slot
from coordinator.rounds import work
from coordinator.runlog import RunLogger
from coordinator.workspace import ProofWorkspace
from mathproof.dispatch import ScriptedDispatcher
from mathproof.workers import ResearchWorker
from shared.llm_client import MockLLMClient


def test_run_logger_writes_human_progress_and_jsonl(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "events.jsonl"
    with RunLogger(
        verbose=True, log_file=path, run_id="run-1", stream=stream
    ) as logger:
        logger.emit("worker_started", {
            "proof_id": "p1", "round": 2, "slot": "explore",
            "worker_class": "llm-research", "move_id": "p1/rm-1",
        })

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["run_id"] == "run-1"
    assert record["event"] == "worker_started"
    assert record["timestamp"].endswith("+00:00")
    assert "[worker_started]" in stream.getvalue()
    assert "slot=explore" in stream.getvalue()


def test_one_round_emits_the_runtime_handoff_without_prompt_content(tmp_path):
    journal = tmp_path / "journal.sqlite"
    initialize(journal, "p1", "True", "Investigate True")
    events = []
    client = MockLLMClient(default_json={
        "moves": [], "reasoning": "No justified move remains.",
    })
    workspace = ProofWorkspace(
        proof_id="p1",
        open_store=lambda: JournalStore(str(journal)),
        dispatcher=ScriptedDispatcher({
            "llm-research": ResearchWorker(client).run,
        }),
        view=MemoryView(),
        event_sink=lambda event, fields: events.append({"event": event, **fields}),
    )

    report = work(
        workspace,
        1,
        slots=(Slot("explore", "research-move", "llm-research"),),
    )

    assert report["slots"][0]["accepted"]
    names = [event["event"] for event in events]
    assert names == [
        "round_started",
        "slot_started",
        "lease_issued",
        "worker_started",
        "context_compiled",
        "worker_completed",
        "proposal_accepted",
        "projection_updated",
        "lease_released",
        "slot_completed",
        "round_work_completed",
    ]
    context = next(event for event in events if event["event"] == "context_compiled")
    assert context["context_digest"].startswith("sha256:")
    serialized = json.dumps(events)
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
    assert "model_output" not in serialized


def test_cli_keeps_result_on_stdout_and_progress_on_stderr(
    tmp_path, capsys, monkeypatch
):
    log_file = tmp_path / "events.jsonl"

    def fake_run(journal, proof_id, **kwargs):
        sink, run_id = kwargs["event_sink"], kwargs["run_id"]
        sink("run_started", {"proof_id": proof_id, "backend": kwargs["backend"]})
        sink("run_completed", {
            "proof_id": proof_id, "outcome": "halted", "revision": 1,
        })
        return {"proof_id": proof_id, "outcome": "halted", "run_id": run_id}

    monkeypatch.setattr(cli, "run_proof", fake_run)

    assert cli.main([
        "run", "--journal", str(tmp_path / "proof.sqlite"),
        "--proof-id", "p1", "--rounds", "1", "--verbose",
        "--log-file", str(log_file),
    ]) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert result["outcome"] == "halted"
    assert result["run_id"] == records[0]["run_id"]
    assert [record["event"] for record in records] == [
        "run_started", "run_completed",
    ]
    assert "[run_started] proof_id=p1 backend=memory" in captured.err
