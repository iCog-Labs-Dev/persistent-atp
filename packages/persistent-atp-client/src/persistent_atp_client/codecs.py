"""Canonical request body and strict transport parsing shared with receivers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import Any

from .models import ACTION_TYPES, ActionRequest, ArtifactRef, Budget, Preconditions, QueueResearchMoves, ResearchGuidance, CoordinatorEscalation, QueueCoordinatorEscalations


def _dump(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        result = {f.name: _dump(getattr(value, f.name)) for f in fields(value)}
        if type(value) in ACTION_TYPES.values():
            return {"kind": value.kind, **result}
        return result
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON mapping keys must be strings")
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise TypeError(f"cannot encode {type(value).__name__}")


def to_dict(value: Any) -> dict[str, Any]:
    result = _dump(value)
    if not isinstance(result, dict):
        raise TypeError("expected a DTO")
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(to_dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def fingerprint(request: ActionRequest) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(request)).hexdigest()


def _strict(cls, raw: object):
    if not isinstance(raw, dict) or set(raw) != {f.name for f in fields(cls)}:
        raise ValueError(f"invalid fields for {cls.__name__}")
    return cls(**raw)


def request_from_dict(raw: object) -> ActionRequest:
    if not isinstance(raw, dict) or set(raw) != {f.name for f in fields(ActionRequest)}:
        raise ValueError("invalid ActionRequest fields")
    payload = dict(raw)
    action = payload["action"]
    if not isinstance(action, dict) or not isinstance(action.get("kind"), str):
        raise ValueError("action requires a kind")
    cls = ACTION_TYPES.get(action["kind"])
    if cls is None:
        raise ValueError("unknown action kind")
    data = {name: value for name, value in action.items() if name != "kind"}
    if cls.kind in {"proof-create", "statement-align", "formal-resume"}:
        for name in ("spec_artifact", "lean_source_artifact", "checkpoint_artifact"):
            if name in data and data[name] is not None:
                data[name] = _strict(ArtifactRef, data[name])
    if cls.kind in {"formal-search", "formal-resume"} and "budget" in data:
        data["budget"] = _strict(Budget, data["budget"])
    if cls is QueueResearchMoves and "moves" in data:
        if not isinstance(data["moves"], list):
            raise ValueError("moves must be a list")
        data["moves"] = tuple(_strict(ResearchGuidance, move) for move in data["moves"])
    if cls is QueueCoordinatorEscalations:
        if not isinstance(data.get("escalations"), list) or not isinstance(data.get("basis"), list):
            raise ValueError("escalations and basis must be arrays")
        data["escalations"] = tuple(_strict(CoordinatorEscalation, item) for item in data["escalations"])
        data["basis"] = tuple(data["basis"])
    payload["action"] = _strict(cls, data)
    payload["preconditions"] = _strict(Preconditions, payload["preconditions"])
    if payload["deadline"] is not None:
        if not isinstance(payload["deadline"], str) or not payload["deadline"].endswith("Z"):
            raise ValueError("deadline must be UTC timestamp")
        payload["deadline"] = datetime.fromisoformat(payload["deadline"].replace("Z", "+00:00"))
    return _strict(ActionRequest, payload)


def request_from_json(data: bytes | str) -> ActionRequest:
    def no_duplicate(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON key: {key}")
            obj[key] = value
        return obj

    return request_from_dict(json.loads(data, object_pairs_hook=no_duplicate,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonfinite JSON value: {value}"))))
