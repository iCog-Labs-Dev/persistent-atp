"""SQL journal store for the commit gate.

The journal is the durability authority: an event is committed when it is here.
Every mutation runs inside one `BEGIN IMMEDIATE` transaction, so reading the
head and inserting its successor cannot interleave with another writer.

Only the commit gate may call the mutating methods.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

from .canon import GENESIS_HASH, canonical_json, chain_hash
from .reasons import Reason
from .vocab import WorkerClass

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

    def __init__(
        self, db_path: str = ":memory:", busy_timeout_ms: int = 5000,
        *, clock_ns: Callable[[], int] = time.time_ns,
    ):
        # Autocommit mode: `with conn:` begins no transaction when
        # isolation_level is None, so `_write` opens them explicitly.
        # `busy_timeout_ms` is how long a writer waits for the lock before
        # giving up; giving up is reported as a rejection, never a hang.
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, timeout=busy_timeout_ms / 1000
        )
        self._clock_ns = clock_ns
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
                fencing_token INTEGER NOT NULL,
                actor TEXT NOT NULL,
                worker_class TEXT NOT NULL,
                expires_at_ns INTEGER
            )
            """
        )
        # Existing journals may have leases issued before identity was bound.
        # Such leases remain unusable until the trusted scheduler reacquires them.
        lease_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(leases)")
        }
        if "actor" not in lease_columns:
            self._conn.execute("ALTER TABLE leases ADD COLUMN actor TEXT")
        if "worker_class" not in lease_columns:
            self._conn.execute("ALTER TABLE leases ADD COLUMN worker_class TEXT")
        if "expires_at_ns" not in lease_columns:
            self._conn.execute("ALTER TABLE leases ADD COLUMN expires_at_ns INTEGER")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lease_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                proof_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                actor TEXT NOT NULL,
                worker_class TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at_ns INTEGER NOT NULL,
                expires_at_ns INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejections (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                proof_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL,
                payload TEXT,
                committed_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    def acquire_lease(
        self, proof_id: str, lease_id: str, *, actor: str, worker_class: str,
        ttl_seconds: float = 300.0,
    ) -> int:
        """Take the write lease on `proof_id`, returning its fencing token.

        Tokens increase monotonically per proof and never repeat, so once a
        newer holder has acquired, an older holder's writes are rejected. The
        trusted scheduler supplies the holder's identity and assigned class.
        """
        if not actor:
            raise ValueError("a lease needs a nonempty actor")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 86_400
        ):
            raise ValueError("lease ttl_seconds must be between 0 and 86400")
        try:
            worker_class = WorkerClass(worker_class).value
        except ValueError as exc:
            raise ValueError(f"unknown worker class {worker_class!r}") from exc
        with self._write() as conn:
            now_ns = self._clock_ns()
            expires_at_ns = now_ns + max(1, int(ttl_seconds * 1_000_000_000))
            row = conn.execute(
                "SELECT fencing_token FROM leases WHERE proof_id = ?", (proof_id,)
            ).fetchone()
            if row is None:
                token = 1
                conn.execute(
                    "INSERT INTO leases (proof_id, lease_id, fencing_token, actor, worker_class, expires_at_ns) VALUES (?, ?, ?, ?, ?, ?)",
                    (proof_id, lease_id, token, actor, worker_class, expires_at_ns),
                )
            else:
                token = row["fencing_token"] + 1
                conn.execute(
                    "UPDATE leases SET lease_id = ?, fencing_token = ?, actor = ?, worker_class = ?, expires_at_ns = ? WHERE proof_id = ?",
                    (lease_id, token, actor, worker_class, expires_at_ns, proof_id),
                )
            conn.execute(
                "INSERT INTO lease_audit (proof_id, lease_id, fencing_token, actor, worker_class, event_type, occurred_at_ns, expires_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (proof_id, lease_id, token, actor, worker_class, "acquired", now_ns, expires_at_ns),
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
        if not payload_dict.get("ops"):
            raise ConcurrencyError(
                Reason.EMPTY_PROPOSAL, "proposal carries no graph mutations"
            )
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
                self._check_lease(
                    conn, proof_id, lease_id, fencing_token,
                    payload_dict.get("actor"), payload_dict.get("worker_class"),
                    self._clock_ns(),
                )

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
        actor: str | None,
        worker_class: str | None,
        now_ns: int,
    ) -> None:
        """Confirm the proposer still holds the proof's current lease."""
        row = conn.execute(
            "SELECT lease_id, fencing_token, actor, worker_class, expires_at_ns FROM leases WHERE proof_id = ?",
            (proof_id,),
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
        if row["actor"] != actor or row["worker_class"] != worker_class:
            raise ConcurrencyError(
                Reason.LEASE_IDENTITY_MISMATCH,
                "proposal actor and worker class do not match the lease holder",
            )
        if row["expires_at_ns"] is None or now_ns >= row["expires_at_ns"]:
            raise ConcurrencyError(
                Reason.LEASE_EXPIRED, f"lease {lease_id!r} has expired"
            )

    def read_events(self, proof_id: str) -> Sequence[dict[str, Any]]:
        """Every event payload for a proof, in revision order."""
        rows = self._conn.execute(
            "SELECT payload FROM journal WHERE proof_id = ? ORDER BY revision ASC",
            (proof_id,),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def read_events_between(
        self, proof_id: str, after_revision: int, through_revision: int
    ) -> Sequence[dict[str, Any]]:
        """Events needed to bring a projection through a specific journal head."""
        rows = self._conn.execute(
            "SELECT revision, payload FROM journal WHERE proof_id = ? AND revision > ? "
            "AND revision <= ? ORDER BY revision",
            (proof_id, after_revision, through_revision),
        ).fetchall()
        expected = list(range(after_revision + 1, through_revision + 1))
        if [row["revision"] for row in rows] != expected:
            raise ConcurrencyError(
                Reason.READ_VIEW_OUT_OF_SYNC,
                f"journal cannot supply revisions {after_revision + 1}..{through_revision} "
                f"for proof {proof_id!r}",
            )
        return [json.loads(row["payload"]) for row in rows]

    def record_rejection(
        self,
        proof_id: str,
        reason: str,
        detail: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Journal one refused proposal as an auditable event (Invariants 8, 10).

        Rejections never enter the hash chain -- the chain records committed
        history only -- but late or stale work must remain visible to audit
        rather than vanishing at the door. Returns the rejection's sequence
        number. Raises `ConcurrencyError` if the write lock cannot be taken;
        the gate treats recording as best-effort for that case alone.
        """
        with self._write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rejections (proof_id, reason, detail, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    proof_id,
                    str(reason),
                    detail,
                    canonical_json(payload).decode("utf-8") if payload else None,
                ),
            )
        return cursor.lastrowid

    def read_rejections(self, proof_id: str) -> Sequence[dict[str, Any]]:
        """Every recorded rejection for a proof, oldest first."""
        rows = self._conn.execute(
            """
            SELECT seq, reason, detail, payload, committed_at
            FROM rejections WHERE proof_id = ? ORDER BY seq ASC
            """,
            (proof_id,),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "reason": row["reason"],
                "detail": row["detail"],
                "payload": json.loads(row["payload"]) if row["payload"] else None,
                "committed_at": row["committed_at"],
            }
            for row in rows
        ]

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
