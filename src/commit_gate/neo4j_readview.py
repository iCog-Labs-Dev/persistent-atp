"""A `ReadView` (see `state.py`) backed by a live Neo4j database.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, ManagedTransaction
from neo4j.exceptions import (
    AuthError,
    ConfigurationError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
)

from .state import EdgeRecord, NodeRecord

load_dotenv()


__all__ = ["Neo4jReadView", "ReadViewUnavailable", "ReadViewQueryError"]
 
 
class ReadViewUnavailable(Exception):
    """The graph backing this ReadView could not be reached, or auth/config was rejected."""
 
 
class ReadViewQueryError(Exception):
    """A query against this ReadView's backing graph failed for a reason other than unavailability."""
 
 
def _translate(operation: str, exc: BaseException) -> BaseException:
    if isinstance(exc, (ServiceUnavailable, SessionExpired, AuthError, ConfigurationError)):
        return ReadViewUnavailable(f"{operation}: {exc}")
    if isinstance(exc, (Neo4jError, DriverError)):
        return ReadViewQueryError(f"{operation}: {exc}")
    return exc



class Neo4jReadView:
    """Read-only access to one Neo4j-backed graph, through the ReadView shape.

    Construct with an existing `neo4j.Driver` (recommended when a
    `Neo4jProjection` in the same process already owns one), or let this
    class build its own from the same NEO4J_URI / NEO4J_USER /
    NEO4J_PASSWORD / NEO4J_DATABASE environment variables the sibling
    `neo4j` adapter package already uses -- same convention (including
    loading a `.env` file via `python-dotenv`, see `.env.example`), so ops
    teams configure one set of variables for the whole system.
    """

    def __init__(
        self,
        driver: Optional[Driver] = None,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
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
            # Fail here, with GraphUnavailable, rather than on the first read.
            self._driver.verify_connectivity()
        except Exception as exc:
            raise _translate("connect", exc) from exc

    def close(self) -> None:
        """Close the driver, but only if this instance created it."""
        if self._owns_driver:
            self._driver.close()

    def __enter__(self) -> "Neo4jReadView":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- ReadView ----------------------------------------------------------

    def node(self, node_id: str) -> NodeRecord | None:
        return self._read("node", self._node_tx, node_id=node_id)

    def edge(self, edge_id: str) -> EdgeRecord | None:
        return self._read("edge", self._edge_tx, edge_id=edge_id)

    def edges_from(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return self._read(
            "edges_from", self._edges_from_tx, node_id=node_id, rel_type=rel_type
        )

    def edges_to(self, node_id: str, rel_type: str) -> tuple[EdgeRecord, ...]:
        return self._read(
            "edges_to", self._edges_to_tx, node_id=node_id, rel_type=rel_type
        )

    # -- transaction functions ----------------------------------------------
    # `execute_read` may retry these on a transient failure (leader switch,
    # dropped connection), so each must be a pure function of its arguments
    # with no side effects of its own.

    @staticmethod
    def _node_tx(tx: ManagedTransaction, node_id: str) -> NodeRecord | None:
        record = tx.run(
            "MATCH (n:GateNode {gate_id: $node_id}) "
            "RETURN n AS props, n.gate_label AS label",
            node_id=node_id,
        ).single()
        if record is None:
            return None
        return _to_node_record(node_id, record["label"], record["props"])

    @staticmethod
    def _edge_tx(tx: ManagedTransaction, edge_id: str) -> EdgeRecord | None:
        record = tx.run(
            "MATCH (a:GateNode)-[r {gate_edge_id: $edge_id}]->(b:GateNode) "
            "RETURN r AS props, type(r) AS rel_type, a.gate_id AS src_id, b.gate_id AS dst_id",
            edge_id=edge_id,
        ).single()
        if record is None:
            return None
        return _to_edge_record(
            edge_id, record["rel_type"], record["src_id"], record["dst_id"], record["props"]
        )

    @staticmethod
    def _edges_from_tx(
        tx: ManagedTransaction, node_id: str, rel_type: str
    ) -> tuple[EdgeRecord, ...]:
        records = tx.run(
            "MATCH (a:GateNode {gate_id: $node_id})-[r]->(b:GateNode) "
            "WHERE type(r) = $rel_type "
            "RETURN r AS props, r.gate_edge_id AS edge_id, b.gate_id AS dst_id",
            node_id=node_id, rel_type=rel_type,
        )
        return tuple(
            _to_edge_record(rec["edge_id"], rel_type, node_id, rec["dst_id"], rec["props"])
            for rec in records
        )

    @staticmethod
    def _edges_to_tx(
        tx: ManagedTransaction, node_id: str, rel_type: str
    ) -> tuple[EdgeRecord, ...]:
        records = tx.run(
            "MATCH (a:GateNode)-[r]->(b:GateNode {gate_id: $node_id}) "
            "WHERE type(r) = $rel_type "
            "RETURN r AS props, r.gate_edge_id AS edge_id, a.gate_id AS src_id",
            node_id=node_id, rel_type=rel_type,
        )
        return tuple(
            _to_edge_record(rec["edge_id"], rel_type, rec["src_id"], node_id, rec["props"])
            for rec in records
        )

    def _read(self, operation: str, work: Any, **kwargs: Any) -> Any:
        try:
            with self._driver.session(database=self._database) as session:
                return session.execute_read(work, **kwargs)
        except Exception as exc:
            raise _translate(operation, exc) from exc


def _to_node_record(node_id: str, label: str, neo4j_node: Any) -> NodeRecord:
    fields = {k: v for k, v in dict(neo4j_node).items() if k not in ("gate_id", "gate_label")}
    return NodeRecord(node_id=node_id, label=label, fields=fields)


def _to_edge_record(
    edge_id: str, rel_type: str, src_id: str, dst_id: str, neo4j_rel: Any
) -> EdgeRecord:
    fields = {k: v for k, v in dict(neo4j_rel).items() if k != "gate_edge_id"}
    return EdgeRecord(edge_id=edge_id, rel_type=rel_type, src_id=src_id, dst_id=dst_id, fields=fields)