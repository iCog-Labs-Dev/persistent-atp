# Artifact Store

Content-addressed, byte-level storage for large objects (Lean traces,
prompts, model outputs, source files) that the metagraph and journal
reference only by hash, never by value. This is the "byte-level authority
for large objects" described in the architecture's design: the graph and
journal stay small and fast; this is where the bytes actually live.

Guarantee this store makes: given a hash, the exact original bytes come
back, unchanged, for as long as they were ever stored. Nothing here ever
updates or deletes a stored value — identical content always hashes to the
same key, so storing it twice is a no-op, not a duplicate.

Postgres-backed. Schema creation is a deliberate, separate step from normal
use — see `schema.py` — so the store itself never creates its own table.

## File Summary

- **`hashing.py`** — turns raw bytes into their `sha256:`-prefixed content
  hash, and validates that a string has the shape of one. Same hash format
  as `commit_gate.canon`, kept independent on purpose.
- **`schema.py`** — the one idempotent `CREATE TABLE IF NOT EXISTS` for the
  `artifacts` table. Run once per database, before first use:
  `uv run python -m artifacts.schema`.
- **`store.py`** — `ArtifactStore`: `put(bytes) -> hash`, `get(hash) -> bytes`,
  `exists(hash) -> bool`. Connects via `ARTIFACT_DB_URL`. Raw `psycopg`
  errors are never raised to callers — see `errors.py`.
- **`errors.py`** — this package's exception types
  (`ArtifactStoreUnavailable`, `SchemaNotAppliedError`, `ArtifactNotFoundError`,
  `ArtifactQueryError`), all deriving from `ArtifactStoreError`.

## Setup

1. Create a Postgres database and set `ARTIFACT_DB_URL` in `.env` (see
   `.env.example`).
2. Apply the schema once: `uv run python -m artifacts.schema`
3. Use `ArtifactStore()` as normal — it assumes the table already exists
   and fails clearly (`SchemaNotAppliedError`) if step 2 was skipped.