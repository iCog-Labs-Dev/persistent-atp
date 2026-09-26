"""Database schema for the artifact store.

One idempotent `CREATE TABLE IF NOT EXISTS` statement is the entire schema.
Applying it is a deliberate step, separate from normal store usage (see
`store.py`) — so only the connection string differs between a local database
and a production one; the schema applied is always this exact statement.

Run directly to apply the schema against `ARTIFACT_DB_URL`:

    uv run python -m artifacts.schema
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

__all__ = ["apply_schema", "table_exists"]

load_dotenv()

_DEFAULT_DB_URL = "postgresql://localhost:5432/artifacts"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS artifacts (
    hash        TEXT PRIMARY KEY
                    CHECK (hash ~ '^sha256:[0-9a-f]{64}$'),
    content     BYTEA NOT NULL,
    size_bytes  BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _resolve_db_url(db_url: str | None) -> str:
    return db_url or os.environ.get("ARTIFACT_DB_URL", _DEFAULT_DB_URL)


def apply_schema(db_url: str | None = None) -> None:
    """Create the `artifacts` table if it does not already exist.

    Safe to call any number of times, against an empty database or one that
    already has the table — this is the only place the schema is defined.
    """
    with psycopg.connect(_resolve_db_url(db_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
        # `with psycopg.connect(...)` commits on clean exit; nothing else to do.


def table_exists(db_url: str | None = None) -> bool:
    """Whether the `artifacts` table exists on the target database."""
    with psycopg.connect(_resolve_db_url(db_url)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS ("
                "  SELECT FROM information_schema.tables "
                "  WHERE table_name = 'artifacts'"
                ")"
            )
            (exists,) = cur.fetchone()
            return bool(exists)


if __name__ == "__main__":
    apply_schema()
    print("schema applied")