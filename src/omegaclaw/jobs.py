"""Operational ledger; deliberately outside the proof journal hash chain."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


class RequestConflict(ValueError):
    pass


class JobLedger:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
            body_hash TEXT NOT NULL, workspace TEXT NOT NULL,
            operation TEXT NOT NULL, phase TEXT NOT NULL, response TEXT NOT NULL
        )""")

    def submit(self, request: dict) -> tuple[dict, bool]:
        body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM operations WHERE request_id=?", (request["request_id"],)).fetchone()
            if row:
                if row["body_hash"] != digest:
                    raise RequestConflict("request_id reused with different body")
                result = self._decode(row)
                created = False
            else:
                operation_id = str(uuid.uuid4())
                result = {"operation_id": operation_id, "phase": "submitted", "transport_status": "accepted"}
                if request["operation"] == "proof-cycle":
                    result["proof_id"] = request["arguments"]["proof"]
                self.conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", (
                    operation_id, request["request_id"], digest, request["workspace"],
                    request["operation"], "submitted", json.dumps(result, sort_keys=True),
                ))
                created = True
            self.conn.execute("COMMIT")
            return result, created
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return json.loads(row["response"])

    def update(self, operation_id: str, response: dict) -> None:
        self.conn.execute("UPDATE operations SET phase=?, response=? WHERE operation_id=?", (
            response["phase"], json.dumps(response, sort_keys=True), operation_id,
        ))

    def get(self, operation_id: str, workspace: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM operations WHERE operation_id=? AND workspace=?", (operation_id, workspace)).fetchone()
        return self._decode(row) if row else None

    def active_cycle(self, proof_id: str, workspace: str, *, excluding_request_id: str | None = None) -> dict | None:
        # Operational deduplication only while a cycle is in flight.
        rows = self.conn.execute("SELECT request_id, response FROM operations WHERE workspace=? AND operation='proof-cycle' AND phase NOT IN ('finished','failed')", (workspace,)).fetchall()
        for row in rows:
            if row["request_id"] == excluding_request_id:
                continue
            result = json.loads(row["response"])
            if result.get("proof_id") == proof_id:
                return result
        return None
