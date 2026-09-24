#!/usr/bin/env python3
"""Run two Gemini reviews through CommitGate into live and file MORK.

Run from the repository root with:

    scripts/with-mork.sh .venv/bin/python scripts/alignment-mork-exp.py

Writes inspectable artifacts under ``experiment-output/``. This demonstrates
the workflow; it is not a reviewer benchmark or Lean proof check.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env", override=False)

from alignment.llm_client import GeminiLLMClient
from alignment.models import AlignmentReviewRequest, compute_content_hash
from alignment.reviewer import LLMAlignmentReviewer
from alignment.worker import AlignmentWorker
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore
from mork.backend import MorkSpace, MorkView
from mork.projector import generate_metta_file, project_event_journal


CASES = (
    {
        "name": "matching_add_zero",
        "expected_verdict": "aligned",
        "declaration_id": "decl-add-zero",
        "formal_statement": "theorem add_zero (n : Nat) : n + 0 = n",
    },
    {
        "name": "near_miss_add_one",
        "expected_verdict": "mismatch",
        "declaration_id": "decl-add-one",
        "formal_statement": "theorem add_one_eq_self (n : Nat) : n + 1 = n",
    },
)

INFORMAL_STATEMENT = "For every natural number n, n + 0 = n."


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe(value: Any, secret: str) -> Any:
    """Redact the API key from any text returned by an upstream error."""
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_safe(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item, secret) for key, item in value.items()}
    return value


def _result_dict(result: Any) -> dict[str, Any]:
    return {
        "verdict": result.verdict.value,
        "lifecycle": result.lifecycle.value,
        "reviewer": result.reviewer,
        "reasoning": result.reasoning,
        "criteria": result.criteria.to_dict(),
    }


def _raw_atom(command: str) -> str:
    prefix = "!(add-atom &mork "
    if command.startswith(prefix) and command.endswith(")"):
        return command[len(prefix) : -1]
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", help="Gemini model; defaults to GEMINI_MODEL or gemini-flash-lite-latest")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is missing from the repository .env or process environment")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or REPO_ROOT / "experiment-output" / f"alignment-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("alignment-mork-experiment")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (
        logging.StreamHandler(),
        logging.FileHandler(output_dir / "run.log", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    proof_id = f"alignment-demo-{sha1(stamp.encode()).hexdigest()[:10]}"
    claim_id = f"{proof_id}/claim-add-zero"
    declaration_ids = {
        case["name"]: f"{proof_id}/{case['declaration_id']}" for case in CASES
    }
    alignment_ids = {
        case["name"]: f"{proof_id}/alignment-{case['name']}" for case in CASES
    }
    database_path = output_dir / "journal.sqlite3"
    store = JournalStore(str(database_path))
    view = MorkView(MorkSpace())
    gate = CommitGate(view, store)

    model = args.model or os.getenv("GEMINI_MODEL") or "gemini-flash-lite-latest"
    reviewer = LLMAlignmentReviewer(
        llm_client=GeminiLLMClient(api_key=api_key, model=model, temperature=0.0),
        reviewer_id=f"gemini:{model}",
    )
    worker = AlignmentWorker(actor=f"gemini-alignment-worker:{model}", reviewer=reviewer)
    logger.info("proof_id=%s", proof_id)
    logger.info("model=%s", model)
    logger.info("API key loaded from environment; value is not logged or written")
    logger.info("artifacts=%s", output_dir)

    claim = INFORMAL_STATEMENT
    # Seed the claim and candidate declarations.
    seed = Proposal(
        proof_id=proof_id,
        actor="experiment-coordinator",
        worker_class="coordinator",
        base_revision=0,
        ops=(
            UpsertNode("Claim", claim_id, {"status": "provisional", "statement": claim}),
            *(
                UpsertNode(
                    "FormalDeclaration",
                    declaration_ids[case["name"]],
                    {"exact_hash": compute_content_hash(case["formal_statement"])},
                )
                for case in CASES
            ),
        ),
    )
    seed_result = gate.commit(seed)
    if not seed_result.accepted:
        _json_write(output_dir / "seed-rejections.json", [r.to_dict() for r in seed_result.rejections])
        raise RuntimeError(f"Could not seed experiment graph: {seed_result.rejections}")
    logger.info("seed committed at revision %s", seed_result.revision)

    lease_id = f"{proof_id}-alignment-lease"
    fencing_token = store.acquire_lease(proof_id, lease_id)
    review_records: list[dict[str, Any]] = []
    commit_records: list[dict[str, Any]] = []

    # Review and commit each candidate.
    for case in CASES:
        logger.info("reviewing case=%s expected=%s", case["name"], case["expected_verdict"])
        request = AlignmentReviewRequest(
            proof_id=proof_id,
            claim_id=claim_id,
            declaration_id=declaration_ids[case["name"]],
            informal_statement=INFORMAL_STATEMENT,
            formal_statement=case["formal_statement"],
        )
        started = time.perf_counter()
        result, proposal = worker.run_review(
            request,
            alignment_ids[case["name"]],
            base_revision=store.head(proof_id)[0],
            lease_id=lease_id,
            fencing_token=fencing_token,
        )
        elapsed = time.perf_counter() - started
        review = {
            "case": case["name"],
            "expected_verdict_for_demo": case["expected_verdict"],
            "informal_statement": request.informal_statement,
            "formal_statement": request.formal_statement,
            "declaration_id": request.declaration_id,
            "alignment_id": alignment_ids[case["name"]],
            "statement_hash": request.statement_hash,
            "source_hash": request.source_hash,
            "elapsed_seconds": round(elapsed, 3),
            "review": _result_dict(result),
        }
        expected_relations = {
            "aligned": {"exact", "reformulation"},
            "stronger": {"strengthening"},
            "weaker": {"weakening"},
            "mismatch": {"mismatch"},
        }
        verdict = review["review"]["verdict"]
        relation = review["review"]["criteria"]["relation"]
        if (
            verdict in expected_relations
            and relation not in expected_relations[verdict]
        ):
            review["consistency_warnings"] = [
                f"Verdict {verdict} is inconsistent with relation {relation}."
            ]
        else:
            review["consistency_warnings"] = []
        review = _safe(review, api_key)
        review_records.append(review)
        _json_write(output_dir / "reviews.json", review_records)
        logger.info(
            "  verdict=%s relation=%s elapsed=%.2fs",
            result.verdict.value,
            result.criteria.relation,
            elapsed,
        )
        logger.info("  reasoning=%s", _safe(result.reasoning, api_key))

        if result.lifecycle.value == "review-needed" or result.reasoning.startswith("LLM review failed:"):
            raise RuntimeError(f"Gemini review failed for {case['name']}; see reviews.json")

        commit_result = gate.commit(proposal)
        record = {
            "case": case["name"],
            "accepted": commit_result.accepted,
            "revision": commit_result.revision,
            "event_hash": commit_result.event_hash,
            "rejections": [rejection.to_dict() for rejection in commit_result.rejections],
        }
        commit_records.append(record)
        logger.info(
            "  gate accepted=%s revision=%s",
            commit_result.accepted,
            commit_result.revision,
        )
        if not commit_result.accepted:
            _json_write(output_dir / "gate-results.json", commit_records)
            raise RuntimeError(f"CommitGate refused review {case['name']}; see gate-results.json")

    # Export committed history and its hash links.
    events = [
        {"revision": revision, "payload": payload}
        for revision, payload in enumerate(store.read_events(proof_id), start=1)
    ]
    journal_data = {"proof_id": proof_id, "events": events}
    _json_write(output_dir / "journal.json", journal_data)
    _json_write(output_dir / "gate-results.json", commit_records)
    _json_write(
        output_dir / "journal-chain.json",
        [
            {"revision": revision, "event_hash": event_hash, "previous_hash": previous_hash}
            for revision, event_hash, previous_hash in store.read_chain(proof_id)
        ],
    )

    # Compare replayed file atoms with the live FFI view.
    file_commands = project_event_journal(journal_data)
    file_atoms = sorted(_raw_atom(command) for command in file_commands)
    live_atoms = sorted(
        atom for atom in view.atoms(proof_id) if not atom.startswith("(projected ")
    )
    parity = file_atoms == live_atoms
    (output_dir / "projection.metta").write_text(
        generate_metta_file(file_commands), encoding="utf-8"
    )
    (output_dir / "live_atoms.metta").write_text(
        ";; Snapshot read from the live MORK FFI space\n"
        + "\n".join(live_atoms)
        + "\n",
        encoding="utf-8",
    )
    if not parity:
        missing_live = sorted(set(file_atoms) - set(live_atoms))
        missing_file = sorted(set(live_atoms) - set(file_atoms))
        _json_write(
            output_dir / "parity-difference.json",
            {"missing_from_live": missing_live, "missing_from_file": missing_file},
        )

    consistency_warnings = [
        f"{review['case']}: {warning}"
        for review in review_records
        for warning in review["consistency_warnings"]
    ]
    _json_write(
        output_dir / "summary.json",
        {
            "proof_id": proof_id,
            "model": model,
            "journal_revision": store.head(proof_id)[0],
            "projected_revision": view.projected_revision(proof_id),
            "file_atom_count": len(file_atoms),
            "live_atom_count": len(live_atoms),
            "file_live_parity": parity,
            "review_consistency_warnings": consistency_warnings,
            "review_cases": [
                {
                    "name": item["case"],
                    "expected": item["expected_verdict_for_demo"],
                    "observed": item["review"]["verdict"],
                    "elapsed_seconds": item["elapsed_seconds"],
                }
                for item in review_records
            ],
        },
    )
    logger.info("journal revision=%s projected revision=%s", store.head(proof_id)[0], view.projected_revision(proof_id))
    logger.info("file/live atoms=%s/%s parity=%s", len(file_atoms), len(live_atoms), parity)
    logger.info("output directory=%s", output_dir)
    exit_code = 0 if parity else 2
    store.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
