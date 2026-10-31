#!/usr/bin/env python3
"""Compile one claim and inspect the formalizer's draft proposal.

This demo validates the proposal against an in-memory claim. It does not
submit anything to the journal or claim that Lean accepted the proposition.
"""

from __future__ import annotations

import argparse
import json
import os

from alignment.formalizer import FormalizerWorker
from alignment.llm_client import GeminiLLMClient, HTTPLLMClient, MockLLMClient
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal
from context_compiler import (
    CompileRequest,
    PacketKind,
    TextBudget,
    compile_formalization,
)
from shared.vocab import WorkerClass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim-text",
        default="Every natural number equals itself.",
        help="Informal claim to formalize",
    )
    parser.add_argument(
        "--theorem",
        default="Reflexivity of equality on natural numbers",
        help="Theorem context supplied to the compiler",
    )
    parser.add_argument(
        "--lean-name", default="demo_nat_refl", help="Mock Lean declaration name"
    )
    parser.add_argument(
        "--lean-type", default="∀ n : Nat, n = n", help="Mock Lean proposition"
    )
    parser.add_argument(
        "--budget", type=int, default=2048, help="Maximum context tokens"
    )
    parser.add_argument(
        "--mock", action="store_true", help="Use the fixed sample answer instead of an API"
    )
    parser.add_argument(
        "--provider", choices=("auto", "http", "gemini"), default="auto",
        help="API provider; auto selects from configured keys",
    )
    parser.add_argument("--model", help="Model name (or set LLM_MODEL/GEMINI_MODEL)")
    args = parser.parse_args()

    request = CompileRequest(
        task_id="demo/task-1",
        proof_id="demo",
        base_revision=0,
        packet_kind=PacketKind.FORMALIZATION,
        worker_class=WorkerClass.FORMALIZER,
        text_budget=TextBudget(args.budget),
    )
    claim = {
        "id": "demo/c-1",
        "label": "Claim",
        "fields": {"claim_text": args.claim_text, "status": "conjectural"},
    }
    compilation = compile_formalization(
        request,
        claim=claim,
        dependencies=(),
        theorem={"statement": args.theorem},
    )

    if args.mock:
        client = MockLLMClient(default_json={
            "lean_name": args.lean_name,
            "lean_type": args.lean_type,
            "notes": "Deterministic demo candidate",
        })
    else:
        provider = args.provider
        if provider == "auto":
            if os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"):
                provider = "http"
            elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                provider = "gemini"
            else:
                parser.error(
                    "No API key found. Set LLM_API_KEY/OPENAI_API_KEY or "
                    "GEMINI_API_KEY in the environment or .env; use --mock for a sample."
                )
        if provider == "http":
            if not (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")):
                parser.error("HTTP provider needs LLM_API_KEY or OPENAI_API_KEY")
            model = args.model or os.getenv("LLM_MODEL")
            if not model:
                parser.error("HTTP provider needs --model or LLM_MODEL")
            client = HTTPLLMClient(model=model)
        else:
            if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
                parser.error("Gemini provider needs GEMINI_API_KEY or GOOGLE_API_KEY")
            model = args.model or os.getenv("GEMINI_MODEL") or "gemini-flash-lite-latest"
            client = GeminiLLMClient(model=model)

    result = FormalizerWorker(client, actor="demo-formalizer").run(
        compilation,
        declaration_id="demo/fd-1",
        claim_id="demo/c-1",
        base_revision=0,
        lease_id="demo/ls-1",
        fencing_token=1,
    )
    view = MemoryView()
    view.add_node("demo/c-1", "Claim", claim["fields"])
    findings = validate_proposal(result.proposal, view)

    print(json.dumps({
        "packet_digest": compilation.packet_digest,
        "selection_manifest": compilation.manifest,
        "lean_name": result.lean_name,
        "lean_type": result.lean_type,
        "notes": result.notes,
        "gate_findings": [finding.to_dict() for finding in findings],
        "proposal": result.proposal.to_dict(),
    }, indent=2, ensure_ascii=False))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
