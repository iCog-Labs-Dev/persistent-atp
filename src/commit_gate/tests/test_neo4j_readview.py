"""Integration tests for Neo4jReadView, against a REAL Neo4j server.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest

from commit_gate.neo4j_readview import Neo4jReadView, ReadViewUnavailable

RUN_INTEGRATION = os.environ.get("RUN_NEO4J_INTEGRATION_TESTS", "").strip().lower() in (
    "1", "true", "yes",
)

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason=(
        "RUN_NEO4J_INTEGRATION_TESTS is not set; skipping tests against a "
        "live Neo4j server"
    ),
)


@pytest.fixture(scope="module")
def view() -> Iterator[Neo4jReadView]:
    try:
        v = Neo4jReadView()
    except ReadViewUnavailable as exc:
        pytest.skip(f"opted in via RUN_NEO4J_INTEGRATION_TESTS, but Neo4j is unreachable: {exc}")
        return
    yield v
    v.close()


@pytest.fixture
def prefix() -> str:
    """A unique id prefix per test, so tests never clash with each other or
    with anything already in the database, and cleanup only ever touches
    what this specific test wrote."""
    return f"pytest_readview/{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(view: Neo4jReadView, prefix: str) -> Iterator[None]:
    yield
    with view._driver.session(database=view._database) as session:
        session.run(
            "MATCH (n:GateNode) WHERE n.gate_id STARTS WITH $prefix DETACH DELETE n",
            prefix=prefix,
        )


def _write_node(view: Neo4jReadView, node_id: str, label: str, **fields: object) -> None:
    with view._driver.session(database=view._database) as session:
        session.run(
            "MERGE (n:GateNode {gate_id: $id}) SET n.gate_label = $label, n += $fields",
            id=node_id, label=label, fields=fields,
        )


def _write_edge(
    view: Neo4jReadView, src_id: str, dst_id: str, rel_type: str, edge_id: str, **fields: object
) -> None:
    # rel_type is interpolated directly here, unlike ReadView's own read-side
    # queries -- safe *only* because this is test-fixture-writing code where
    # rel_type is always a hardcoded literal from within this file, never
    # external input. ReadView itself never does this (see neo4j_readview.py).
    with view._driver.session(database=view._database) as session:
        session.run(
            f"MATCH (a:GateNode {{gate_id: $src}}), (b:GateNode {{gate_id: $dst}}) "
            f"MERGE (a)-[r:{rel_type} {{gate_edge_id: $eid}}]->(b) SET r += $fields",
            src=src_id, dst=dst_id, eid=edge_id, fields=fields,
        )


# ---------------------------------------------------------------------------
# node()
# ---------------------------------------------------------------------------


def test_node_returns_the_record_with_label_and_fields(view: Neo4jReadView, prefix: str):
    node_id = f"{prefix}/claim1"
    _write_node(view, node_id, "Claim", status="formally-closed", note="hello")

    record = view.node(node_id)

    assert record is not None
    assert record.node_id == node_id
    assert record.label == "Claim"
    assert record.fields == {"status": "formally-closed", "note": "hello"}


def test_node_returns_none_for_an_unknown_id(view: Neo4jReadView, prefix: str):
    assert view.node(f"{prefix}/does-not-exist") is None


def test_node_fields_round_trip_a_boolean(view: Neo4jReadView, prefix: str):
    node_id = f"{prefix}/replay1"
    _write_node(view, node_id, "LeanReplay", sorry_detected=False, verified=True)

    record = view.node(node_id)

    assert record is not None
    assert record.fields["sorry_detected"] is False
    assert record.fields["verified"] is True


# ---------------------------------------------------------------------------
# edge()
# ---------------------------------------------------------------------------


def test_edge_returns_the_record_by_id_regardless_of_type(view: Neo4jReadView, prefix: str):
    src, dst = f"{prefix}/claim1", f"{prefix}/cert1"
    edge_id = f"{prefix}/e1"
    _write_node(view, src, "Claim")
    _write_node(view, dst, "Certificate", actor="producer-1")
    _write_edge(view, src, dst, "PROVED_BY", edge_id)

    record = view.edge(edge_id)

    assert record is not None
    assert record.edge_id == edge_id
    assert record.rel_type == "PROVED_BY"
    assert record.src_id == src
    assert record.dst_id == dst


def test_edge_returns_none_for_an_unknown_id(view: Neo4jReadView, prefix: str):
    assert view.edge(f"{prefix}/does-not-exist") is None


# ---------------------------------------------------------------------------
# edges_from() / edges_to()
# ---------------------------------------------------------------------------


def test_edges_from_filters_by_rel_type(view: Neo4jReadView, prefix: str):
    claim, cert, align = f"{prefix}/claim1", f"{prefix}/cert1", f"{prefix}/align1"
    _write_node(view, claim, "Claim")
    _write_node(view, cert, "Certificate")
    _write_node(view, align, "Alignment")
    _write_edge(view, claim, cert, "PROVED_BY", f"{prefix}/e1")
    _write_edge(view, align, claim, "ALIGNS_CLAIM", f"{prefix}/e2")

    proved_by = view.edges_from(claim, "PROVED_BY")

    assert len(proved_by) == 1
    assert proved_by[0].dst_id == cert


def test_edges_from_excludes_a_different_rel_type(view: Neo4jReadView, prefix: str):
    claim, cert = f"{prefix}/claim1", f"{prefix}/cert1"
    _write_node(view, claim, "Claim")
    _write_node(view, cert, "Certificate")
    _write_edge(view, claim, cert, "PROVED_BY", f"{prefix}/e1")

    assert view.edges_from(claim, "REPLAYED_BY") == ()


def test_edges_to_returns_multiple_matching_edges(view: Neo4jReadView, prefix: str):
    cert = f"{prefix}/cert1"
    claim_a, claim_b = f"{prefix}/claim_a", f"{prefix}/claim_b"
    _write_node(view, cert, "Certificate")
    _write_node(view, claim_a, "Claim")
    _write_node(view, claim_b, "Claim")
    _write_edge(view, claim_a, cert, "PROVED_BY", f"{prefix}/e1")
    _write_edge(view, claim_b, cert, "PROVED_BY", f"{prefix}/e2")

    incoming = view.edges_to(cert, "PROVED_BY")

    assert sorted(e.src_id for e in incoming) == sorted([claim_a, claim_b])


def test_edges_to_returns_empty_for_a_node_with_no_matching_incoming_edges(
    view: Neo4jReadView, prefix: str
):
    lonely = f"{prefix}/lonely"
    _write_node(view, lonely, "Claim")
    assert view.edges_to(lonely, "PROVED_BY") == ()