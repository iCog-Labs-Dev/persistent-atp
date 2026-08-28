"""SQL journal store for the commit gate.

The journal is the durability authority: an event is committed when it is here.
Every mutation runs inside one `BEGIN IMMEDIATE` transaction, so reading the
head and inserting its successor cannot interleave with another writer.

Only the commit gate may call the mutating methods.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from .canon import GENESIS_HASH, canonical_json, chain_hash
from .reasons import Reason

__all__ = ["JournalStore", "ConcurrencyError", "HashChainError"]


class ConcurrencyError(Exception):
    """A write lost a race against another writer.

    Carries the `Reason` the gate reports back to the proposer, so both layers
    name the failure identically without the store building a `Rejection`.
    """

    def __init__(self, reason: Reason, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class HashChainError(Exception):
    """Raised when a journal's recorded hashes do not chain."""


class JournalStore:
    """A SQLite-backed append-only journal of proof events."""

    def __init__(self, db_path: str = ":memory:", busy_timeout_ms: int = 5000):
        # Autocommit mode: `with conn:` begins no transaction when
        # isolation_level is None, so `_write` opens them explicitly.
        # `busy_timeout_ms` is how long a writer waits for the lock before
        # giving up; giving up is reported as a rejection, never a hang.
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, timeout=busy_timeout_ms / 1000
        )
        self._conn.row_factory = sqlite3.Row
        # WAL lets a projector read the journal while a worker writes it. 
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS journal (
                proof_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                event_hash TEXT NOT NULL UNIQUE,
                prev_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                worker_class TEXT NOT NULL,
                payload TEXT NOT NULL,
                committed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (proof_id, revision)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                proof_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL
            )
            """
        )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Hold the database write lock for the whole block, or roll back."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # Another writer held the lock past the busy timeout. Nothing was
            # written, and there is no transaction to roll back.
            raise ConcurrencyError(
                Reason.JOURNAL_BUSY, f"could not take the journal write lock: {exc}"
            ) from exc
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        try:
            self._conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self._conn.execute("ROLLBACK")
            raise ConcurrencyError(
                Reason.JOURNAL_BUSY, f"could not commit the journal write: {exc}"
            ) from exc

    def _head_row(self, proof_id: str) -> sqlite3.Row | None:
        """This proof's latest journal row, or None if it has no events."""
        return self._conn.execute(
            """
            SELECT revision, event_hash, prev_hash, payload FROM journal
            WHERE proof_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (proof_id,),
        ).fetchone()

    def head(self, proof_id: str) -> tuple[int, str]:
        """The `(revision, event_hash)` of this proof's latest event."""
        row = self._head_row(proof_id)
        if row is None:
            return 0, GENESIS_HASH
        return row["revision"], row["event_hash"]

    def acquire_lease(self, proof_id: str, lease_id: str) -> int:
        """Take the write lease on `proof_id`, returning its fencing token.

        Tokens increase monotonically per proof and never repeat, so once a
        newer holder has acquired, an older holder's writes are rejected.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT fencing_token FROM leases WHERE proof_id = ?", (proof_id,)
            ).fetchone()
            if row is None:
                token = 1
                conn.execute(
                    "INSERT INTO leases (proof_id, lease_id, fencing_token) VALUES (?, ?, ?)",
                    (proof_id, lease_id, token),
                )
            else:
                token = row["fencing_token"] + 1
                conn.execute(
                    "UPDATE leases SET lease_id = ?, fencing_token = ? WHERE proof_id = ?",
                    (lease_id, token, proof_id),
                )
        return token

    def append(self, payload_dict: dict[str, Any]) -> tuple[int, str]:
        """Append one already-validated proposal; return `(revision, event_hash)`.

        Reads the head, checks the proposal's concurrency expectations against
        it, chains onto it, and inserts — all under one write lock, so the head
        cannot move between the check and the insert.

        Raises `HashChainError` if the head's own hash does not match its
        payload: chaining onto a corrupt hash would bury the corruption under
        a link that verifies.
        """
        proof_id = payload_dict["proof_id"]
        base_revision = payload_dict.get("base_revision")
        lease_id = payload_dict.get("lease_id")
        fencing_token = payload_dict.get("fencing_token")

        with self._write() as conn:
            row = self._head_row(proof_id)
            if row is None:
                head_revision, head_hash = 0, GENESIS_HASH
            else:
                self._verify_row(row)
                head_revision, head_hash = row["revision"], row["event_hash"]

            if base_revision is not None and base_revision != head_revision:
                raise ConcurrencyError(
                    Reason.STALE_BASE_REVISION,
                    f"proposal is based on revision {base_revision}, head is {head_revision}",
                )

            if lease_id is not None or fencing_token is not None:
                self._check_lease(conn, proof_id, lease_id, fencing_token)

            revision = head_revision + 1
            event_hash = chain_hash(head_hash, payload_dict)
            conn.execute(
                """
                INSERT INTO journal (
                    proof_id, revision, event_hash, prev_hash,
                    actor, worker_class, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proof_id,
                    revision,
                    event_hash,
                    head_hash,
                    payload_dict["actor"],
                    payload_dict["worker_class"],
                    canonical_json(payload_dict).decode("utf-8"),
                ),
            )
        return revision, event_hash

    @staticmethod
    def _check_lease(
        conn: sqlite3.Connection,
        proof_id: str,
        lease_id: str | None,
        fencing_token: int | None,
    ) -> None:
        """Confirm the proposer still holds the proof's current lease."""
        row = conn.execute(
            "SELECT lease_id, fencing_token FROM leases WHERE proof_id = ?", (proof_id,)
        ).fetchone()
        if row is None:
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD, f"no lease is held on {proof_id!r}"
            )
        if row["lease_id"] != lease_id:
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD,
                f"lease {lease_id!r} is not the lease held on {proof_id!r}",
            )
        if row["fencing_token"] != fencing_token:
            raise ConcurrencyError(
                Reason.FENCING_TOKEN_SUPERSEDED,
                f"fencing token {fencing_token!r} is superseded by {row['fencing_token']!r}",
            )

    def read_events(self, proof_id: str) -> Sequence[dict[str, Any]]:
        """Every event payload for a proof, in revision order."""
        rows = self._conn.execute(
            "SELECT payload FROM journal WHERE proof_id = ? ORDER BY revision ASC",
            (proof_id,),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def proof_ids(self) -> Sequence[str]:
        """Every proof the journal holds events for, in order."""
        rows = self._conn.execute(
            "SELECT DISTINCT proof_id FROM journal ORDER BY proof_id"
        ).fetchall()
        return [row["proof_id"] for row in rows]

    def read_events_after(
        self, proof_id: str, revision: int
    ) -> Sequence[tuple[int, dict[str, Any]]]:
        """Each `(revision, payload)` past `revision`, in order.

        What a projection replays to catch up.
        """
        rows = self._conn.execute(
            """
            SELECT revision, payload FROM journal
            WHERE proof_id = ? AND revision > ? ORDER BY revision ASC
            """,
            (proof_id, revision),
        ).fetchall()
        return [(row["revision"], json.loads(row["payload"])) for row in rows]

    def read_chain(self, proof_id: str) -> Sequence[tuple[int, str, str]]:
        """Every `(revision, event_hash, prev_hash)` for a proof, in order."""
        rows = self._conn.execute(
            """
            SELECT revision, event_hash, prev_hash FROM journal
            WHERE proof_id = ? ORDER BY revision ASC
            """,
            (proof_id,),
        ).fetchall()
        return [(row["revision"], row["event_hash"], row["prev_hash"]) for row in rows]

    @staticmethod
    def _verify_row(row: sqlite3.Row) -> None:
        """Confirm one row's `event_hash` is the hash of its own contents.

        Catches a payload edited in place: the recorded hash then no longer
        matches what the payload chains to.
        """
        recomputed = chain_hash(row["prev_hash"], json.loads(row["payload"]))
        if recomputed != row["event_hash"]:
            raise HashChainError(
                f"revision {row['revision']} records {row['event_hash']} "
                f"but its payload chains to {recomputed}"
            )

    def verify_chain(self, proof_id: str) -> int:
        """Recompute a proof's whole chain; return how many events were checked.

        Raises `HashChainError` at the first row that is out of sequence, does
        not link to its predecessor, or does not hash to what it records. An
        empty journal verifies: zero events chain trivially from genesis.
        """
        rows = self._conn.execute(
            """
            SELECT revision, event_hash, prev_hash, payload FROM journal
            WHERE proof_id = ? ORDER BY revision ASC
            """,
            (proof_id,),
        ).fetchall()

        prev_hash = GENESIS_HASH
        for expected_revision, row in enumerate(rows, start=1):
            if row["revision"] != expected_revision:
                raise HashChainError(
                    f"{proof_id!r} skips from revision {expected_revision - 1} "
                    f"to {row['revision']}"
                )
            if row["prev_hash"] != prev_hash:
                raise HashChainError(
                    f"revision {row['revision']} follows {row['prev_hash']} "
                    f"but its predecessor is {prev_hash}"
                )
            self._verify_row(row)
            prev_hash = row["event_hash"]
        return len(rows)
