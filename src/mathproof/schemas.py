"""Loading and validating against the repo's JSON Schemas.

Every schema carries an ``$id`` under ``https://persistent-atp.invalid/schemas/``
and may reference siblings by that id (formal-state reuses score-vector). The
registry here preloads the whole ``schemas/`` directory so ``$ref`` resolution
works offline in tests and tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"

__all__ = ["SCHEMAS_DIR", "load_schema", "validator_for", "validate"]


def _build_registry() -> Registry:
    registry: Registry = Registry()
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(schema["$id"], resource)
    return registry


_REGISTRY: Registry | None = None


def load_schema(name: str) -> dict[str, Any]:
    """One schema by file stem, e.g. ``load_schema('formal-state')``."""
    path = SCHEMAS_DIR / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validator_for(name: str) -> Draft202012Validator:
    """A validator for one schema, with sibling ``$ref``s resolvable."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return Draft202012Validator(load_schema(name), registry=_REGISTRY)


def validate(name: str, payload: Any) -> list[str]:
    """Human-readable error strings for every violation of `name` by `payload`."""
    return [
        f"{'/'.join(str(p) for p in err.absolute_path)}: {err.message}"
        for err in validator_for(name).iter_errors(payload)
    ]
