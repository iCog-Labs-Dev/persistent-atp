"""Artifact-store exceptions.

Callers of the store should never have to import ``psycopg`` errors directly:
every driver failure raised inside the store is translated into one of the
exceptions below, so the underlying database stays an implementation detail.
"""

from __future__ import annotations

__all__ = [
    "ArtifactStoreError",
    "ArtifactStoreUnavailable",
    "SchemaNotAppliedError",    
    "ArtifactNotFoundError",
    "ArtifactQueryError",
]


class ArtifactStoreError(Exception):
    """Base class for every failure raised by the artifact store."""


class ArtifactStoreUnavailable(ArtifactStoreError):
    """The database could not be reached, authenticated with, or kept a connection."""


class SchemaNotAppliedError(ArtifactStoreError):
    """The `artifacts` table does not exist.

    Schema creation is a deliberate, separate step (see `schema.py`), never
    run implicitly by the store, so this means that step has not been run
    yet against the target database.
    """


class ArtifactNotFoundError(ArtifactStoreError):
    """`get()` was called with a hash that is not in the store."""

    def __init__(self, artifact_hash: str):
        super().__init__(f"no artifact stored for hash {artifact_hash!r}")
        self.artifact_hash = artifact_hash


class ArtifactQueryError(ArtifactStoreError):
    """The database rejected or failed a statement (bad parameter, or a
    server-side error that outlived the driver's own retries)."""