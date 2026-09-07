"""Integration tests for Neo4jProjection, against a REAL Neo4j server.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pytest

from commit_gate.neo4j_projection import Neo4jProjection, ReadViewUnavailable
from commit_gate.neo4j_readview import Neo4jReadView
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore

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
def projection() -> Iterator[Neo4jProjection]:
    store = JournalStore()  # in-memory SQLite, one per module run; disposable
    try:
        p = Neo4jProjection(store)
    except ReadViewUnavailable as exc:
        pytest.skip(f"opted in via RUN_NEO4J_INTEGRATION_TESTS, but Neo4j is unreachable: {exc}")
        return
    yield p
    p.close()


@pytest.fixture
def readview(projection: Neo4jProjection) -> Neo4jReadView:
    """Shares the projection's own driver -- one connection pool, not two."""
    return Neo4jReadView(driver=projection._driver, database=projection._database)


@pytest.fixture
def proof_id() -> str:
    """A unique proof id per test: separate watermark, separate node/edge
    ids, separate cleanup scope -- tests never clash with each other."""
    return f"pytest_projection/{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(projection: Neo4jProjection, proof_id: str) -> Iterator[None]:
    yield
    with projection._driver.session(database=projection._database) as session:
        session.run(
            "MATCH (n:GateNode) WHERE n.gate_id STARTS WITH $prefix DETACH DELETE n",
            prefix=proof_id,
        )
        session.run(
            "MATCH (w:ProjectionWatermark {proof_id: $proof_id}) DELETE w",
            proof_id=proof_id,
        )


def _propose(store: JournalStore, proof_id: str, fencing_token: int, *ops, base_revision: int) -> None:
    proposal = Proposal(
        proof_id=proof_id, actor="pytest", worker_class="human",
        ops=tuple(ops), base_revision=base_revision,
        lease_id="lease-1", fencing_token=fencing_token,
    )
    store.append(proposal.to_dict())


# ---------------------------------------------------------------------------
# watermark()
# ---------------------------------------------------------------------------


def test_watermark_starts_at_zero_for_a_new_proof(projection: Neo4jProjection, proof_id: str):
    assert projection.watermark(proof_id) == 0


# ---------------------------------------------------------------------------
# catch_up() -- basic application and idempotency
# ---------------------------------------------------------------------------


def test_catch_up_with_nothing_pending_applies_zero_events(
    projection: Neo4jProjection, proof_id: str
):
    assert projection.catch_up(proof_id) == 0
    assert projection.watermark(proof_id) == 0


def test_catch_up_applies_a_pending_event_and_advances_the_watermark(
    projection: Neo4jProjection, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    _propose(
        store, proof_id, token,
        UpsertNode("Claim", f"{proof_id}/claim1", {"status": "formally-closed"}),
        base_revision=0,
    )

    applied = projection.catch_up(proof_id)

    assert applied == 1
    assert projection.watermark(proof_id) == 1


def test_catch_up_called_twice_in_a_row_only_applies_once(
    projection: Neo4jProjection, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    _propose(
        store, proof_id, token,
        UpsertNode("Claim", f"{proof_id}/claim1", {}),
        base_revision=0,
    )

    first = projection.catch_up(proof_id)
    second = projection.catch_up(proof_id)

    assert first == 1
    assert second == 0
    assert projection.watermark(proof_id) == 1


# ---------------------------------------------------------------------------
# catch_up() -- what actually lands in the graph, verified via Neo4jReadView
# ---------------------------------------------------------------------------


def test_upsert_node_is_readable_after_catch_up(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    _propose(
        store, proof_id, token,
        UpsertNode("Claim", f"{proof_id}/claim1", {"status": "formally-closed"}),
        base_revision=0,
    )
    projection.catch_up(proof_id)

    record = readview.node(f"{proof_id}/claim1")

    assert record is not None
    assert record.label == "Claim"
    assert record.fields == {"status": "formally-closed"}


def test_add_edge_is_readable_after_catch_up(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    claim, cert = f"{proof_id}/claim1", f"{proof_id}/cert1"
    _propose(
        store, proof_id, token,
        UpsertNode("Claim", claim, {}),
        UpsertNode("Certificate", cert, {"actor": "producer-1"}),
        AddEdge("PROVED_BY", claim, cert, f"{proof_id}/e1"),
        base_revision=0,
    )
    projection.catch_up(proof_id)

    edges = readview.edges_from(claim, "PROVED_BY")

    assert len(edges) == 1
    assert edges[0].dst_id == cert


def test_setfield_event_updates_the_field(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    claim = f"{proof_id}/claim1"
    _propose(store, proof_id, token, UpsertNode("Claim", claim, {"status": "formally-closed"}), base_revision=0)
    projection.catch_up(proof_id)

    _propose(
        store, proof_id, token,
        SetField("Claim", claim, "status", "lean-verified", prior="formally-closed"),
        base_revision=1,
    )
    projection.catch_up(proof_id)

    record = readview.node(claim)
    assert record is not None
    assert record.fields["status"] == "lean-verified"


def test_remove_edge_event_deletes_the_edge(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    claim, cert = f"{proof_id}/claim1", f"{proof_id}/cert1"
    edge_id = f"{proof_id}/e1"
    _propose(
        store, proof_id, token,
        UpsertNode("Claim", claim, {}),
        UpsertNode("Certificate", cert, {}),
        AddEdge("PROVED_BY", claim, cert, edge_id),
        base_revision=0,
    )
    projection.catch_up(proof_id)
    assert readview.edge(edge_id) is not None

    _propose(store, proof_id, token, RemoveEdge("PROVED_BY", edge_id), base_revision=1)
    projection.catch_up(proof_id)

    assert readview.edge(edge_id) is None


def test_a_stale_reupsert_does_not_clobber_a_value_already_changed_by_setfield(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    """The core soundness property: apply.py's MemoryView drops a re-upsert
    of an already-committed node; Neo4jProjection must match that exactly,
    or a stale worker replaying old ops could silently undo real progress."""
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    claim = f"{proof_id}/claim1"

    _propose(store, proof_id, token, UpsertNode("Claim", claim, {"status": "formally-closed"}), base_revision=0)
    projection.catch_up(proof_id)

    _propose(
        store, proof_id, token,
        SetField("Claim", claim, "status", "lean-verified", prior="formally-closed"),
        base_revision=1,
    )
    projection.catch_up(proof_id)

    # A stale re-upsert carrying the OLD status arrives late.
    _propose(store, proof_id, token, UpsertNode("Claim", claim, {"status": "formally-closed"}), base_revision=2)
    projection.catch_up(proof_id)

    record = readview.node(claim)
    assert record is not None
    assert record.fields["status"] == "lean-verified"


def test_a_failing_op_rolls_back_the_whole_event_including_the_watermark(
    projection: Neo4jProjection, readview: Neo4jReadView, proof_id: str
):
    
    store = projection._store
    token = store.acquire_lease(proof_id, "lease-1")
    node_id = f"{proof_id}/should_not_survive_rollback"

    _propose(
        store, proof_id, token,
        UpsertNode("Claim", node_id, {"status": "formally-closed"}),  # would succeed alone
        SetField("Claim", node_id, "bad_field", {"nested": "not a legal Neo4j property value"}, prior=None),
        base_revision=0,
    )

    with pytest.raises(Exception):
        projection.catch_up(proof_id)

    assert readview.node(node_id) is None, (
        "the first op's write survived even though the event's second op "
        "failed -- ops and the watermark are NOT being committed atomically"
    )
    assert projection.watermark(proof_id) == 0, (
        "the watermark advanced even though the event failed -- it is not "
        "in the same transaction as the ops it claims to cover"
    )