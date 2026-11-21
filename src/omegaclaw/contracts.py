"""Strict, string-only Omega operation contract."""
from __future__ import annotations

from collections.abc import Mapping

ARGUMENTS = {
    "proof-create": ("spec", "workspace"),
    "proof-cycle": ("proof", "workspace"),
    "proof-status": ("proof", "workspace"),
    "proof-audit": ("proof", "workspace"),
    "proof-export": ("proof", "destination", "workspace"),
    "formal-search": ("proof", "claim", "budget", "workspace"),
    "formal-resume": ("proof", "run", "budget", "workspace"),
    "statement-align": ("proof", "claim", "lean_source", "workspace"),
    "lemma-bridge": ("proof", "obstruction", "workspace"),
}

MAX_ARGUMENT_BYTES = 16_384


class ContractError(ValueError):
    pass


def validate_request(raw: object) -> dict:
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "request_id", "workspace", "operation", "arguments"}:
        raise ContractError("operation envelope has missing or forbidden fields")
    if raw["schema_version"] != 1:
        raise ContractError("unsupported schema version")
    request_id = raw["request_id"]
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in request_id):
        raise ContractError("invalid request_id")
    operation = raw["operation"]
    if operation not in ARGUMENTS:
        raise ContractError("unknown operation")
    args = raw["arguments"]
    if not isinstance(args, Mapping) or set(args) != set(ARGUMENTS[operation]) - {"workspace"}:
        raise ContractError("operation arguments have missing or forbidden fields")
    if not isinstance(raw["workspace"], str) or not raw["workspace"]:
        raise ContractError("invalid workspace")
    if any(not isinstance(v, str) or len(v.encode("utf-8")) > MAX_ARGUMENT_BYTES for v in args.values()):
        raise ContractError("arguments must be bounded strings")
    return dict(raw)
