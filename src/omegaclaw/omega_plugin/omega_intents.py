"""Durable prepared SDK requests for trusted Omega tool-call intents.

The host supplies intent identity. Mathematical state remains receiver-owned.
This module is copied alongside the bridge into derived Omega installations.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from persistent_atp_client import ActionRequest, OperationRef


class IntentStore:
    def __init__(self, path: str, receiver_id: str):
        if not receiver_id or not path:
            raise ValueError("durable intents require a path and receiver/principal identity")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(target, isolation_level=None)
        names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names.intersection({"journal", "requests", "operations", "executions"}):
            self.db.close()
            raise ValueError("intent storage must be separate from receiver and proof databases")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS intents (
            receiver_id TEXT NOT NULL, workspace TEXT NOT NULL, intent_id TEXT NOT NULL,
            arguments_hash TEXT NOT NULL, request TEXT NOT NULL, operation_ref TEXT,
            PRIMARY KEY(receiver_id,workspace,intent_id))""")
        self.receiver_id = receiver_id

    def run(self, client, *, workspace: str, intent_id: str, arguments: dict, prepare):
        if not isinstance(intent_id, str) or not 1 <= len(intent_id) <= 256:
            raise ValueError("invalid trusted intent identity")
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (self.receiver_id, workspace, intent_id)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT arguments_hash,request,operation_ref FROM intents WHERE receiver_id=? AND workspace=? AND intent_id=?", key).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("intent identity was reused with different arguments")
                request = ActionRequest.from_dict(json.loads(row[1]))
                reference = OperationRef.from_dict(json.loads(row[2])) if row[2] else None
            else:
                request = prepare()
                reference = None
                self.db.execute("INSERT INTO intents VALUES (?,?,?,?,?,NULL)",
                    (*key, digest, json.dumps(request.to_dict(), sort_keys=True, ensure_ascii=False)))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        # Committed preparation precedes delivery. Ambiguous submission leaves
        # the same bytes on disk; a new host deliberately retries that request.
        handle = client.attach(reference) if reference else client.submit(request)
        self.db.execute("UPDATE intents SET operation_ref=? WHERE receiver_id=? AND workspace=? AND intent_id=?",
                        (json.dumps(handle.ref.to_dict(), sort_keys=True), *key))
        return request, client.wait(handle, timeout=10)

    def close(self):
        self.db.close()
