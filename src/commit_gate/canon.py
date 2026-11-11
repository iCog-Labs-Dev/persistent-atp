"""Canonical serialization and hashing for the event journal.

One logical event serializes to exactly one byte string: sorted keys, no
incidental whitespace, UTF-8 without escaping, no non-finite floats.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

__all__ = ["canonical_json", "content_hash", "chain_hash", "GENESIS_HASH"]

GENESIS_HASH = "sha256:" + "0" * 64
"""Predecessor hash of the first event in a proof's journal."""


def _check_floats(value: Any) -> None:
    """Reject non-finite floats, which have no canonical JSON form."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not canonically hashable: {value!r}")
    elif isinstance(value, dict):
        for item in value.values():
            _check_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_floats(item)


def canonical_json(payload: Any) -> bytes:
    """Serialize to the one byte string this system considers canonical."""
    _check_floats(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    """`sha256:`-prefixed digest of the canonical form of `payload`."""
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def chain_hash(prev_hash: str, body: Any) -> str:
    """Digest binding an event body to its predecessor."""
    return content_hash({"prev": prev_hash, "body": body})
