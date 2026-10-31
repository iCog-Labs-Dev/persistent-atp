"""LLM-backed informal-to-Lean formalization worker.

Formalization produces a draft declaration only. Statement alignment and
Lean replay remain separate, authoritative steps before the declaration can
be searched or treated as verified.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.vocab import DeclarationStatus, WorkerClass
from context_compiler.formalization import FormalizationCompilation

from .llm_client import LLMClientProtocol, extract_json_from_text

__all__ = ["FormalizationResult", "FormalizerWorker", "FORMALIZER_SYSTEM_PROMPT"]

FORMALIZER_SYSTEM_PROMPT = """You translate an informal mathematical claim into a Lean 4 proposition.
Return exactly one JSON object with fields lean_name, lean_type, module_path,
universe_level, and notes. lean_type is the proposition only; do not provide a
proof, tactic script, sorry, axiom, theorem body, or claim that Lean accepted
it. Preserve the claim's quantifiers, domains, assumptions, and strength. Use
only names and assumptions present in the supplied context. If the claim is
ambiguous or the context is insufficient, set lean_type to an empty string and
explain the issue in notes. Do not invent missing assumptions.
"""

_LEAN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")
_DECLARATION_ID = re.compile(r"^[^/]+/fd-[1-9][0-9]*$")
_LEASE_ID = re.compile(r"^[^/]+/ls-[1-9][0-9]*$")
_PROOF_BODY = re.compile(r"(?:\bby\b|:=|\bsorry\b|\badmit\b|\baxiom\b)")


@dataclass(frozen=True, slots=True)
class FormalizationResult:
    """A candidate Lean proposition and its inert gate proposal."""

    declaration_id: str
    claim_id: str
    lean_name: str
    lean_type: str
    module_path: str | None
    universe_level: str | None
    notes: str
    proposal: Proposal


class FormalizerWorker:
    """Generate one draft declaration from a compiled formalization packet."""

    def __init__(self, llm_client: LLMClientProtocol, *, actor: str) -> None:
        if not actor.strip():
            raise ValueError("actor must be a non-empty string")
        self.llm_client = llm_client
        self.actor = actor

    def run(
        self,
        compilation: FormalizationCompilation,
        *,
        declaration_id: str,
        claim_id: str,
        base_revision: int,
        lease_id: str,
        fencing_token: int,
        module_path: str | None = None,
    ) -> FormalizationResult:
        if not isinstance(compilation, FormalizationCompilation):
            raise TypeError("compilation must be FormalizationCompilation")
        if not _DECLARATION_ID.fullmatch(declaration_id):
            raise ValueError("declaration_id must look like <proof>/fd-<n>")
        if not declaration_id.startswith(f"{compilation.result.packet.proof_id}/"):
            raise ValueError("declaration_id must belong to the compiled proof")
        if claim_id != compilation.payload["claim"]["id"]:
            raise ValueError("claim_id must match the compiled target claim")
        if not claim_id.startswith(f"{compilation.result.packet.proof_id}/"):
            raise ValueError("claim_id must belong to the compiled proof")
        if (
            isinstance(base_revision, bool)
            or not isinstance(base_revision, int)
            or base_revision < 0
        ):
            raise ValueError("base_revision must be a non-negative integer")
        if base_revision != compilation.result.packet.base_revision:
            raise ValueError("base_revision must match the compiled packet revision")
        if not isinstance(lease_id, str) or not _LEASE_ID.fullmatch(lease_id):
            raise ValueError("lease_id must look like <proof>/ls-<n>")
        if not lease_id.startswith(f"{compilation.result.packet.proof_id}/"):
            raise ValueError("lease_id must belong to the compiled proof")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token must be a positive integer")

        raw = self.llm_client.generate(
            FORMALIZER_SYSTEM_PROMPT,
            "Compile this context packet into a Lean 4 proposition:\n\n"
            + compilation.context_text,
        )
        candidate = extract_json_from_text(raw)
        lean_name = _required_text(candidate, "lean_name")
        lean_type = _required_text(candidate, "lean_type")
        notes = candidate.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError("notes must be a string")
        if not _LEAN_NAME.fullmatch(lean_name):
            raise ValueError("lean_name must be a simple Lean identifier")
        if _PROOF_BODY.search(lean_type):
            raise ValueError("lean_type must not contain a proof, axiom, or sorry")
        if "lean_value" in candidate or "proof" in candidate:
            raise ValueError("formalizer output must contain a proposition only")

        output_module = candidate.get("module_path", module_path)
        if output_module is not None and (
            not isinstance(output_module, str) or not output_module.strip()
        ):
            raise ValueError("module_path must be a non-empty string when present")
        universe = candidate.get("universe_level")
        if universe is not None and (
            not isinstance(universe, str) or not universe.strip()
        ):
            raise ValueError("universe_level must be a non-empty string when present")

        proposal = Proposal(
            proof_id=compilation.result.packet.proof_id,
            actor=self.actor,
            worker_class=WorkerClass.FORMALIZER.value,
            base_revision=base_revision,
            lease_id=lease_id,
            fencing_token=fencing_token,
            ops=(
                UpsertNode(
                    "FormalDeclaration",
                    declaration_id,
                    {
                        "status": DeclarationStatus.DRAFT.value,
                        "lean_name": lean_name,
                        "lean_type": lean_type,
                        "module_path": output_module,
                        "universe_level": universe,
                    },
                ),
                AddEdge(
                    rel_type="DRAFTS_CLAIM",
                    src_id=declaration_id,
                    dst_id=claim_id,
                    edge_id=_draft_edge_id(declaration_id, claim_id),
                ),
            ),
        )
        return FormalizationResult(
            declaration_id=declaration_id,
            claim_id=claim_id,
            lean_name=lean_name,
            lean_type=lean_type,
            module_path=output_module,
            universe_level=universe,
            notes=notes,
            proposal=proposal,
        )


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _draft_edge_id(declaration_id: str, claim_id: str) -> str:
    seed = f"drafts-claim\0{declaration_id}\0{claim_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(seed).hexdigest()
