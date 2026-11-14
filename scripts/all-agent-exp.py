#!/usr/bin/env python3
"""Run an inspectable Gemini-to-MORK workflow with real or test ATP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]


def _ensure_mork_preloaded() -> None:
    library = os.environ.get("MORK_LIBRARY", "")
    preloaded = os.environ.get("LD_PRELOAD", "").split(":")
    if library and library in preloaded:
        return
    wrapper = REPO / "scripts" / "with-mork.sh"
    os.execv(
        str(wrapper),
        [str(wrapper), sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def _default_lean_project() -> Path | None:
    candidates = (
        REPO.parent.parent / "Pantograph",
        REPO.parent.parent / "OmegaClaw-Core" / "lean",
    )
    return next((path for path in candidates if (path / "lake-manifest.json").is_file()), None)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Gemini agents and Lean/MORK with real or test ATP/replay."
    )
    parser.add_argument("--output-root", type=Path, default=REPO / "runs" / "all-agent")
    parser.add_argument("--run-id", help="optional artifact directory name; must not already exist")
    parser.add_argument("--lean-project", type=Path, default=_default_lean_project())
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--formal-atp-url", help="use the real Mathematics-AI HTTP API")
    parser.add_argument("--artifact-db-url", help="PostgreSQL artifact store for real ATP")
    parser.add_argument(
        "--statement",
        default="If you add zero to any natural number, you get the same number back.",
        help="ordinary-language theorem supplied to the system",
    )
    return parser.parse_args()


def _load_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_workflow(events: list[dict], result: dict, *, real_atp: bool) -> list[str]:
    if not any(
        event.get("event") == "run_started" and event.get("backend") == "mork"
        for event in events
    ) or not any(
        event.get("event") == "projection_updated"
        and event.get("backend") == "MorkView"
        for event in events
    ):
        raise RuntimeError("the run did not use the live MORK projection")
    responses = [
        event
        for event in events
        if event.get("event") == "agent_response"
    ]
    expected = (
        "llm-research", "critic", "formalizer", "alignment-reviewer", "formal-atp"
    ) if real_atp else (
        "llm-research", "critic", "formalizer", "alignment-reviewer", "formal-atp", "replayer"
    )
    missing = [
        worker for worker in expected
        if not any(event.get("worker_class") == worker for event in responses)
    ]
    if missing:
        raise RuntimeError("expected agents did not run: " + ", ".join(missing))
    if not any(
        event.get("worker_class") == "llm-research"
        and event["response"].get("candidate_argument")
        for event in responses
    ):
        raise RuntimeError("research did not converge to a candidate argument")
    if not any(
        event.get("worker_class") == "critic"
        and event["response"].get("verdict") == "supported"
        for event in responses
    ):
        raise RuntimeError("critic did not support the candidate argument")
    if not any(
        event.get("worker_class") == "formalizer"
        and event["response"].get("lean_type")
        for event in responses
    ):
        raise RuntimeError("formalizer did not produce a Lean proposition")
    if not any(
        event.get("worker_class") == "alignment-reviewer"
        and event["response"].get("verdict") == "aligned"
        for event in responses
    ):
        raise RuntimeError("alignment reviewer did not approve the Lean declaration")
    if not real_atp and not any(
        event.get("worker_class") == "formal-atp"
        and event["response"].get("disposition") == "proved-pending-replay"
        for event in responses
    ):
        raise RuntimeError("test-mode formal ATP did not produce a candidate certificate")
    if not real_atp and not any(
        event.get("worker_class") == "replayer"
        and event["response"].get("verdict") == "verified"
        and event["response"].get("test_mode") is True
        for event in responses
    ):
        raise RuntimeError("test-mode replay did not accept the candidate certificate")
    alignment_checks = [
        event for event in events
        if event.get("event") == "worker_completed"
        and event.get("worker_class") == "alignment-reviewer"
    ]
    if not alignment_checks or alignment_checks[-1].get("statement_checked") is not True:
        raise RuntimeError("real Lean statement elaboration did not succeed")
    if not real_atp and (result.get("outcome") != "test-verified" or result.get("verification_mode") != "test"):
        raise RuntimeError("workflow did not reach the explicitly test-verified outcome")
    return list(expected)


def main() -> int:
    _ensure_mork_preloaded()
    args = _parse_args()
    if args.lean_project is None:
        raise SystemExit("no Lean project found; pass --lean-project")
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not set (it may be placed in .env)")

    from coordinator.cli import initialize, run_proof
    from coordinator.runlog import RunLogger

    run_name = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_name
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise SystemExit(f"experiment artifacts already exist: {run_dir}") from exc

    proof_id = "nat-add-zero-" + uuid4().hex[:12]
    journal = run_dir / "proof.sqlite"
    log_file = run_dir / "events.jsonl"
    initialize(
        journal,
        proof_id,
        args.statement,
        "Investigate the user's statement and take the next justified step.",
    )
    try:
        with RunLogger(verbose=True, log_file=log_file) as logger:
            result = run_proof(
                journal, proof_id, rounds=args.rounds, backend="mork",
                lean_project=args.lean_project.resolve(), event_sink=logger.emit,
                run_id=logger.run_id, test_mode=not bool(args.formal_atp_url),
                formal_atp_url=args.formal_atp_url, log_agent_responses=True,
                artifact_db_url=args.artifact_db_url,
            )
        agents = _assert_workflow(_load_events(log_file), result, real_atp=bool(args.formal_atp_url))
    except Exception as exc:
        raise SystemExit(
            f"experiment failed: {exc}\nartifacts retained at {run_dir}"
        ) from exc

    print(json.dumps({
        "mork_used": True,
        "agents_ran": agents,
        "outcome": result["outcome"],
        "verification_mode": result["verification_mode"],
        "verification_notice": (
            "TEST VERIFICATION ONLY: ATP and replay were faked; this is not a real Lean proof."
            if not args.formal_atp_url else
            "Real ATP used; only an independently accepted provider trace counts as verified."
        ),
        "proof_id": proof_id,
        "input_statement": args.statement,
        "journal": str(journal),
        "log": str(log_file),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
