"""SQL journal store for the commit gate.

The journal is the durability authority: an event is committed when it is here.
Every mutation runs inside one `BEGIN IMMEDIATE` transaction, so reading the
head and inserting its successor cannot interleave with another writer.

Only the commit gate may call the mutating methods. Leases are scheduling
state, not committed proof state: they live beside the journal (never in the
hash chain) so dispatching work does not advance a proof's revision, and every
lease transition is mirrored into an append-only `lease_events` audit trail.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from .canon import GENESIS_HASH, canonical_json, chain_hash
from .reasons import Reason

__all__ = [
    "JournalStore",
    "ConcurrencyError",
    "HashChainError",
    "LeaseRow",
]


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


@dataclass(frozen=True, slots=True)
class LeaseRow:
    """One issued lease as the store records it."""

    proof_id: str
    lease_id: str
    worker_class: str
    selected_move_id: str | None
    fencing_token: int
    base_revision: int
    ttl_seconds: float
    issued_at: float
    expires_at: float | None
    status: str  # active | expired | released | superseded

    def is_live(self, now: float) -> bool:
        """Held and unexpired: usable for a commit."""
        return self.status == "active" and (self.expires_at is None or now < self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "lease_id": self.lease_id,
            "worker_class": self.worker_class,
            "selected_move_id": self.selected_move_id,
            "fencing_token": self.fencing_token,
            "base_revision": self.base_revision,
            "ttl_seconds": self.ttl_seconds,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class JournalStore:
    """A SQLite-backed append-only journal of proof events."""

    def __init__(
        self,
        db_path: str = ":memory:",
        busy_timeout_ms: int = 5000,
        clock: Callable[[], float] = time.time,
    ):
        # Autocommit mode: `with conn:` begins no transaction when
        # isolation_level is None, so `_write` opens them explicitly.
        # `busy_timeout_ms` is how long a writer waits for the lock before
        # giving up; giving up is reported as a rejection, never a hang.
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, timeout=busy_timeout_ms / 1000
        )
        self._conn.row_factory = sqlite3.Row
        self._clock = clock
        # Must precede any locking pragma below: `busy_timeout` itself takes
        # no lock, and the conversion to WAL does not honor the connect-time
        # timeout, so it needs the retry loop itself.
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        # WAL lets a projector read the journal while a worker writes it.
        # Only the first connection converts the database (WAL is
        # persistent); that conversion needs a brief exclusive lock and, unlike
        # ordinary statements, fails immediately under contention rather than
        # waiting on the busy handler -- so a fresh opener retries a few times
        # instead of surfacing `database is locked`.
        self._set_wal_mode()
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    def _set_wal_mode(self) -> None:
        """Convert the database to WAL, tolerating racing openers.

        A clean non-WAL answer is permanent -- an in-memory database reports
        'memory' -- so only a lock failure retries, a few times before
        giving up and surfacing the error.
        """
        for attempt in range(5):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(0.05)

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
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(leases)").fetchall()
        }
        if columns and "status" not in columns:
            # Pre-scheduler shape (one write-lease row per proof): no live
            # deployments read it, so rebuild rather than migrate.
            self._conn.execute("DROP TABLE leases")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                proof_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                worker_class TEXT NOT NULL DEFAULT '',
                selected_move_id TEXT,
                fencing_token INTEGER NOT NULL,
                base_revision INTEGER NOT NULL DEFAULT 0,
                ttl_seconds REAL NOT NULL DEFAULT 0,
                issued_at REAL NOT NULL,
                expires_at REAL,
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (proof_id, lease_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lease_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                proof_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                event TEXT NOT NULL,
                fencing_token INTEGER,
                worker_class TEXT,
                selected_move_id TEXT,
                score_snapshot TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # -- leases -------------------------------------------------------------
    #
    # Two issuance shapes share one monotonic token counter per proof:
    #
    #   acquire_lease  -- the proof-level write lock (8.4): issuing it
    #     supersedes every other active lease on the proof. Coordinator-grade
    #     structural work holds this; scheduler-dispatched workers then lose
    #     their leases and must re-lease, which is the point of an exclusive
    #     maintenance window.
    #   issue_lease    -- one move dispatch (2.x): supersedes only an active
    #     lease on the same move, so distinct moves run concurrently while a
    #     move is never double-dispatched.
    #
    # A commit must name (lease_id, fencing_token) matching an *active,
    # unexpired* lease; expired/superseded/released rows are dead forever
    # because tokens never repeat.

    def acquire_lease(self, proof_id: str, lease_id: str, ttl_seconds: float = 0.0) -> int:
        """Take the exclusive write lease on `proof_id`; return its token.

        Tokens increase monotonically per proof and never repeat. Any ttl
        given also bounds this acquisition; 0 means no expiry.
        """
        with self._write() as conn:
            self._expire_due_leases(conn, proof_id)
            conn.execute(
                """
                UPDATE leases SET status = 'superseded'
                WHERE proof_id = ? AND status = 'active' AND lease_id <> ?
                """,
                (proof_id, lease_id),
            )
            for row in conn.execute(
                "SELECT lease_id FROM leases WHERE proof_id = ? AND status = 'superseded'",
                (proof_id,),
            ):
                self._record_lease_event(
                    conn, proof_id, row["lease_id"], "superseded"
                )
            token = self._next_token(conn, proof_id)
            now = self._clock()
            expires_at = now + ttl_seconds if ttl_seconds > 0 else None
            conn.execute(
                """
                INSERT INTO leases (
                    proof_id, lease_id, worker_class, selected_move_id,
                    fencing_token, base_revision, ttl_seconds,
                    issued_at, expires_at, status
                ) VALUES (?, ?, '', NULL, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(proof_id, lease_id) DO UPDATE SET
                    worker_class = '',
                    selected_move_id = NULL,
                    fencing_token = excluded.fencing_token,
                    base_revision = excluded.base_revision,
                    ttl_seconds = excluded.ttl_seconds,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at,
                    status = 'active'
                """,
                (
                    proof_id,
                    lease_id,
                    token,
                    self.head(proof_id)[0],
                    ttl_seconds,
                    now,
                    expires_at,
                ),
            )
            self._record_lease_event(
                conn, proof_id, lease_id, "issued", fencing_token=token
            )
        return token

    def issue_lease(
        self,
        proof_id: str,
        lease_id: str,
        *,
        worker_class: str,
        selected_move_id: str | None = None,
        base_revision: int | None = None,
        ttl_seconds: float = 600.0,
        score_snapshot: dict[str, Any] | None = None,
    ) -> LeaseRow:
        """Issue one scheduler dispatch lease (Section 2).

        One indivisible operation inside the journal write lock: lapse any
        due leases on this proof, supersede any active lease still held
        against the same move, mint the next fencing token, record the lease
        with its score snapshot in the audit trail, and return the row.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with self._write() as conn:
            self._expire_due_leases(conn, proof_id)
            if selected_move_id is not None:
                conn.execute(
                    """
                    UPDATE leases SET status = 'superseded'
                    WHERE proof_id = ? AND status = 'active'
                      AND selected_move_id IS ?
                      AND lease_id <> ?
                    """,
                    (proof_id, selected_move_id, lease_id),
                )
                for row in conn.execute(
                    """
                    SELECT lease_id FROM leases
                    WHERE proof_id = ? AND status = 'superseded'
                      AND selected_move_id IS ? AND lease_id <> ?
                    """,
                    (proof_id, selected_move_id, lease_id),
                ):
                    self._record_lease_event(
                        conn, proof_id, row["lease_id"], "superseded"
                    )
            token = self._next_token(conn, proof_id)
            now = self._clock()
            revision = (
                self.head(proof_id)[0] if base_revision is None else base_revision
            )
            conn.execute(
                """
                INSERT INTO leases (
                    proof_id, lease_id, worker_class, selected_move_id,
                    fencing_token, base_revision, ttl_seconds,
                    issued_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(proof_id, lease_id) DO UPDATE SET
                    worker_class = excluded.worker_class,
                    selected_move_id = excluded.selected_move_id,
                    fencing_token = excluded.fencing_token,
                    base_revision = excluded.base_revision,
                    ttl_seconds = excluded.ttl_seconds,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at,
                    status = 'active'
                """,
                (
                    proof_id,
                    lease_id,
                    worker_class,
                    selected_move_id,
                    token,
                    revision,
                    ttl_seconds,
                    now,
                    now + ttl_seconds,
                ),
            )
            self._record_lease_event(
                conn,
                proof_id,
                lease_id,
                "issued",
                fencing_token=token,
                worker_class=worker_class,
                selected_move_id=selected_move_id,
                score_snapshot=score_snapshot,
            )
            return LeaseRow(
                proof_id=proof_id,
                lease_id=lease_id,
                worker_class=worker_class,
                selected_move_id=selected_move_id,
                fencing_token=token,
                base_revision=revision,
                ttl_seconds=ttl_seconds,
                issued_at=now,
                expires_at=now + ttl_seconds,
                status="active",
            )

    def release_lease(self, proof_id: str, lease_id: str) -> bool:
        """Mark a lease finished; its token can never commit again."""
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE leases SET status = 'released'
                WHERE proof_id = ? AND lease_id = ? AND status = 'active'
                """,
                (proof_id, lease_id),
            )
            if cursor.rowcount:
                self._record_lease_event(conn, proof_id, lease_id, "released")
            return bool(cursor.rowcount)

    def active_leases(self, proof_id: str) -> list[LeaseRow]:
        """Live leases on this proof, lapsing any that are due first."""
        with self._write() as conn:
            self._expire_due_leases(conn, proof_id)
            rows = conn.execute(
                "SELECT * FROM leases WHERE proof_id = ? AND status = 'active'",
                (proof_id,),
            ).fetchall()
        return [self._lease_row(row) for row in rows]

    def read_lease_events(self, proof_id: str) -> Sequence[dict[str, Any]]:
        """The append-only audit trail of lease transitions, oldest first."""
        rows = self._conn.execute(
            """
            SELECT seq, lease_id, event, fencing_token, worker_class,
                   selected_move_id, score_snapshot, created_at
            FROM lease_events WHERE proof_id = ? ORDER BY seq ASC
            """,
            (proof_id,),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "lease_id": row["lease_id"],
                "event": row["event"],
                "fencing_token": row["fencing_token"],
                "worker_class": row["worker_class"],
                "selected_move_id": row["selected_move_id"],
                "score_snapshot": (
                    json.loads(row["score_snapshot"])
                    if row["score_snapshot"]
                    else None
                ),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _next_token(conn: sqlite3.Connection, proof_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(fencing_token) AS top FROM leases WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        return (row["top"] or 0) + 1

    def _expire_due_leases(self, conn: sqlite3.Connection, proof_id: str) -> int:
        """Lapse every active lease past its expiry; return how many."""
        now = self._clock()
        due = conn.execute(
            """
            SELECT lease_id FROM leases
            WHERE proof_id = ? AND status = 'active'
              AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (proof_id, now),
        ).fetchall()
        for row in due:
            conn.execute(
                """
                UPDATE leases SET status = 'expired'
                WHERE proof_id = ? AND lease_id = ?
                """,
                (proof_id, row["lease_id"]),
            )
            self._record_lease_event(
                conn, proof_id, row["lease_id"], "expired", when=now
            )
        return len(due)

    @staticmethod
    def _record_lease_event(
        conn: sqlite3.Connection,
        proof_id: str,
        lease_id: str,
        event: str,
        *,
        fencing_token: int | None = None,
        worker_class: str | None = None,
        selected_move_id: str | None = None,
        score_snapshot: dict[str, Any] | None = None,
        when: float | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lease_events (
                proof_id, lease_id, event, fencing_token, worker_class,
                selected_move_id, score_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proof_id,
                lease_id,
                event,
                fencing_token,
                worker_class,
                selected_move_id,
                canonical_json(score_snapshot).decode("utf-8")
                if score_snapshot is not None
                else None,
            ),
        )

    @staticmethod
    def _lease_row(row: sqlite3.Row) -> LeaseRow:
        return LeaseRow(
            proof_id=row["proof_id"],
            lease_id=row["lease_id"],
            worker_class=row["worker_class"],
            selected_move_id=row["selected_move_id"],
            fencing_token=row["fencing_token"],
            base_revision=row["base_revision"],
            ttl_seconds=row["ttl_seconds"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            status=row["status"],
        )

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

        try:
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
        except ConcurrencyError as exc:
            if exc.reason is Reason.LEASE_NOT_HELD:
                # The refused append rolled back, taking the lazy lapse of the
                # dead lease with it. Re-lapse in its own transaction so the
                # expiry stays audited (Invariant 10).
                with self._write() as conn:
                    self._expire_due_leases(conn, proof_id)
            raise
        return revision, event_hash

    def _check_lease(
        self,
        conn: sqlite3.Connection,
        proof_id: str,
        lease_id: str | None,
        fencing_token: int | None,
    ) -> None:
        """Confirm the proposer holds an active, unexpired lease on this proof.

        Any live dispatch lease counts -- distinct moves may be leased
        concurrently -- but a lapsed, released, or superseded row is dead
        forever, and a token that does not match its lease is superseded.
        """
        self._expire_due_leases(conn, proof_id)
        row = conn.execute(
            "SELECT * FROM leases WHERE proof_id = ? AND lease_id = ?",
            (proof_id, lease_id),
        ).fetchone()
        if row is None:
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD, f"no lease {lease_id!r} is held on {proof_id!r}"
            )
        if row["status"] != "active":
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD,
                f"lease {lease_id!r} on {proof_id!r} is {row['status']}, not active",
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
