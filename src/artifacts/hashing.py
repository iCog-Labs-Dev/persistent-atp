"""Content hashing for the artifact store.

Uses the same `sha256:`-prefixed digest format as `commit_gate.canon`, so a
hash looks and behaves identically whether it names a journal event or an
artifact. Deliberately self-contained — no import from `commit_gate` — since
the artifact store is not exclusively a commit-gate dependency.
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["hash_content", "is_valid_hash", "HASH_PATTERN"]

HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
"""Matches the exact shape of a valid artifact hash. `schema.py`'s CHECK
constraint enforces this same shape at the database level."""


def hash_content(content: bytes) -> str:
    """`sha256:`-prefixed digest of raw bytes.

    Two calls with identical bytes always return the identical hash; this is
    the property the artifact store's content-addressing and deduplication
    depend on.
    """
    if not isinstance(content, bytes):
        raise TypeError(f"content must be bytes, got {type(content).__name__}")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def is_valid_hash(value: str) -> bool:
    """Whether `value` has the exact shape of a hash this module produces."""
    return isinstance(value, str) and bool(HASH_PATTERN.match(value))