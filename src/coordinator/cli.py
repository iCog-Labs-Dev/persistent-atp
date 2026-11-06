"""Small journal-backed command line entry point for coordinator runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from alignment.reviewer import LLMAlignmentReviewer
from alignment.worker import AlignmentWorker
from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.dispatch import ScriptedDispatcher
from mathproof.formal_atp import FakeFormalATP
from mathproof.lean_statement import LeanStatementChecker
from mathproof.workers import CriticWorker, FormalizerWorker, ResearchWorker
from mork.backend import MorkUnavailable
from shared.llm_client import GeminiLLMClient, LLMClientProtocol

from .environment import LeanEnvironment
from .portfolio import AGENT_PORTFOLIO, PORTFOLIO, frontier_by_category
from .runlog import EventSink, RunLogger
from .workspace import ProofWorkspace, project


def initialize(
    journal: Path, proof_id: str, statement: str, first_task: str,
    assumptions: Sequence[str] = (),
) -> int:
    """Seed one theorem and its first research move through the commit gate."""
    if not statement.strip() or not first_task.strip():
        raise ValueError("statement and first task must be nonempty")
    if any(not item.strip() for item in assumptions):
        raise ValueError("assumptions must be nonempty")
    journal.parent.mkdir(parents=True, exist_ok=True)
    store = JournalStore(str(journal))
    if store.head(proof_id)[0] != 0:
        raise ValueError(f"proof {proof_id!r} already has journal events")
    claim = f"{proof_id}/theorem"
    state = f"{proof_id}/rs-1"
    move = f"{proof_id}/rm-1"
    lease_id = f"{proof_id}/init"
    token = store.acquire_lease(proof_id, lease_id, actor="init", worker_class="maintenance")
    try:
        result = CommitGate(MemoryView(), store).commit(Proposal(
            proof_id=proof_id,
            actor="init",
            worker_class="maintenance",
            base_revision=0,
            lease_id=lease_id,
            fencing_token=token,
            ops=(
                UpsertNode("Proof", f"{proof_id}/proof", {
                    "title": statement.strip(), "assumptions": list(assumptions),
                }),
                UpsertNode("Claim", claim, {
                    "status": "conjectural", "claim_text": statement.strip(),
                    "assumptions": list(assumptions),
                }),
                UpsertNode("ResearchState", state, {"status": "open"}),
                UpsertNode("ResearchMove", move, {
                    "status": "queued", "state_id": state, "detail": first_task.strip(),
                }),
                AddEdge("HAS_TARGET", f"{proof_id}/proof", claim, f"{proof_id}/target"),
                AddEdge("PROPOSES", state, move, f"{proof_id}/first-proposal"),
                AddEdge("MOVE_TARGETS", move, claim, f"{proof_id}/first-target"),
            ),
        ))
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=token)
    if not result.accepted:
        raise ValueError("initial proposal rejected: " + ", ".join(
            rejection.reason.value for rejection in result.rejections
        ))
    return result.revision


def inspect(journal: Path, proof_id: str) -> dict:
    """Read the durable journal without needing MORK or an LLM."""
    if not journal.is_file():
        raise ValueError(f"journal does not exist: {journal}")
    store = JournalStore(str(journal))
    view = MemoryView()
    revision = project(view, store, proof_id)
    if revision == 0:
        raise ValueError(f"proof {proof_id!r} has no journal events")
    from .progress import read_proof

    reading = read_proof(view, proof_id, revision)
    replays = view.list_nodes(proof_id, "LeanReplay")
    return {
        "proof_id": proof_id,
        "revision": revision,
        "verified_chain": store.verify_chain(proof_id),
        "progress": list(reading.progress),
        "verification_mode": (
            "test" if any(replay.fields.get("test_mode") is True for replay in replays)
            else "simulated" if any(replay.fields.get("simulated") is True for replay in replays)
            else "replayed" if replays
            else "not-replayed"
        ),
        "frontier": frontier_by_category(
            ProofWorkspace(proof_id, lambda: JournalStore(str(journal)), ScriptedDispatcher({}), view=view),
            store,
        ),
    }


def run_proof(
    journal: Path,
    proof_id: str,
    *,
    rounds: int,
    backend: str,
    lean_project: Path | None,
    llm: LLMClientProtocol | None = None,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
    fake_formal: bool = False,
    test_mode: bool = False,
    log_agent_responses: bool = False,
) -> dict:
    """Run configured agents over one proof, then summarize journal state."""
    if rounds < 1:
        raise ValueError("rounds must be positive")
    if not journal.is_file() or JournalStore(str(journal)).head(proof_id)[0] == 0:
        raise ValueError(f"proof {proof_id!r} has no journal events")
    if backend == "mork":
        from mork.backend import MorkSpace, MorkView
        view = MorkView(MorkSpace())
    elif backend == "memory":
        view = MemoryView()
    else:
        raise ValueError(f"unknown backend: {backend}")
    if lean_project is not None and not lean_project.is_dir():
        raise ValueError(f"Lean project does not exist: {lean_project}")
    if fake_formal and test_mode:
        raise ValueError("choose either --fake-formal or --test-mode, not both")
    if (fake_formal or test_mode) and lean_project is None:
        raise ValueError("fake formal execution requires --lean-project for environment pinning")

    try:
        from .graph import Coordinator
    except ImportError as exc:
        if exc.name and exc.name.startswith("langgraph"):
            raise ValueError("install the coordinator extra to run: uv sync --extra coordinator") from exc
        raise
    client = llm or GeminiLLMClient()
    handlers = {
        "llm-research": ResearchWorker(client).run,
        "critic": CriticWorker(client).run,
    }
    adapters = {}
    environment = None
    if lean_project is not None:
        environment = LeanEnvironment.discover(lean_project)
        handlers["formalizer"] = FormalizerWorker(client).run
        adapters = {
            "alignment-reviewer": AlignmentWorker(
                actor="alignment-reviewer",
                reviewer=LLMAlignmentReviewer(client),
            ),
            "lean-statement-checker": LeanStatementChecker(lean_project),
        }
        if fake_formal or test_mode:
            fake = FakeFormalATP(
                default_disposition="proved-pending-replay",
                test_mode=test_mode,
            )
            adapters.update({"formal-atp": fake, "replayer": fake})
            slots = AGENT_PORTFOLIO
        else:
            slots = tuple(
                slot for slot in AGENT_PORTFOLIO
                if slot.category not in {"formal-run", "replay-task"}
            )
    else:
        slots = tuple(
            slot for slot in PORTFOLIO
            if slot.category not in {"formal-run", "replay-task"}
        )

    workspace = ProofWorkspace(
        proof_id=proof_id,
        open_store=lambda: JournalStore(str(journal)),
        dispatcher=ScriptedDispatcher(handlers),
        adapters=adapters,
        view=view,
        environment=environment,
        event_sink=event_sink,
        log_agent_responses=log_agent_responses,
    )
    if event_sink is not None:
        event_sink("run_started", {
            "proof_id": proof_id,
            "backend": backend,
            "max_rounds": rounds,
            "journal": str(journal),
            "slots": [slot.role for slot in slots],
        })
    try:
        result = Coordinator([workspace], max_rounds=rounds, slots=slots).run()
    except Exception as exc:
        if event_sink is not None:
            event_sink("run_failed", {
                "proof_id": proof_id,
                "error_type": type(exc).__name__,
                "message": (" ".join(str(exc).split()) or type(exc).__name__)[:1000],
            })
        raise
    summary = inspect(journal, proof_id)
    outcome_reason = _outcome_reason(result["outcome"], result["tracks"])
    reported_outcome = result["outcome"]
    if fake_formal and summary["verification_mode"] == "simulated":
        reported_outcome = "simulated-complete"
        outcome_reason = (
            "the fake ATP and fake replay completed; a real replay is still required"
        )
    if test_mode and summary["verification_mode"] == "test":
        reported_outcome = "test-verified"
        outcome_reason = (
            "test mode forced fake ATP and replay acceptance; no real Lean proof was verified"
        )
    output = {
        **summary,
        "outcome": reported_outcome,
        "outcome_reason": outcome_reason,
        "rounds": result["round"],
        "slots": [slot.role for slot in slots],
        "reports": result["reports"],
        "escalations": result["escalations"],
        "milestones": result["milestones"],
    }
    if run_id is not None:
        output["run_id"] = run_id
    if event_sink is not None:
        event_sink("run_completed", {
            "proof_id": proof_id,
            "outcome": output["outcome"],
            "reason": outcome_reason,
            "rounds": output["rounds"],
            "revision": output["revision"],
            "progress": output["progress"],
        })
    return output


def _outcome_reason(outcome: str, tracks: dict) -> str:
    statuses = {track.get("status") for track in tracks.values()}
    if outcome == "solved":
        return "all proofs have replayed declarations"
    if outcome == "budget-exhausted":
        return "the configured round budget was exhausted while work remained"
    if statuses == {"parked"}:
        return "proof certificates are waiting for replay"
    if "stalled" in statuses:
        return (
            "no runnable work remains for the configured agents; "
            "formal ATP and certificate replay are not configured by this command"
        )
    return "no proof remains active"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="persistent-atp")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "run"):
        command = commands.add_parser(name)
        command.add_argument("--journal", type=Path, required=True)
        command.add_argument("--proof-id", required=True)
        if name == "init":
            command.add_argument("--statement", required=True)
            command.add_argument("--first-task", default="Investigate the theorem and propose a testable next step")
            command.add_argument("--assumption", action="append", default=[])
        if name == "run":
            command.add_argument("--rounds", type=int, default=3)
            command.add_argument("--backend", choices=("memory", "mork"), default="memory")
            command.add_argument("--lean-project", type=Path)
            command.add_argument(
                "--fake-formal", action="store_true",
                help="exercise fake ATP and replay; never reports a real verified proof",
            )
            command.add_argument(
                "--test-mode", action="store_true",
                help="force fake ATP and replay acceptance to test the terminal workflow",
            )
            command.add_argument(
                "--verbose", action="store_true",
                help="print live progress events to stderr",
            )
            command.add_argument(
                "--log-file", type=Path,
                help="append structured runtime events as JSONL",
            )
            command.add_argument(
                "--log-agent-responses", action="store_true",
                help="log normalized agent responses without prompts or context packets",
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            output = {"proof_id": args.proof_id, "revision": initialize(
                args.journal, args.proof_id, args.statement, args.first_task,
                args.assumption,
            )}
        elif args.command == "status":
            output = inspect(args.journal, args.proof_id)
        else:
            with RunLogger(verbose=args.verbose, log_file=args.log_file) as run_log:
                output = run_proof(
                    args.journal, args.proof_id, rounds=args.rounds,
                    backend=args.backend, lean_project=args.lean_project,
                    event_sink=run_log.emit, run_id=run_log.run_id,
                    fake_formal=args.fake_formal,
                    test_mode=args.test_mode,
                    log_agent_responses=args.log_agent_responses,
                )
    except (ValueError, OSError, MorkUnavailable) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0
