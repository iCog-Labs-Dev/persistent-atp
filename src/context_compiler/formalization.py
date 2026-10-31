"""Budgeted, revision-bound context for informal-to-Lean formalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from shared.vocab import ClaimStatus, WorkerClass

from .budgeting import CharHeuristicTokenCounter, Section, TokenCounter, fit_sections
from .contracts import (
    CompileRequest,
    CompiledResult,
    ContextPacket,
    PacketKind,
    SelectionDecision,
)
from .errors import ContextValidationError
from .finalize import finalize_packet

__all__ = ["FormalizationCompilation", "compile_formalization"]


@dataclass(frozen=True)
class FormalizationCompilation:
    """Compiled context, parsed payload, and selection manifest for one claim."""

    result: CompiledResult
    payload: Mapping[str, Any]

    @property
    def packet_digest(self) -> str:
        return self.result.packet_digest

    @property
    def context_text(self) -> str:
        return self.result.packet.content

    @property
    def manifest(self) -> Mapping[str, object]:
        return self.result.manifest


def _claim_record(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextValidationError(f"{name} must be an object")
    identity, fields = value.get("id"), value.get("fields")
    if not isinstance(identity, str) or not identity.strip():
        raise ContextValidationError(f"{name}.id must be a non-empty string")
    if value.get("label") != "Claim":
        raise ContextValidationError(f"{name} must be a Claim")
    if not isinstance(fields, Mapping):
        raise ContextValidationError(f"{name}.fields must be an object")
    return value


def compile_formalization(
    request: CompileRequest,
    *,
    claim: Mapping[str, Any],
    dependencies: Sequence[Mapping[str, Any]],
    theorem: Mapping[str, Any],
    token_counter: TokenCounter | None = None,
    compiler_version: str = "formalization/1",
) -> FormalizationCompilation:
    """Compile an informal claim and its mandatory, verified context.

    Target text, theorem context, and every declared dependency are mandatory.
    If they exceed the configured budget, compilation fails rather than
    silently omitting mathematical assumptions or dependencies.
    """
    if not isinstance(request, CompileRequest):
        raise ContextValidationError("request must be a CompileRequest")
    if request.packet_kind is not PacketKind.FORMALIZATION:
        raise ContextValidationError("request packet_kind must be formalization")
    if request.worker_class is not WorkerClass.FORMALIZER:
        raise ContextValidationError("formalization requires the formalizer worker class")
    if request.text_budget is None:
        raise ContextValidationError("formalization requires a text budget")

    claim = _claim_record("claim", claim)
    claim_id = claim["id"]
    if not claim_id.startswith(f"{request.proof_id}/"):
        raise ContextValidationError("formalization claim is outside the proof")
    claim_fields = claim["fields"]
    statement = claim_fields.get("claim_text")
    if not isinstance(statement, str) or not statement.strip():
        raise ContextValidationError("formalization claim has no claim_text")
    try:
        claim_status = ClaimStatus(claim_fields.get("status")).value
    except (TypeError, ValueError) as exc:
        raise ContextValidationError("formalization claim has an invalid status") from exc
    if claim_status in {
        ClaimStatus.TAINTED.value,
        ClaimStatus.REFUTED.value,
        ClaimStatus.RETRACTED.value,
        ClaimStatus.STALE.value,
    }:
        raise ContextValidationError(
            f"formalization target claim is not usable ({claim_status})"
        )
    if not isinstance(theorem, Mapping) or not theorem:
        raise ContextValidationError("formalization requires theorem context")

    normalized_dependencies: list[dict[str, Any]] = []
    decisions = [
        SelectionDecision(
            source_id=claim_id,
            source_version=str(request.base_revision),
            status=claim_status,
            included=True,
            reason="leased target claim",
        )
    ]
    seen = {claim_id}
    for index, raw in enumerate(dependencies):
        dependency = _claim_record(f"dependencies[{index}]", raw)
        dependency_id = dependency["id"]
        if not dependency_id.startswith(f"{request.proof_id}/"):
            raise ContextValidationError("dependency is outside the proof")
        if dependency_id in seen:
            raise ContextValidationError("formalization sources contain duplicate claims")
        seen.add(dependency_id)
        fields = dependency["fields"]
        if fields.get("status") != ClaimStatus.LEAN_VERIFIED.value:
            raise ContextValidationError(
                f"dependency {dependency_id} is not Lean verified"
            )
        text = fields.get("claim_text")
        if not isinstance(text, str) or not text.strip():
            raise ContextValidationError(
                f"verified dependency {dependency_id} has no claim_text"
            )
        normalized_dependencies.append(dict(dependency))
        decisions.append(
            SelectionDecision(
                source_id=dependency_id,
                source_version=str(request.base_revision),
                status=ClaimStatus.LEAN_VERIFIED.value,
                included=True,
                reason="verified dependency of target claim",
            )
        )

    payload = {
        "role": "formalizer",
        "task_id": request.task_id,
        "proof_id": request.proof_id,
        "revision": request.base_revision,
        "claim": dict(claim),
        "dependencies": normalized_dependencies,
        "theorem": dict(theorem),
    }
    try:
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ContextValidationError(
            "formalization context must contain JSON-compatible values"
        ) from exc
    fit = fit_sections(
        (
            Section(
                "formalization-context",
                "Formalization context",
                serialized,
                mandatory=True,
            ),
        ),
        request.text_budget,
        token_counter or CharHeuristicTokenCounter(),
        # The formalizer consumes the context as JSON, so omit prose headings.
        render=lambda sections: "\n\n".join(section.body for section in sections),
    )
    packet = ContextPacket(
        task_id=request.task_id,
        proof_id=request.proof_id,
        base_revision=request.base_revision,
        packet_kind=PacketKind.FORMALIZATION,
        content=fit.text,
        text_budget=request.text_budget,
    )
    result = finalize_packet(
        packet,
        tuple(decisions),
        compiler_version=compiler_version,
        rendering_version="canonical-json/1",
    )
    return FormalizationCompilation(result=result, payload=payload)
