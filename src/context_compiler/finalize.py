import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import StrEnum
from typing import Any

from .contracts import CompiledResult, ContextPacket, SelectionDecision
from .errors import ContextValidationError


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def finalize_packet(
    packet: ContextPacket,
    selection_decisions: tuple[SelectionDecision, ...],
    *,
    compiler_version: str,
    rendering_version: str,
) -> CompiledResult:
    """Attach a deterministic digest and provenance manifest to a packet."""
    if not isinstance(packet, ContextPacket):
        raise ContextValidationError("packet must be a ContextPacket")
    if not isinstance(selection_decisions, tuple):
        raise ContextValidationError(
            "selection_decisions must be a tuple"
        )
    if not all(
        isinstance(decision, SelectionDecision)
        for decision in selection_decisions
    ):
        raise ContextValidationError(
            "selection_decisions must contain SelectionDecision values"
        )
    for name, value in (
        ("compiler_version", compiler_version),
        ("rendering_version", rendering_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ContextValidationError(
                f"{name} must be a non-empty string"
            )

    packet_digest = "sha256:" + hashlib.sha256(
        _canonical_json(packet)
    ).hexdigest()
    budgets: dict[str, object] = {}
    if packet.text_budget is not None:
        budgets["text_tokens"] = packet.text_budget.max_tokens
    if packet.formal_limits is not None:
        budgets["formal_execution"] = {
            "max_steps": packet.formal_limits.max_steps,
            "wall_clock_ms": packet.formal_limits.wall_clock_ms,
        }

    manifest = {
        "task_id": packet.task_id,
        "proof_id": packet.proof_id,
        "base_revision": packet.base_revision,
        "packet_kind": packet.packet_kind.value,
        "sources": [
            {
                "source_id": decision.source_id,
                "source_version": decision.source_version,
                "status": decision.status,
                "included": decision.included,
                "reason": decision.reason,
            }
            for decision in selection_decisions
        ],
        "compiler_version": compiler_version,
        "rendering_version": rendering_version,
        "budgets": budgets,
        "packet_digest": packet_digest,
    }
    return CompiledResult(
        packet=packet,
        selection_decisions=selection_decisions,
        manifest=manifest,
        packet_digest=packet_digest,
    )