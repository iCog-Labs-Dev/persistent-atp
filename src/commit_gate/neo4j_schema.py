"""Schema DDL and guard queries for the Neo4j-backed commit_gate graph.

Two genuinely different mechanisms, deliberately kept separate:

Schema DDL (`ensure_constraints`) -- invariants Neo4j *uniqueness*
constraints can enforce natively, at write time, on Community Edition (the
common case for local dev and CI): every GateNode's gate_id is unique, and
every relationship's gate_edge_id is unique per relationship type (one
constraint per type -- Neo4j scopes a relationship constraint to a single
literal type, same restriction as a MERGE pattern's type). A violating
write is rejected outright; the bad state never exists to be found later.

Guard queries (`find_bad_edge_endpoints`, `find_nodes_missing_label`) --
for invariants native constraints cannot express on this edition, or
cannot express at all. `find_bad_edge_endpoints` mirrors validate.py's own
check_references, but runs against what's actually landed in the graph --
catching a bug in the projector, or a write that bypassed it entirely.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, ManagedTransaction

from .neo4j_readview import ReadViewQueryError, ReadViewUnavailable, _translate
from .validate import EDGE_ENDPOINTS

load_dotenv()

__all__ = [
    "ensure_constraints",
    "find_bad_edge_endpoints",
    "find_nodes_missing_label",
    "ReadViewUnavailable",
    "ReadViewQueryError",
]


def _driver_from_env(
    driver: Optional[Driver], uri: Optional[str], user: Optional[str], password: Optional[str]
) -> tuple[Driver, bool]:
    if driver is not None:
        return driver, False
    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    if password is None:
        password = os.environ.get("NEO4J_PASSWORD", "")
    try:
        d = GraphDatabase.driver(uri, auth=(user, password))
        d.verify_connectivity()
        return d, True
    except Exception as exc:
        raise _translate("connect", exc) from exc


def ensure_constraints(
    driver: Optional[Driver] = None,
    *,
    database: Optional[str] = None,
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """Create every schema constraint, idempotently (IF NOT EXISTS).

    Safe to call on every process start -- a repeat call against an
    already-constrained database is a fast no-op per statement.
    """
    resolved_driver, owns_driver = _driver_from_env(driver, uri, user, password)
    database = database or os.environ.get("NEO4J_DATABASE") or None
    try:
        with resolved_driver.session(database=database) as session:
            session.run("DROP CONSTRAINT gate_node_label_exists IF EXISTS")
            session.run(
                "CREATE CONSTRAINT gate_node_id_unique IF NOT EXISTS "
                "FOR (n:GateNode) REQUIRE n.gate_id IS UNIQUE"
            )
            for rel_type in EDGE_ENDPOINTS:
                # One constraint per type: Neo4j scopes a relationship
                # constraint to a single, literal type -- it cannot be
                # parameterized any more than a MERGE pattern's type can.
                constraint_name = f"gate_edge_id_unique_{rel_type.lower()}"
                session.run(
                    f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                    f"FOR ()-[r:{rel_type}]-() REQUIRE r.gate_edge_id IS UNIQUE"
                )
    except Exception as exc:
        raise _translate("ensure_constraints", exc) from exc
    finally:
        if owns_driver:
            resolved_driver.close()


def find_bad_edge_endpoints(
    driver: Optional[Driver] = None,
    *,
    database: Optional[str] = None,
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every edge whose actual endpoint labels don't match EDGE_ENDPOINTS.

    Empty list means the graph is clean. Each violation reports enough to
    locate and fix it: the edge id, its relationship type, what the source
    and destination labels actually are, and what EDGE_ENDPOINTS expects.
    """
    resolved_driver, owns_driver = _driver_from_env(driver, uri, user, password)
    database = database or os.environ.get("NEO4J_DATABASE") or None
    violations: list[dict[str, Any]] = []
    try:
        with resolved_driver.session(database=database) as session:
            for rel_type, (expected_src, expected_dst) in EDGE_ENDPOINTS.items():
                records = session.execute_read(
                    _find_bad_endpoints_tx, rel_type=rel_type,
                    expected_src=expected_src, expected_dst=expected_dst,
                )
                violations.extend(records)
        return violations
    except Exception as exc:
        raise _translate("find_bad_edge_endpoints", exc) from exc
    finally:
        if owns_driver:
            resolved_driver.close()


def _find_bad_endpoints_tx(
    tx: ManagedTransaction, rel_type: str, expected_src: str, expected_dst: str
) -> list[dict[str, Any]]:
    records = tx.run(
        "MATCH (a:GateNode)-[r]->(b:GateNode) "
        "WHERE type(r) = $rel_type "
        "AND (a.gate_label <> $expected_src OR b.gate_label <> $expected_dst) "
        "RETURN r.gate_edge_id AS edge_id, type(r) AS rel_type, "
        "       a.gate_label AS actual_src, b.gate_label AS actual_dst",
        rel_type=rel_type, expected_src=expected_src, expected_dst=expected_dst,
    )
    return [
        {
            "edge_id": rec["edge_id"],
            "rel_type": rec["rel_type"],
            "actual": (rec["actual_src"], rec["actual_dst"]),
            "expected": (expected_src, expected_dst),
        }
        for rec in records
    ]


def find_nodes_missing_label(
    driver: Optional[Driver] = None,
    *,
    database: Optional[str] = None,
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> list[str]:
    """Every GateNode with no gate_label. Empty list means the graph is clean.
    """
    resolved_driver, owns_driver = _driver_from_env(driver, uri, user, password)
    database = database or os.environ.get("NEO4J_DATABASE") or None
    try:
        with resolved_driver.session(database=database) as session:
            return session.execute_read(_find_missing_label_tx)
    except Exception as exc:
        raise _translate("find_nodes_missing_label", exc) from exc
    finally:
        if owns_driver:
            resolved_driver.close()


def _find_missing_label_tx(tx: ManagedTransaction) -> list[str]:
    records = tx.run(
        "MATCH (n:GateNode) WHERE n.gate_label IS NULL RETURN n.gate_id AS gate_id"
    )
    return [rec["gate_id"] for rec in records]