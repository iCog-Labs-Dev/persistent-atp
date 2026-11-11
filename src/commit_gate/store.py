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
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Sequence

from .canon import GENESIS_HASH, canonical_json, chain_hash
from .reasons import Reason
from .vocab import WorkerClass

__all__ = ["JournalStore", "ConcurrencyError", "HashChainError", "LeaseRow", "LeaseCandidate"]


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
    """One dispatch or proof-wide lease, including its trusted holder."""

    proof_id: str
    lease_id: str
    actor: str
    worker_class: str
    selected_move_id: str | None
    fencing_token: int
    base_revision: int
    ttl_seconds: float
    issued_at: float
    expires_at: float | None
    status: str

    def is_live(self, now: float) -> bool:
        return (
            self.status == "active" and self.expires_at is not None
            and now < self.expires_at
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LeaseCandidate:
    """A ranked move and other IDs representing the same work target."""

    move_id: str
    score_snapshot: dict[str, float]
    conflict_ids: tuple[str, ...] = ()


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
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        # Concurrent openers can race while first enabling persistent WAL.
        for attempt in range(5):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                break
            except sqlite3.OperationalError as exc:
                if attempt == 4 or "locked" not in str(exc).lower():
                    raise
                time.sleep(0.05)
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    @staticmethod
    def _create_leases(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE leases (
                proof_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                actor TEXT,
                worker_class TEXT,
                expires_at_ns INTEGER,
                selected_move_id TEXT,
                base_revision INTEGER NOT NULL DEFAULT 0,
                ttl_seconds REAL NOT NULL DEFAULT 0,
                issued_at_ns INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (proof_id, lease_id)
            )
            """
        )

    def _init_schema(self) -> None:
        # Include schema inspection and migration in the same lock: a second
        # connection must not observe or repeat a partially upgraded schema.
        with self._write() as conn:
            conn.execute(
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
                row["name"] for row in conn.execute("PRAGMA table_info(leases)")
            }
            if not columns:
                self._create_leases(conn)
            elif "status" not in columns:
                # Preserve every legacy token, including leases that predate
                # holder binding. Unbound leases cannot become active again.
                for name, kind in (
                    ("actor", "TEXT"), ("worker_class", "TEXT"),
                    ("expires_at_ns", "INTEGER"),
                ):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE leases ADD COLUMN {name} {kind}")
                conn.execute("ALTER TABLE leases RENAME TO leases_legacy")
                self._create_leases(conn)
                conn.execute(
                    """
                    INSERT INTO leases (
                        proof_id, lease_id, fencing_token, actor, worker_class,
                        expires_at_ns, status
                    )
                    SELECT proof_id, lease_id, fencing_token, actor, worker_class,
                           expires_at_ns,
                           CASE
                             WHEN actor IS NULL OR actor = ''
                               OR worker_class IS NULL OR worker_class = ''
                               OR expires_at_ns IS NULL THEN 'superseded'
                             ELSE 'active'
                           END
                    FROM leases_legacy
                    """
                )
                conn.execute("DROP TABLE leases_legacy")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS active_move_lease
                ON leases (proof_id, selected_move_id)
                WHERE status = 'active' AND selected_move_id IS NOT NULL
                """
            )
            conn.execute(
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
            audit_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(lease_audit)")
            }
            for name, kind in (
                ("selected_move_id", "TEXT"), ("score_snapshot", "TEXT"),
                ("base_revision", "INTEGER"),
            ):
                if name not in audit_columns:
                    conn.execute(f"ALTER TABLE lease_audit ADD COLUMN {name} {kind}")
            conn.execute(
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

    def close(self) -> None:
        """Close the database and release its WAL sidecars."""
        self._conn.close()

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

    @staticmethod
    def _validate_holder(actor: str, worker_class: str, ttl_seconds: float) -> str:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("a lease needs a nonempty actor")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 86_400
        ):
            raise ValueError("lease ttl_seconds must be between 0 and 86400")
        try:
            return WorkerClass(worker_class).value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown worker class {worker_class!r}") from exc

    def acquire_lease(
        self, proof_id: str, lease_id: str, *, actor: str, worker_class: str,
        ttl_seconds: float = 300.0,
    ) -> int:
        """Take an exclusive proof-wide lease, superseding all dispatches."""
        worker_class = self._validate_holder(actor, worker_class, ttl_seconds)
        with self._write() as conn:
            now_ns = self._clock_ns()
            self._expire_due_leases(conn, proof_id, now_ns)
            rows = conn.execute(
                "SELECT * FROM leases WHERE proof_id = ? AND status = 'active'",
                (proof_id,),
            ).fetchall()
            self._supersede_leases(conn, rows, now_ns)
            lease = self._insert_lease(
                conn, proof_id, lease_id, actor, worker_class, None,
                self.head(proof_id)[0], ttl_seconds, now_ns, "acquired",
            )
        return lease.fencing_token

    def issue_lease(
        self, proof_id: str, lease_id: str, *, actor: str, worker_class: str,
        selected_move_id: str, base_revision: int | None = None,
        ttl_seconds: float = 600.0,
        score_snapshot: dict[str, Any] | None = None,
    ) -> LeaseRow:
        """Dispatch one move; distinct moves may hold leases concurrently.

        Reissuing a move renews ownership with a new token. The trusted
        scheduler binds the worker identity; proposals must name this move.
        """
        worker_class = self._validate_holder(actor, worker_class, ttl_seconds)
        if (
            not isinstance(selected_move_id, str)
            or not selected_move_id.startswith(f"{proof_id}/")
            or selected_move_id == f"{proof_id}/"
        ):
            raise ValueError("a dispatch lease needs a move in the proof namespace")
        with self._write() as conn:
            now_ns = self._clock_ns()
            self._expire_due_leases(conn, proof_id, now_ns)
            exclusive = conn.execute(
                """
                SELECT 1 FROM leases
                WHERE proof_id = ? AND status = 'active' AND selected_move_id IS NULL
                """,
                (proof_id,),
            ).fetchone()
            if exclusive is not None:
                raise ConcurrencyError(
                    Reason.LEASE_NOT_HELD, "proof is held by an exclusive lease"
                )
            revision = self.head(proof_id)[0]
            if base_revision is not None and (
                type(base_revision) is not int or base_revision != revision
            ):
                raise ConcurrencyError(
                    Reason.STALE_BASE_REVISION,
                    f"dispatch is based on revision {base_revision!r}, head is {revision}",
                )
            rows = conn.execute(
                """
                SELECT * FROM leases WHERE proof_id = ? AND status = 'active'
                AND (selected_move_id = ? OR lease_id = ?)
                """,
                (proof_id, selected_move_id, lease_id),
            ).fetchall()
            self._supersede_leases(conn, rows, now_ns)
            return self._insert_lease(
                conn, proof_id, lease_id, actor, worker_class, selected_move_id,
                revision, ttl_seconds, now_ns, "issued", score_snapshot,
            )

    def claim_next(
        self, proof_id: str, candidates: Sequence[LeaseCandidate], *,
        actor: str, worker_class: str, base_revision: int,
        ttl_seconds: float = 600.0,
    ) -> LeaseRow | None:
        """Reserve the first free candidate without superseding another worker.

        Ranking happens outside the lock; the revision check ensures it still
        describes committed state when the reservation is made.
        """
        worker_class = self._validate_holder(actor, worker_class, ttl_seconds)
        for candidate in candidates:
            if not candidate.move_id.startswith(f"{proof_id}/") or candidate.move_id == f"{proof_id}/":
                raise ValueError("a dispatch lease needs a move in the proof namespace")
        with self._write() as conn:
            now_ns = self._clock_ns()
            self._expire_due_leases(conn, proof_id, now_ns)
            revision = self.head(proof_id)[0]
            if type(base_revision) is not int or base_revision != revision:
                raise ConcurrencyError(
                    Reason.STALE_BASE_REVISION,
                    f"dispatch is based on revision {base_revision!r}, head is {revision}",
                )
            busy = {row["selected_move_id"] for row in conn.execute(
                "SELECT selected_move_id FROM leases WHERE proof_id = ? AND status = 'active'",
                (proof_id,),
            )}
            if None in busy:  # An exclusive proof-wide lease blocks dispatch.
                return None
            for candidate in candidates:
                if busy.intersection((candidate.move_id, *candidate.conflict_ids)):
                    continue
                row = conn.execute(
                    "SELECT MAX(fencing_token) AS top FROM leases WHERE proof_id = ?", (proof_id,),
                ).fetchone()
                serial = (row["top"] or 0) + 1
                while conn.execute(
                    "SELECT 1 FROM leases WHERE proof_id = ? AND lease_id = ?",
                    (proof_id, f"{proof_id}/ls-{serial}"),
                ).fetchone() is not None:
                    serial += 1
                return self._insert_lease(
                    conn, proof_id, f"{proof_id}/ls-{serial}", actor, worker_class,
                    candidate.move_id, revision, ttl_seconds, now_ns, "issued",
                    candidate.score_snapshot,
                )
        return None

    def _insert_lease(
        self, conn: sqlite3.Connection, proof_id: str, lease_id: str,
        actor: str, worker_class: str, selected_move_id: str | None,
        base_revision: int, ttl_seconds: float, now_ns: int, event: str,
        score_snapshot: dict[str, Any] | None = None,
    ) -> LeaseRow:
        row = conn.execute(
            "SELECT MAX(fencing_token) AS top FROM leases WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        token = (row["top"] or 0) + 1
        expires_at_ns = now_ns + max(1, int(ttl_seconds * 1_000_000_000))
        conn.execute(
            """
            INSERT INTO leases (
                proof_id, lease_id, fencing_token, actor, worker_class,
                expires_at_ns, selected_move_id, base_revision,
                ttl_seconds, issued_at_ns, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(proof_id, lease_id) DO UPDATE SET
                fencing_token = excluded.fencing_token,
                actor = excluded.actor, worker_class = excluded.worker_class,
                expires_at_ns = excluded.expires_at_ns,
                selected_move_id = excluded.selected_move_id,
                base_revision = excluded.base_revision,
                ttl_seconds = excluded.ttl_seconds,
                issued_at_ns = excluded.issued_at_ns, status = 'active'
            """,
            (proof_id, lease_id, token, actor, worker_class, expires_at_ns,
             selected_move_id, base_revision, ttl_seconds, now_ns),
        )
        row = conn.execute(
            "SELECT * FROM leases WHERE proof_id = ? AND lease_id = ?",
            (proof_id, lease_id),
        ).fetchone()
        self._record_lease_event(conn, row, event, now_ns, score_snapshot)
        return self._lease_row(row)

    def release_lease(
        self, proof_id: str, lease_id: str, *, fencing_token: int,
    ) -> bool:
        """Release only this generation, so a late release cannot kill a renewal."""
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("release needs a positive integer fencing token")
        with self._write() as conn:
            now_ns = self._clock_ns()
            self._expire_due_leases(conn, proof_id, now_ns)
            row = conn.execute(
                """
                SELECT * FROM leases WHERE proof_id = ? AND lease_id = ?
                AND fencing_token = ? AND status = 'active'
                """,
                (proof_id, lease_id, fencing_token),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE leases SET status = 'released' WHERE proof_id = ? AND lease_id = ?",
                (proof_id, lease_id),
            )
            self._record_lease_event(conn, row, "released", now_ns)
        return True

    def active_leases(self, proof_id: str) -> list[LeaseRow]:
        with self._write() as conn:
            self._expire_due_leases(conn, proof_id, self._clock_ns())
            rows = conn.execute(
                """
                SELECT * FROM leases WHERE proof_id = ? AND status = 'active'
                ORDER BY fencing_token
                """,
                (proof_id,),
            ).fetchall()
        return [self._lease_row(row) for row in rows]

    def read_lease_events(self, proof_id: str) -> Sequence[dict[str, Any]]:
        """Lease history lives in the existing audit table, outside proof history."""
        rows = self._conn.execute(
            """
            SELECT *, datetime(occurred_at_ns / 1000000000, 'unixepoch') AS created_at
            FROM lease_audit WHERE proof_id = ? ORDER BY seq
            """,
            (proof_id,),
        ).fetchall()
        return [
            {
                "seq": row["seq"], "lease_id": row["lease_id"],
                "event": row["event_type"], "fencing_token": row["fencing_token"],
                "actor": row["actor"], "worker_class": row["worker_class"],
                "selected_move_id": row["selected_move_id"],
                "base_revision": row["base_revision"],
                "score_snapshot": (
                    json.loads(row["score_snapshot"]) if row["score_snapshot"] else None
                ),
                "occurred_at_ns": row["occurred_at_ns"],
                "expires_at_ns": row["expires_at_ns"], "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _lease_row(row: sqlite3.Row) -> LeaseRow:
        return LeaseRow(
            proof_id=row["proof_id"], lease_id=row["lease_id"],
            actor=row["actor"], worker_class=row["worker_class"],
            selected_move_id=row["selected_move_id"], fencing_token=row["fencing_token"],
            base_revision=row["base_revision"], ttl_seconds=row["ttl_seconds"],
            issued_at=row["issued_at_ns"] / 1_000_000_000,
            expires_at=(row["expires_at_ns"] / 1_000_000_000
                        if row["expires_at_ns"] is not None else None),
            status=row["status"],
        )

    @staticmethod
    def _record_lease_event(
        conn: sqlite3.Connection, row: sqlite3.Row, event: str, now_ns: int,
        score_snapshot: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lease_audit (
                proof_id, lease_id, fencing_token, actor, worker_class,
                event_type, occurred_at_ns, expires_at_ns,
                selected_move_id, score_snapshot, base_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["proof_id"], row["lease_id"], row["fencing_token"],
             row["actor"], row["worker_class"], event, now_ns, row["expires_at_ns"],
             row["selected_move_id"],
             canonical_json(score_snapshot).decode("utf-8")
             if score_snapshot is not None else None, row["base_revision"]),
        )

    def _supersede_leases(
        self, conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], now_ns: int,
    ) -> None:
        for row in rows:
            conn.execute(
                "UPDATE leases SET status = 'superseded' WHERE proof_id = ? AND lease_id = ?",
                (row["proof_id"], row["lease_id"]),
            )
            self._record_lease_event(conn, row, "superseded", now_ns)

    def _expire_due_leases(
        self, conn: sqlite3.Connection, proof_id: str, now_ns: int,
    ) -> None:
        rows = conn.execute(
            """
            SELECT * FROM leases WHERE proof_id = ? AND status = 'active'
            AND expires_at_ns <= ?
            """,
            (proof_id, now_ns),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE leases SET status = 'expired' WHERE proof_id = ? AND lease_id = ?",
                (proof_id, row["lease_id"]),
            )
            self._record_lease_event(conn, row, "expired", now_ns)

    def append(self, payload_dict: dict[str, Any]) -> tuple[int, str]:
        """Append one already-validated proposal; return `(revision, event_hash)`.

        Reads the head, checks the proposal's concurrency expectations against
        it, chains onto it, and inserts — all under one write lock, so the head
        cannot move between the check and the insert.

        Raises `HashChainError` if the head's own hash does not match its
        payload: chaining onto a corrupt hash would bury the corruption under
        a link that verifies.
        """
        try:
            return self._append(payload_dict)
        except ConcurrencyError as exc:
            if exc.reason is Reason.LEASE_EXPIRED:
                # The rejected append rolls back. Persist expiry in its own
                # transaction so a failed worker write cannot erase the audit.
                with self._write() as conn:
                    self._expire_due_leases(conn, payload_dict["proof_id"], self._clock_ns())
            raise

    def _append(self, payload_dict: dict[str, Any]) -> tuple[int, str]:
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
                    self._clock_ns(), payload_dict.get("selected_move_id"),
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
        selected_move_id: str | None,
    ) -> None:
        """Bind a live token to its worker and, for dispatches, its move."""
        row = conn.execute(
            "SELECT * FROM leases WHERE proof_id = ? AND lease_id = ?",
            (proof_id, lease_id),
        ).fetchone()
        if row is None:
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD, f"no lease is held on {proof_id!r}"
            )
        if type(fencing_token) is not int or row["fencing_token"] != fencing_token:
            raise ConcurrencyError(
                Reason.FENCING_TOKEN_SUPERSEDED,
                f"fencing token {fencing_token!r} is superseded by {row['fencing_token']!r}",
            )
        if row["actor"] != actor or row["worker_class"] != worker_class:
            raise ConcurrencyError(
                Reason.LEASE_IDENTITY_MISMATCH,
                "proposal actor and worker class do not match the lease holder",
            )
        if row["selected_move_id"] is not None and row["selected_move_id"] != selected_move_id:
            raise ConcurrencyError(
                Reason.LEASE_MOVE_MISMATCH, "proposal move does not match the leased move"
            )
        if row["expires_at_ns"] is None or now_ns >= row["expires_at_ns"]:
            raise ConcurrencyError(
                Reason.LEASE_EXPIRED, f"lease {lease_id!r} has expired"
            )
        if row["status"] != "active":
            raise ConcurrencyError(
                Reason.LEASE_NOT_HELD,
                f"lease {lease_id!r} is {row['status']}, not active",
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
