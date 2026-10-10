"""Content-addressed artifact store.

The byte-level authority for large objects (Lean traces, prompts, model
outputs, source files) referenced elsewhere only by their content hash. The
graph and journal stay small; this is where the bytes actually live.

Callers of this module should never see a raw `psycopg` error: every driver
failure is translated into one of the exceptions in `errors.py`.

Schema creation is a deliberate, separate step — see `schema.py`. This store
assumes the `artifacts` table already exists; it never creates it.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from dotenv import load_dotenv

from .errors import (
    ArtifactNotFoundError,
    ArtifactQueryError,
    ArtifactStoreUnavailable,
    SchemaNotAppliedError,
)
from .hashing import hash_content, is_valid_hash

__all__ = ["ArtifactStore"]

load_dotenv()

_DEFAULT_DB_URL = "postgresql://localhost:5432/artifacts"


@contextmanager
def _translate_errors(operation: str) -> Iterator[None]:
    """Re-raise psycopg exceptions as this package's own exception types."""
    try:
        yield
    except psycopg.errors.UndefinedTable as exc:
        raise SchemaNotAppliedError(
            "the 'artifacts' table does not exist; run "
            "`python -m artifacts.schema` against this database first"
        ) from exc
    except psycopg.OperationalError as exc:
        raise ArtifactStoreUnavailable(f"{operation}: {exc}") from exc
    except psycopg.Error as exc:
        raise ArtifactQueryError(f"{operation}: {exc}") from exc


class ArtifactStore:
    """A Postgres-backed, content-addressed store of immutable byte blobs."""

    def __init__(self, db_url: str | None = None):
        db_url = db_url or os.environ.get("ARTIFACT_DB_URL", _DEFAULT_DB_URL)
        with _translate_errors("connect"):
            self._conn = psycopg.connect(db_url)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def put(self, content: bytes) -> str:
        """Store `content`, returning its `sha256:`-prefixed hash.

        Storing identical content twice is a no-op the second time: the row
        already exists, keyed by hash, so nothing is duplicated.
        """
        if not isinstance(content, bytes):
            raise TypeError(f"content must be bytes, got {type(content).__name__}")

        content_hash = hash_content(content)
        with _translate_errors("put"):
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO artifacts (hash, content, size_bytes) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (hash) DO NOTHING",
                    (content_hash, content, len(content)),
                )
            self._conn.commit()
        return content_hash

    def get(self, artifact_hash: str) -> bytes:
        """Return the exact bytes stored under `artifact_hash`.

        Raises `ArtifactNotFoundError` if nothing is stored under that hash.
        """
        if not is_valid_hash(artifact_hash):
            raise ValueError(f"not a valid artifact hash: {artifact_hash!r}")

        with _translate_errors("get"):
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM artifacts WHERE hash = %s",
                    (artifact_hash,),
                )
                row = cur.fetchone()

        if row is None:
            raise ArtifactNotFoundError(artifact_hash)
        (content,) = row
        return bytes(content)

    def exists(self, artifact_hash: str) -> bool:
        """Whether something is stored under `artifact_hash`."""
        if not is_valid_hash(artifact_hash):
            raise ValueError(f"not a valid artifact hash: {artifact_hash!r}")

        with _translate_errors("exists"):
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM artifacts WHERE hash = %s)",
                    (artifact_hash,),
                )
                (found,) = cur.fetchone()
        return bool(found)