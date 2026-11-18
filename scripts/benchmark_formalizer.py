#!/usr/bin/env python3
"""Generate and verify a reproducible 50-case ProofNet-Verified sample.

Generate: --dataset DATASET [--limit 50] [--output RESULTS.jsonl]
Verify:   --verify-results RESULTS.jsonl --lean-project PROJECT
          [--output VERIFIED.jsonl]

Only the informal statement, Lean header, and helper definitions reach the
model. The reference formal statement is saved for LeanFlow's TypeCheck and
BEqPlus metrics; it is never included in the model prompt. Verification uses
the dataset project's pinned Lean/Mathlib environment and requires LeanFlow.
Install LeanFlow separately and set up the dataset project before verifying.
BEqPlus proves equivalence in both directions when it succeeds; a false result
means equivalence was not established, not necessarily that it is false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from alignment.formalizer import FormalizerWorker, LLMFormalizerBackend
from alignment.llm_client import GeminiLLMClient, HTTPLLMClient
from context_compiler import CompileRequest, PacketKind, TextBudget, compile_formalization
from dotenv import load_dotenv
from shared.vocab import WorkerClass


class QuotaFallbackClient:
    """Use a second Gemini key only after a quota/rate-limit response."""

    def __init__(self, keys: list[str], model: str) -> None:
        self.clients = [GeminiLLMClient(api_key=key, model=model, max_retries=1) for key in keys]
        self.active = 0
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        while True:
            try:
                return self.clients[self.active].generate(system_prompt, user_prompt)
            except Exception as exc:
                message = str(exc).lower()
                exhausted = any(term in message for term in ("429", "resource_exhausted", "quota", "rate limit"))
                if not exhausted or self.active + 1 == len(self.clients):
                    raise
                self.active += 1
                print("Primary Gemini key exhausted; using fallback key.", file=sys.stderr)


def load_sample(path: Path, limit: int, seed: int) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not all(isinstance(row.get(key), str) for key in ("informal_stmt", "formal_stmt")):
                raise ValueError(f"{path}:{line_number}: missing informal_stmt or formal_stmt")
            rows.append(row)
    if not rows:
        raise ValueError("Dataset is empty")
    return random.Random(seed).sample(rows, min(limit, len(rows)))


def make_compilation(row: dict[str, Any], number: int, budget: int):
    proof_id = f"benchmark-{number}"
    claim_id = f"{proof_id}/c-1"
    request = CompileRequest(
        task_id=f"{proof_id}/task-1",
        proof_id=proof_id,
        base_revision=0,
        packet_kind=PacketKind.FORMALIZATION,
        worker_class=WorkerClass.FORMALIZER,
        text_budget=TextBudget(budget),
    )
    claim = {"id": claim_id, "label": "Claim", "fields": {
        "claim_text": row["informal_stmt"], "status": "conjectural",
    }}
    theorem = {"lean_header": row.get("header") or "import Mathlib"}
    if row.get("helper"):
        theorem["helper_definitions"] = row["helper"]
    return compile_formalization(request, claim=claim, dependencies=(), theorem=theorem)


def make_client(provider: str, model: str | None):
    if provider == "auto":
        provider = "gemini" if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) else "http"
    if provider == "gemini":
        keys = list(dict.fromkeys(filter(None, (os.getenv("GEMINI_API_KEY"), os.getenv("GOOGLE_API_KEY")))))
        if not keys:
            raise ValueError("Set GEMINI_API_KEY (and optionally GOOGLE_API_KEY for fallback)")
        return QuotaFallbackClient(keys, model or os.getenv("GEMINI_MODEL") or "gemini-flash-lite-latest"), provider
    if not (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")):
        raise ValueError("Set LLM_API_KEY or OPENAI_API_KEY")
    selected_model = model or os.getenv("LLM_MODEL")
    if not selected_model:
        raise ValueError("Set --model or LLM_MODEL for the HTTP provider")
    return HTTPLLMClient(model=selected_model), provider


def safe_error(exc: Exception) -> str:
    message = str(exc)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    generated = sum(row.get("status") == "generated" for row in rows)
    durations = [row["seconds"] for row in rows if isinstance(row.get("seconds"), (int, float))]
    summary = {
        "attempted": len(rows),
        "generated": generated,
        "generation_failures": len(rows) - generated,
        "median_seconds": round(statistics.median(durations), 2) if durations else None,
    }
    if any("typecheck" in row for row in rows):
        summary.update(
            typecheck_pass=sum(row.get("typecheck") is True for row in rows),
            typecheck_fail=sum(row.get("typecheck") is False for row in rows),
            typecheck_not_run=sum(row.get("typecheck") is None for row in rows),
            beq_plus_pass=sum(row.get("beq_plus") is True for row in rows),
            beq_plus_not_established=sum(row.get("beq_plus") is False for row in rows),
            beq_plus_not_run=sum(row.get("beq_plus") is None for row in rows),
            verifier_errors=sum("verifier_error" in row for row in rows),
            beq_plus_pass_rate_all=round(
                sum(row.get("beq_plus") is True for row in rows) / len(rows), 3
            ) if rows else None,
        )
    return summary


def verify_results(source: Path, output: Path, lean_project: Path) -> None:
    if not lean_project.is_dir() or not (lean_project / "lean-toolchain").exists():
        raise ValueError("--lean-project must point to the matching ProofNet-Verified Lake project")
    if not (lean_project / ".lake" / "packages" / "mathlib").is_dir():
        raise RuntimeError("Mathlib is missing from --lean-project; run lake update there first")
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("Verification output must be a new file, separate from input")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("Result file is empty")
    dataset_hashes = {row.get("dataset_sha256") for row in rows}
    project_files = (lean_project / "data").rglob("*.jsonl")
    if len(dataset_hashes) != 1 or not any(
        hashlib.sha256(path.read_bytes()).hexdigest() in dataset_hashes for path in project_files
    ):
        raise ValueError("Result dataset does not match a JSONL file in --lean-project")
    try:
        from leanflow import BEqPlus, TypeCheck
    except ImportError as exc:
        raise RuntimeError("Verification needs LeanFlow: pip install leanflow") from exc
    config = {"project_path": str(lean_project.resolve()), "require_mathlib": True}
    typecheck = TypeCheck(repl_config=config)
    beq_plus = BEqPlus(repl_config=config)
    output.parent.mkdir(parents=True, exist_ok=True)
    verified = []
    with output.open("x", encoding="utf-8") as stream:
        for row in rows:
            row = dict(row)
            row["typecheck"] = None
            row["beq_plus"] = None
            if row.get("status") == "generated":
                header = (row.get("header") or "import Mathlib") + "\n" + (row.get("helper") or "")
                reference = row["reference_formal_stmt"]
                candidate = f"theorem benchmark_candidate : {row['lean_type']} := by sorry"
                try:
                    if not typecheck.compute(reference, header=header):
                        row["verifier_error"] = "Reference failed type-check: check Lean project/dataset version"
                    else:
                        row["typecheck"] = bool(typecheck.compute(candidate, header=header))
                        if row["typecheck"]:
                            row["beq_plus"] = bool(beq_plus.compute(reference, candidate, header=header))
                except Exception as exc:
                    row["verifier_error"] = safe_error(exc)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            verified.append(row)
            print(f"Checked {len(verified)}/{len(rows)}: {row.get('name')}", file=sys.stderr)
    print(json.dumps(summarize(verified), indent=2))
    print(f"Verified results: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, help="ProofNet-Verified JSONL file")
    parser.add_argument("--limit", type=int, default=50, help="Maximum examples/API calls (default: 50)")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed (default: 42)")
    parser.add_argument("--budget", type=int, default=2048, help="Maximum context tokens per example")
    parser.add_argument("--provider", choices=("auto", "gemini", "http"), default="auto")
    parser.add_argument("--model", help="Model name; otherwise use provider default/environment")
    parser.add_argument("--output", type=Path, help="New JSONL output path")
    parser.add_argument("--env-file", type=Path, help="Optional .env file containing API keys")
    parser.add_argument("--dry-run", action="store_true", help="Show selected examples without API calls")
    parser.add_argument("--verify-results", type=Path, help="Verify saved results without API calls")
    parser.add_argument("--lean-project", type=Path, help="Matching ProofNet-Verified Lake project for verification")
    args = parser.parse_args()
    if args.verify_results:
        if not args.lean_project:
            parser.error("--verify-results requires --lean-project")
        output = args.output or Path("formalizer_verified.jsonl")
        try:
            verify_results(args.verify_results, output, args.lean_project)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        return 0
    if not args.dataset:
        parser.error("--dataset is required unless --verify-results is used")
    if args.limit < 1 or args.budget < 1:
        parser.error("--limit and --budget must be positive")
    load_dotenv(args.env_file if args.env_file else None)
    rows = load_sample(args.dataset, args.limit, args.seed)
    output = args.output or Path("formalizer_benchmark.jsonl")
    dataset_sha256 = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    if args.dry_run:
        for number, row in enumerate(rows, 1):
            compilation = make_compilation(row, number, args.budget)
            print(json.dumps({"index": row.get("index"), "name": row.get("name"),
                              "packet_digest": compilation.packet_digest}, ensure_ascii=False))
        return 0
    try:
        client, provider = make_client(args.provider, args.model)
    except ValueError as exc:
        parser.error(str(exc))
    worker = FormalizerWorker(
        LLMFormalizerBackend(client), actor="benchmark-formalizer"
    )
    results = []
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error(f"Output already exists: {output}; choose a new --output path")
    with output.open("x", encoding="utf-8") as stream:
        for number, row in enumerate(rows, 1):
            record: dict[str, Any] = {
                "index": row.get("index"), "name": row.get("name"),
                "textbook": row.get("textbook"), "informal_stmt": row["informal_stmt"],
                "reference_formal_stmt": row["formal_stmt"],
                "header": row.get("header") or "import Mathlib", "helper": row.get("helper") or "",
                "provider": provider, "model": args.model or getattr(client, "model", None),
                "dataset_sha256": dataset_sha256, "selection_seed": args.seed,
                "context_budget": args.budget,
            }
            start = time.monotonic()
            try:
                compilation = make_compilation(row, number, args.budget)
                if row["formal_stmt"] in compilation.context_text:
                    raise ValueError("Reference formal statement appeared in model context")
                record["packet_digest"] = compilation.packet_digest
                result = worker.run(
                    compilation, declaration_id=f"benchmark-{number}/fd-1",
                    claim_id=f"benchmark-{number}/c-1", base_revision=0,
                    lease_id=f"benchmark-{number}/ls-1", fencing_token=1,
                )
                record.update(status="generated", lean_name=result.lean_name,
                              lean_type=result.lean_type, notes=result.notes,
                              module_path=result.module_path,
                              backend_id=result.backend_id)
            except Exception as exc:
                record.update(status="error", error_type=type(exc).__name__, error=safe_error(exc))
            record["seconds"] = round(time.monotonic() - start, 2)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            results.append(record)
            print(f"{number}/{len(rows)} {row.get('name', row.get('index'))}: {record['status']}", file=sys.stderr)
    print(json.dumps(summarize(results), indent=2))
    print(f"Results: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
