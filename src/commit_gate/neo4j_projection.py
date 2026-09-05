"""Applies committed journal events onto a live Neo4j graph.

Reads events from a `JournalStore` (the SQL source of truth) via
`read_events_since`, and replays each committed proposal's ops onto Neo4j
using the same schema `Neo4jReadView` reads: nodes as `:GateNode {gate_id,
gate_label, ...fields}`; edges as typed relationships carrying
`gate_edge_id`.

One journal event -> one Neo4j write transaction, which also advances the
watermark (a `:ProjectionWatermark {proof_id, revision}` node) to that
event's revision, in the SAME transaction as the graph writes. Neo4j
transactions are all-or-nothing, so a crash mid-event can never leave the
watermark ahead of what was actually written -- `catch_up` just picks back
up from wherever it left off, safely, on the next call.

"""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, ManagedTransaction

from .neo4j_readview import ReadViewQueryError, ReadViewUnavailable, _translate
from .ops import AddEdge, Op, RemoveEdge, SetField, UpsertNode, ops_from_dicts
from .store import JournalStore
from .validate import EDGE_ENDPOINTS

load_dotenv()

__all__ = ["Neo4jProjection", "ReadViewUnavailable", "ReadViewQueryError"]


class Neo4jProjection:
    """Replays a `JournalStore`'s committed events onto a live Neo4j graph.

    Construct with an existing `neo4j.Driver` (recommended when a
    `Neo4jReadView` in the same process already owns one -- share it rather
    than opening a second connection pool), or let this class build its own
    from the same NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE
    environment variables (see `.env.example`).
    """

    def __init__(
        self,
        store: JournalStore,
        driver: Optional[Driver] = None,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
        self._store = store
        self._database = database or os.environ.get("NEO4J_DATABASE") or None
        self._owns_driver = driver is None
        if driver is not None:
            self._driver = driver
            return

        uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = user or os.environ.get("NEO4J_USER", "neo4j")
        if password is None:
            password = os.environ.get("NEO4J_PASSWORD", "")
        try:
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            self._driver.verify_connectivity()
        except Exception as exc:
            raise _translate("connect", exc) from exc

    def close(self) -> None:
        """Close the driver, but only if this instance created it."""
        if self._owns_driver:
            self._driver.close()

    def __enter__(self) -> "Neo4jProjection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- public API ----------------------------------------------------------

    def watermark(self, proof_id: str) -> int:
        """The revision this projection has applied up to. 0 if none yet."""
        return self._read("watermark", self._watermark_tx, proof_id=proof_id)

    def catch_up(self, proof_id: str) -> int:
        """Apply every event committed since the watermark, in order.

        Returns how many events were applied. Safe to call repeatedly --
        each call only ever moves forward from wherever the watermark is
        right now, and a crash partway through leaves it at the last event
        that genuinely committed, not ahead of it.
        """
        applied = 0
        current = self.watermark(proof_id)
        for revision, payload in self._store.read_events_since(proof_id, current):
            ops = ops_from_dicts(payload["ops"])
            try:
                with self._driver.session(database=self._database) as session:
                    session.execute_write(
                        self._apply_event_tx, proof_id=proof_id, revision=revision, ops=ops
                    )
            except Exception as exc:
                raise _translate("catch_up", exc) from exc
            applied += 1
        return applied

    # -- transaction functions ------------------------------------------------
    # `execute_write` may retry `_apply_event_tx` on a transient failure, so
    # it must be a pure function of its arguments -- no closures over mutable
    # state, no side effects outside `tx.run`.

    @staticmethod
    def _watermark_tx(tx: ManagedTransaction, proof_id: str) -> int:
        record = tx.run(
            "MATCH (w:ProjectionWatermark {proof_id: $proof_id}) RETURN w.revision AS revision",
            proof_id=proof_id,
        ).single()
        return record["revision"] if record is not None else 0

    @classmethod
    def _apply_event_tx(
        cls, tx: ManagedTransaction, proof_id: str, revision: int, ops: list[Op]
    ) -> None:
        for op in ops:
            cls._apply_op(tx, op)
        tx.run(
            "MERGE (w:ProjectionWatermark {proof_id: $proof_id}) SET w.revision = $revision",
            proof_id=proof_id, revision=revision,
        )

    @staticmethod
    def _apply_op(tx: ManagedTransaction, op: Op) -> None:
        if isinstance(op, UpsertNode):
            # ON CREATE only, no ON MATCH: a re-upsert of an existing node
            # must be a no-op, exactly like apply.py's MemoryView handling.
            tx.run(
                "MERGE (n:GateNode {gate_id: $id}) "
                "ON CREATE SET n.gate_label = $label, n += $fields",
                id=op.node_id, label=op.label, fields=dict(op.fields),
            )
        elif isinstance(op, SetField):
            tx.run(
                "MATCH (n:GateNode {gate_id: $id}) SET n[$field] = $value",
                id=op.node_id, field=op.field, value=op.value,
            )
        elif isinstance(op, AddEdge):
            if op.rel_type not in EDGE_ENDPOINTS:
                # Defense in depth: the gate's own check_references already
                # rejects unknown relationship types before an op is ever
                # journaled, so this should be unreachable. Checked again
                # here because the next line interpolates rel_type directly
                # into Cypher -- Cypher cannot parameterize a relationship
                # type in a CREATE/MERGE pattern -- and this is the last
                # line of defense against a validation gap ever becoming a
                # Cypher-injection path.
                raise ValueError(f"refusing to write unknown relationship type {op.rel_type!r}")
            tx.run(
                f"MATCH (a:GateNode {{gate_id: $src}}), (b:GateNode {{gate_id: $dst}}) "
                f"MERGE (a)-[r:{op.rel_type} {{gate_edge_id: $edge_id}}]->(b) "
                f"ON CREATE SET r += $fields",
                src=op.src_id, dst=op.dst_id, edge_id=op.edge_id, fields=dict(op.fields or {}),
            )
        elif isinstance(op, RemoveEdge):
            tx.run(
                "MATCH ()-[r {gate_edge_id: $edge_id}]-() WHERE type(r) = $rel_type DELETE r",
                edge_id=op.edge_id, rel_type=op.rel_type,
            )
        else:
            raise ValueError(f"unknown op type: {op!r}")

    def _read(self, operation: str, work: Any, **kwargs: Any) -> Any:
        try:
            with self._driver.session(database=self._database) as session:
                return session.execute_read(work, **kwargs)
        except Exception as exc:
            raise _translate(operation, exc) from exc