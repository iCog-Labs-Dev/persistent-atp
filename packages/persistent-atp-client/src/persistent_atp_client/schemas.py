"""Packaged public schema IDs and byte identities for contract negotiation."""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json

SCHEMA_FILES = ("action-request.schema.json", "coordination-request.schema.json", "public-models.schema.json")


def schema_manifest() -> tuple[dict[str, str], ...]:
    root = files("persistent_atp_client").joinpath("schemas")
    result = []
    for name in SCHEMA_FILES:
        content = root.joinpath(name).read_bytes()
        schema = json.loads(content)
        result.append({"name": name, "schema_id": schema["$id"],
                       "sha256": "sha256:" + hashlib.sha256(content).hexdigest()})
    return tuple(result)
