"""Integration tests for neo4j_schema.py, against a REAL Neo4j server.

    RUN_NEO4J_INTEGRATION_TESTS=1 pytest commit_gate/tests/test_neo4j_schema.py

Two things are tested, deliberately kept apart (see neo4j_schema.py's
module docstring for why): that `ensure_constraints` genuinely *rejects* a
violating write (not just "runs without error"), and that
`find_bad_edge_endpoints` genuinely *detects* a deliberately broken graph
(not just "returns empty on data that was already fine").
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import ConstraintError

from commit_gate.neo4j_readview import ReadViewUnavailable
from commit_gate.neo4j_schema import ensure_constraints, find_bad_edge_endpoints

RUN_INTEGRATION = os.environ.get("RUN_NEO4J_INTEGRATION_TESTS", "").strip().lower() in (
    "1", "true", "yes",
)

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason=(
        "RUN_NEO4J_INTEGRATION_TESTS is not set; skipping tests against a "
        "live Neo4j server (see this file's module docstring)"
    ),
)


@pytest.fixture(scope="module")
def driver():
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    try:
        d = GraphDatabase.driver(uri, auth=(user, password))
        d.verify_connectivity()
    except Exception as exc:
        pytest.skip(f"opted in via RUN_NEO4J_INTEGRATION_TESTS, but Neo4j is unreachable: {exc}")
        return
    yield d
    d.close()


@pytest.fixture(scope="module", autouse=True)
def _constraints_exist(driver) -> None:
    """Applied once for the whole module -- ensure_constraints is
    idempotent, so every test can rely on the constraints already being in
    place without re-running the DDL itself each time."""
    ensure_constraints(driver)


@pytest.fixture
def prefix() -> str:
    return f"pytest_schema/{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(driver, prefix: str) -> Iterator[None]:
    yield
    with driver.session() as session:
        session.run(
            "MATCH (n:GateNode) WHERE n.gate_id STARTS WITH $prefix DETACH DELETE n",
            prefix=prefix,
        )


def _write_node(driver, node_id: str, label: str, **fields: object) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (n:GateNode {gate_id: $id}) SET n.gate_label = $label, n += $fields",
            id=node_id, label=label, fields=fields,
        )


def _write_edge_raw(driver, src_id: str, dst_id: str, rel_type: str, edge_id: str) -> None:
    """Writes an edge directly, bypassing Neo4jProjection -- used here to
    simulate a bug or a manual/out-of-band write that check_references
    would normally have caught before journaling."""
    with driver.session() as session:
        session.run(
            f"MATCH (a:GateNode {{gate_id: $src}}), (b:GateNode {{gate_id: $dst}}) "
            f"MERGE (a)-[r:{rel_type} {{gate_edge_id: $eid}}]->(b)",
            src=src_id, dst=dst_id, eid=edge_id,
        )


# ---------------------------------------------------------------------------
# ensure_constraints: proves violations are genuinely REJECTED, not just
# that the DDL statements run without error.
# ---------------------------------------------------------------------------


def test_ensure_constraints_is_idempotent(driver):
    ensure_constraints(driver)  # a second call, on top of the module fixture's first
    ensure_constraints(driver)  # a third, for good measure


def test_duplicate_gate_id_is_rejected(driver, prefix: str):
    node_id = f"{prefix}/dup1"
    _write_node(driver, node_id, "Claim")

    with pytest.raises(ConstraintError):
        with driver.session() as session:
            session.run(
                "CREATE (n:GateNode {gate_id: $id, gate_label: $label})",
                id=node_id, label="Claim",
            )


def test_missing_gate_label_is_flagged_by_the_guard_query(driver, prefix: str):
    """Community Edition accepts an existence constraint without enforcing
    it (see neo4j_schema.py's module docstring) -- so this is a guard
    query, not a raised error. Prove it actually detects the violation."""
    from commit_gate.neo4j_schema import find_nodes_missing_label

    node_id = f"{prefix}/nolabel1"
    with driver.session() as session:
        session.run("CREATE (n:GateNode {gate_id: $id})", id=node_id)

    violations = find_nodes_missing_label(driver)

    assert node_id in violations


def test_a_node_with_a_label_is_not_flagged(driver, prefix: str):
    from commit_gate.neo4j_schema import find_nodes_missing_label

    node_id = f"{prefix}/haslabel1"
    _write_node(driver, node_id, "Claim")

    violations = find_nodes_missing_label(driver)

    assert node_id not in violations


def test_duplicate_gate_edge_id_for_the_same_rel_type_is_rejected(driver, prefix: str):
    src, dst1, dst2 = f"{prefix}/src", f"{prefix}/dst1", f"{prefix}/dst2"
    _write_node(driver, src, "Claim")
    _write_node(driver, dst1, "Certificate")
    _write_node(driver, dst2, "Certificate")
    edge_id = f"{prefix}/e1"
    _write_edge_raw(driver, src, dst1, "PROVED_BY", edge_id)

    with pytest.raises(ConstraintError):
        with driver.session() as session:
            session.run(
                "MATCH (a:GateNode {gate_id: $src}), (b:GateNode {gate_id: $dst}) "
                "CREATE (a)-[r:PROVED_BY {gate_edge_id: $eid}]->(b)",
                src=src, dst=dst2, eid=edge_id,  # same edge_id, different target
            )


def test_the_same_edge_id_is_allowed_on_a_different_relationship_type(driver, prefix: str):
    """Constraints are scoped per relationship type -- gate_edge_id only
    has to be unique *within* one type, not globally across all 23."""
    a, b = f"{prefix}/a", f"{prefix}/b"
    _write_node(driver, a, "Alignment")
    _write_node(driver, b, "Claim")
    shared_id = f"{prefix}/shared_edge_id"

    _write_edge_raw(driver, a, b, "ALIGNS_CLAIM", shared_id)
    # No error expected: different relationship type, same gate_edge_id value.
    _write_edge_raw(driver, b, a, "DEPENDS_ON", shared_id)


# ---------------------------------------------------------------------------
# find_bad_edge_endpoints: proves it actually DETECTS a broken graph, not
# just that it returns empty on data that was fine to begin with.
# ---------------------------------------------------------------------------


def test_find_bad_edge_endpoints_returns_empty_on_a_realistic_proof_graph(driver, prefix: str):
    """A thin, one-edge fixture is easy to accidentally get right by luck.
    This exercises a representative slice of a real proof graph -- ten
    different relationship types across the soundness-gate and formal-
    search schemas -- and confirms none of them are flagged."""
    nodes = {
        "claim1": "Claim",
        "claim2": "Claim",
        "cert1": "Certificate",
        "replay1": "LeanReplay",
        "decl1": "FormalDeclaration",
        "align1": "Alignment",
        "run1": "FormalRun",
        "root_state": "FormalState",
        "child_state": "FormalState",
        "tactic1": "TacticApplication",
    }
    for key, label in nodes.items():
        _write_node(driver, f"{prefix}/{key}", label)

    edges = [
        ("PROVED_BY", "claim1", "cert1"),
        ("REPLAYED_BY", "cert1", "replay1"),
        ("CERTIFIES", "cert1", "decl1"),
        ("ALIGNS_CLAIM", "align1", "claim1"),
        ("DEPENDS_ON", "claim1", "claim2"),
        ("PRODUCED_CERTIFICATE", "run1", "cert1"),
        ("SEARCHES", "run1", "decl1"),
        ("HAS_ROOT", "run1", "root_state"),
        ("HAS_TACTIC", "root_state", "tactic1"),
        ("CLOSES_STATE", "tactic1", "child_state"),
    ]
    for i, (rel_type, src_key, dst_key) in enumerate(edges):
        _write_edge_raw(driver, f"{prefix}/{src_key}", f"{prefix}/{dst_key}", rel_type, f"{prefix}/e{i}")

    violations = find_bad_edge_endpoints(driver)

    prefixed_violations = [v for v in violations if v["edge_id"].startswith(prefix)]
    assert prefixed_violations == [], (
        f"expected zero violations on a valid {len(edges)}-edge fixture, got: {prefixed_violations}"
    )


def test_find_bad_edge_endpoints_detects_a_wrong_source_label(driver, prefix: str):
    """PROVED_BY must run Claim -> Certificate. Here the source is
    mislabeled as Certificate -- simulating a projector bug or an
    out-of-band write that never went through check_references."""
    wrong_src, dst = f"{prefix}/wrong_src", f"{prefix}/dst1"
    _write_node(driver, wrong_src, "Certificate")  # should be "Claim"
    _write_node(driver, dst, "Certificate")
    edge_id = f"{prefix}/bad_e1"
    _write_edge_raw(driver, wrong_src, dst, "PROVED_BY", edge_id)

    violations = find_bad_edge_endpoints(driver)

    matches = [v for v in violations if v["edge_id"] == edge_id]
    assert len(matches) == 1
    assert matches[0]["rel_type"] == "PROVED_BY"
    assert matches[0]["actual"] == ("Certificate", "Certificate")
    assert matches[0]["expected"] == ("Claim", "Certificate")