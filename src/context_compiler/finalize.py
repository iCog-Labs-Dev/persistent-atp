import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import StrEnum
from typing import Any, Mapping

from .contracts import CompiledResult, ContextPacket, SelectionDecision
from .errors import ContextValidationError


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, SelectionDecision):
        payload = {
            "source_id": value.source_id,
            "source_version": value.source_version,
            "status": value.status,
            "included": value.included,
            "reason": value.reason,
        }
        if value.selection_score is not None:
            payload["selection_score"] = value.selection_score
        if value.score_breakdown:
            payload["score_breakdown"] = dict(value.score_breakdown)
        if value.selection_policy is not None:
            payload["selection_policy"] = value.selection_policy
        return payload
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
    extra_budgets: Mapping[str, object] | None = None,
    selection_policy_configs: Mapping[str, Mapping[str, object]] | None = None,
) -> CompiledResult:
    """Attach a deterministic digest and provenance manifest to a packet.

    ``extra_budgets`` records additional selection limits (for example the
    formal item-count limits) in the manifest.  The manifest is provenance
    only; the digest covers the packet, so it is unaffected.
    """
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

    if extra_budgets:
        serialized_extra = _json_value(dict(extra_budgets))
        overlap = budgets.keys() & serialized_extra.keys()
        if overlap:
            raise ContextValidationError(
                "extra_budgets cannot override packet budgets: "
                f"{sorted(overlap)}"
            )
        budgets.update(serialized_extra)

    manifest = {
        "task_id": packet.task_id,
        "proof_id": packet.proof_id,
        "base_revision": packet.base_revision,
        "packet_kind": packet.packet_kind.value,
        "sources": [
            _json_value(decision) for decision in selection_decisions
        ],
        "compiler_version": compiler_version,
        "rendering_version": rendering_version,
        "budgets": budgets,
        "packet_digest": packet_digest,
    }
    if selection_policy_configs is not None:
        if not isinstance(selection_policy_configs, Mapping):
            raise ContextValidationError("selection_policy_configs must be a mapping")
        for name, config in selection_policy_configs.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(config, Mapping):
                raise ContextValidationError("selection policy configs need names and mapping values")
    else:
        selection_policy_configs = {}
    policies = sorted({
        decision.selection_policy
        for decision in selection_decisions
        if decision.selection_policy is not None
    } | set(selection_policy_configs))
    if policies:
        manifest["selection_policies"] = policies
    if selection_policy_configs:
        manifest["selection_policy_configs"] = _json_value(dict(selection_policy_configs))
    return CompiledResult(
        packet=packet,
        selection_decisions=selection_decisions,
        manifest=manifest,
        packet_digest=packet_digest,
    )
