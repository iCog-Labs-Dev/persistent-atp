"""Translate one committed claim into an unverified Lean statement draft."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from commit_gate.vocab import WorkerClass
from shared.llm_client import LLMClientProtocol


FORMALIZER_PROMPT = """You translate one mathematical claim into a Lean 4 proposition, not a proof. Preserve every quantifier, domain, hypothesis, and definition in the supplied claim and theorem context. Use only dependencies already verified in the packet. Do not weaken the claim to True, add axioms, or invent definitions, imports, or lemma names. If the statement cannot be faithfully translated with the available context, explain the blocker and return null for lean_type.

Return one JSON object with exactly: {"lean_type": string|null, "imports": [string], "reasoning": string}. lean_type is a proposition expression, not a complete theorem or tactic script. Imports must be known Lean modules; do not include code in them. Never emit sorry or admit."""

PROMPT_VERSION = "formalizer-v1"
_IMPORT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
_UNSAFE = re.compile(r"\b(?:sorry|admit|axiom|unsafe|run_tac|set_option|by)\b|#|;|\n", re.I)


class FormalizerWorker:
    worker_class = WorkerClass.FORMALIZER.value

    def __init__(self, llm: LLMClientProtocol):
        self.llm = llm

    def run(self, lease: Any, packet: Mapping[str, Any]) -> dict[str, Any]:
        claim = packet.get("claim")
        if (
            lease.worker_class != self.worker_class
            or packet.get("role") != "formalizer"
            or not isinstance(claim, Mapping)
            or claim.get("id") != lease.selected_move_id
            or not claim.get("fields", {}).get("claim_text")
        ):
            raise ValueError("formalizer needs the leased claim and its statement")
        answer = self.llm.generate_json(FORMALIZER_PROMPT, json.dumps(packet, sort_keys=True))
        if set(answer) != {"lean_type", "imports", "reasoning"}:
            raise ValueError("formalizer result has the wrong fields")
        reasoning = answer["reasoning"]
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("formalizer result needs reasoning")
        lean_type = answer["lean_type"]
        if lean_type is None:
            raise ValueError(f"formalizer could not translate claim: {reasoning.strip()}")
        if not isinstance(lean_type, str) or not lean_type.strip() or _UNSAFE.search(lean_type):
            raise ValueError("formalizer returned an unsafe or empty Lean proposition")
        imports = answer["imports"]
        if not isinstance(imports, list) or not imports or any(
            not isinstance(item, str) or not _IMPORT.fullmatch(item) for item in imports
        ):
            raise ValueError("formalizer needs valid Lean imports")
        local = claim["id"].rsplit("/", 1)[-1]
        name = "clm_" + local.replace("-", "_")
        namespace = "Proof_" + hashlib.sha256(lease.proof_id.encode()).hexdigest()[:12]
        source = (
            "\n".join(f"import {item}" for item in dict.fromkeys(imports))
            + f"\n\nnamespace {namespace}\ntheorem {name} : {lean_type.strip()}\nend {namespace}\n"
        )
        return {
            "lean_name": name,
            "namespace": namespace,
            "lean_type": lean_type.strip(),
            "imports": list(dict.fromkeys(imports)),
            "statement_source": source,
            "reasoning": reasoning.strip(),
            "prompt_version": PROMPT_VERSION,
            "context_digest": packet["manifest_digest"],
            "model_output": answer,
        }
