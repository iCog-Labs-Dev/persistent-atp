"""Pinned Mathematics-ai-atp v2 contract and content-address helpers."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator, validators

CONTRACT_DIR = Path(__file__).parent / "contracts" / "formal-v2"
PROVIDER_COMMIT = "947a98a30eb30b869009d130e5d7bf7fecbb7644"
MANIFEST_HASH = "sha256:780199744f9c3b8a41ec9843f4707ee3a9f3bec46a44c20f8d1379a85995df76"


class ArtifactRepository(Protocol):
    """Implemented by artifacts.store.ArtifactStore; owned by the caller."""

    def put(self, content: bytes) -> str: ...
    def get(self, artifact_hash: str) -> bytes: ...
    def exists(self, artifact_hash: str) -> bool: ...


def address(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def without_nulls(value):
    if isinstance(value, dict):
        return {k: without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [without_nulls(v) for v in value]
    return value


@lru_cache(maxsize=1)
def verify_package() -> None:
    """Verify the original upstream manifest and the nine runtime schemas."""
    raw = (CONTRACT_DIR / "manifest.json").read_bytes()
    if address(raw) != MANIFEST_HASH:
        raise ValueError("formal v2 contract manifest differs from the pinned package")
    manifest = json.loads(raw)
    for name in manifest["schema_canonical_sha256"]:
        expected = manifest["files_sha256"][name]
        if address((CONTRACT_DIR / name).read_bytes()) != expected:
            raise ValueError(f"formal v2 contract file differs from manifest: {name}")


@lru_cache(maxsize=None)
def validator(name):
    verify_package()
    schema = json.loads((CONTRACT_DIR / "schemas" / f"{name}.v2.schema.json").read_bytes())
    # JSON Schema permits 1.0 as an integer; the provider uses strict integers.
    checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    )
    return validators.extend(Draft202012Validator, type_checker=checker)(schema)


def validate(name, value) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
            for e in validator(name).iter_errors(value)]
