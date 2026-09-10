"""Unit and gate-validation tests for ProposalBuilder."""

from dataclasses import dataclass
from typing import Any
import pytest

from atp_bridge.proposal_builder import ProposalBuilder, build_proposal_from_search
from atp_bridge.request import FormalSearchBudget, FormalSearchRequest
from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore


@dataclass
class MockGoal:
    expression: str
    hypotheses: list[str]


@dataclass
class MockTactic:
    tactic_name: str
    arguments: list[str]
    probability: float


@dataclass
class MockNode:
    id: int
    goal: MockGoal
    depth: int = 0
    gnn_probability: float = 1.0
    stv: Any = None
    status: str = "open"


@dataclass
class MockEdge:
    id: int
    source_id: int
    tactic: MockTactic
    child_ids: list[int]
    status: str = "pending"


@pytest.fixture
def sample_request() -> FormalSearchRequest:
    return FormalSearchRequest(
        proof_id="p101",
        claim_id="c1",
        formal_declaration_id="fd1",
        run_id="fr1",
        base_revision=0,
        lease_id="lease_test",
        fencing_token=1,
        goal_statement="forall (p q: Prop), Or p q -> Or q p",
    )


def test_proposal_builder_direct_construction(sample_request):
    builder = ProposalBuilder(sample_request)

    fs0 = builder.add_formal_state(
        node_id=0,
        goal_text="Or p q -> Or q p",
        depth=0,
        status="open",
    )
    assert fs0 == "p101/fr1/fs_0"

    fs1 = builder.add_formal_state(
        node_id=1,
        goal_text="Or q p",
        depth=1,
        status="open",
    )
    fs2 = builder.add_formal_state(
        node_id=2,
        goal_text="Or q p",
        depth=1,
        status="open",
    )

    ta0 = builder.add_tactic_application(
        edge_id=0,
        source_node_id=0,
        tactic_name="cases h",
        child_node_ids=[1, 2],
        executor_result="lean-accepted",
        status="pending",
    )
    assert ta0 == "p101/fr1/ta_0"

    proposal = builder.build()

    assert proposal.proof_id == "p101"
    assert proposal.actor == "formal-atp-worker"
    assert proposal.base_revision == 0
    assert proposal.lease_id == "lease_test"
    assert proposal.fencing_token == 1
    assert len(proposal.ops) > 0


def test_proposal_accepted_by_commit_gate():
    """Verify that a proposal built by ProposalBuilder passes CommitGate validation."""
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)

    fencing_token = store.acquire_lease("p101", "lease_test")

    req = FormalSearchRequest(
        proof_id="p101",
        claim_id="c1",
        formal_declaration_id="fd1",
        run_id="fr1",
        base_revision=0,
        lease_id="lease_test",
        fencing_token=fencing_token,
    )

    builder = ProposalBuilder(req)
    builder.add_formal_state(0, "p ∧ q → q ∧ p", depth=0, status="expanded")
    builder.add_formal_state(1, "q ∧ p", depth=1, status="open")

    builder.add_tactic_application(
        edge_id=10,
        source_node_id=0,
        tactic_name="intro h",
        child_node_ids=[1],
        executor_result="lean-accepted",
        status="pending",
    )

    proposal = builder.build()
    result = gate.commit(proposal)

    assert result.accepted is True, f"Rejections: {result.rejections}"
    assert result.revision is not None
    assert result.event_hash is not None


def test_subgoal_conservation_and_qed_closes_state():
    """Test 0-goal QED transition generating CLOSES_STATE edge and passing gate validation."""
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)

    fencing_token = store.acquire_lease("p101", "lease_test")

    req = FormalSearchRequest(
        proof_id="p101",
        claim_id="c1",
        formal_declaration_id="fd1",
        run_id="fr1",
        base_revision=0,
        lease_id="lease_test",
        fencing_token=fencing_token,
    )

    builder = ProposalBuilder(req)
    builder.add_formal_state(0, "True", depth=0, status="formally-closed")

    # QED tactic application (0 subgoals)
    builder.add_tactic_application(
        edge_id=99,
        source_node_id=0,
        tactic_name="trivial",
        child_node_ids=[],
        executor_result="lean-accepted",
        status="closed",
    )

    proposal = builder.build()
    result = gate.commit(proposal)

    assert result.accepted is True, f"Rejections: {result.rejections}"


def test_build_proposal_from_search_helper():
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)

    fencing_token = store.acquire_lease("p101", "lease_test")

    req = FormalSearchRequest(
        proof_id="p101",
        claim_id="c1",
        formal_declaration_id="fd1",
        run_id="fr1",
        base_revision=0,
        lease_id="lease_test",
        fencing_token=fencing_token,
    )

    nodes = [
        MockNode(id=0, goal=MockGoal("p ∨ q → q ∨ p", ["p: Prop"]), depth=0, status="open"),
        MockNode(id=1, goal=MockGoal("q ∨ p", ["h1: p"]), depth=1, status="open"),
        MockNode(id=2, goal=MockGoal("q ∨ p", ["h2: q"]), depth=1, status="open"),
    ]
    edges = [
        MockEdge(id=0, source_id=0, tactic=MockTactic("cases h", ["h"], 0.95), child_ids=[1, 2], status="pending")
    ]

    proposal = build_proposal_from_search(req, nodes, edges)
    result = gate.commit(proposal)

    assert result.accepted is True, f"Rejections: {result.rejections}"
