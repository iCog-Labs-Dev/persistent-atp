"""Pinned provider contract and serialization helpers; no second ATP adapter."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator, validators

CONTRACT_DIR = Path(__file__).parent / "contracts" / "formal-v2"
PROVIDER_COMMIT = "947a98a30eb30b869009d130e5d7bf7fecbb7644"
MANIFEST_HASH = "sha256:9db7fff5c8f06eb6c5f2f76de2dc0a1942bd91786f7b783ee4909731ee892486"


class ArtifactRepository(Protocol):
    """The existing coordinator-owned artifact store."""

    def put(self, content: bytes) -> str: ...
    def get(self, artifact_hash: str) -> bytes: ...
    def exists(self, artifact_hash: str) -> bool: ...


def address(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON: {value}")))


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


@lru_cache(maxsize=1)
def verify_package() -> None:
    raw = (CONTRACT_DIR / "manifest.json").read_bytes()
    if address(raw) != MANIFEST_HASH:
        raise ValueError("formal v2 contract manifest differs from the pinned package")
    manifest = _json(raw)
    for name in manifest["schema_canonical_sha256"]:
        if address((CONTRACT_DIR / name).read_bytes()) != manifest["files_sha256"][name]:
            raise ValueError(f"formal v2 contract file differs from manifest: {name}")


@lru_cache(maxsize=None)
def validator(name):
    verify_package()
    schema = _json((CONTRACT_DIR / "schemas" / f"{name}.v2.schema.json").read_bytes())
    checker = Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda checker, value: type(value) is int)
    return validators.extend(Draft202012Validator, type_checker=checker)(schema)


def validate(name, value) -> list[str]:
    return [f"{'/'.join(map(str, error.absolute_path))}: {error.message}"
            for error in validator(name).iter_errors(value)]
