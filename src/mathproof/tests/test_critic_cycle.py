"""Critic reviews remain scoped, leased, and advisory."""

from types import SimpleNamespace
from artifacts.hashing import hash_content

import pytest

from alignment.llm_client import MockLLMClient as AlignmentMockLLMClient
from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.critic_cycle import run_critic_cycle
from mathproof.scheduler import GlobalScheduler
from mathproof.workers import CriticWorker
from shared.llm_client import MockLLMClient


def _setup(response):
    view, store = MemoryView(), JournalStore()
    view.add_node("p1/c-1", "Claim", {"status": "provisional", "claim_text": "n + 0 = n",
                                    "proof_sketch": "By the definition of addition, n + 0 = n."})
    view.add_node("p1/c-2", "Claim", {"status": "critic-accepted", "claim_text": "0 is neutral"})
    view.add_edge("DEPENDS_ON", "p1/c-1", "p1/c-2", "p1/e-1")
    view.add_node("p2/c-1", "Claim", {"status": "provisional", "claim_text": "other proof"})
    llm = MockLLMClient(default_json=response)
    return view, store, llm, GlobalScheduler(view, store), CommitGate(view, store)


def test_critic_commits_review_without_promoting_claim():
    view, store, llm, scheduler, gate = _setup({
        "verdict": "supported", "reasoning": "The cited definition closes the only step.",
        "evidence_ids": ["p1/c-1#proof_sketch"], "earliest_gap": None,
        "counterexample": None, "repair": None, "missing_context": [],
    })
    result = run_critic_cycle(
        "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
    )
    assert result is not None
    assert result.commit.accepted, result.commit.rejections
    view.apply_journal_event(store, "p1", 1)
    assert view.node("p1/c-1").fields["status"] == "provisional"
    assert view.node("p1/at-1").fields["status"] == "supported"
    assert view.node("p1/at-1").fields["evidence_ids"] == ["p1/c-1#proof_sketch"]
    assert view.edges_from("p1/at-1", "REVIEWS_CLAIM")[0].dst_id == "p1/c-1"
    assert store.verify_chain("p1") == 1
    assert store.head("p2")[0] == 0
    assert store.active_leases("p1") == []
    assert "0 is neutral" in llm.history[0][1]
    assert "other proof" not in llm.history[0][1]
    assert run_critic_cycle(
        "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
    ) is None


def test_invalid_verdict_cannot_write_or_claim_verification():
    _, store, llm, scheduler, gate = _setup({
        "verdict": "lean-verified", "reasoning": "unsupported assertion",
        "evidence_ids": ["p1/c-1#proof_sketch"],
    })
    with pytest.raises(ValueError, match="invalid critic verdict"):
        run_critic_cycle(
            "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
        )
    assert store.head("p1")[0] == 0
    assert store.active_leases("p1") == []


def test_shared_llm_keeps_critic_prompt_distinct_from_alignment():
    assert AlignmentMockLLMClient is MockLLMClient
    llm = MockLLMClient(default_json={
        "verdict": "pending", "reasoning": "the definition is missing",
        "evidence_ids": ["p1/c-1#proof_sketch"],
        "missing_context": ["definition of addition"],
    })
    CriticWorker(llm).run(
        SimpleNamespace(worker_class="critic", selected_move_id="p1/c-1"),
        {"evidence": [{"id": "p1/c-1#proof_sketch", "text": "argument"}]},
    )
    assert "mathematical critic" in llm.history[0][0]
    assert "earliest exact gap" in llm.history[0][0]
    with pytest.raises(ValueError, match="wrong worker class"):
        CriticWorker(llm).run(
            SimpleNamespace(worker_class="llm-research", selected_move_id="p1/c-1"), {}
        )


def test_no_argument_is_pending_even_if_model_would_support():
    view, store, llm, scheduler, gate = _setup({"verdict": "supported"})
    view.nodes["p1/c-1"].fields.pop("proof_sketch")
    result = run_critic_cycle(
        "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
    )
    assert result.commit.accepted
    assert result.review["verdict"] == "pending"
    assert llm.history == []
    assert store.head("p1")[0] == 1


def test_refutation_keeps_exact_gap_and_repair():
    view, store, llm, scheduler, gate = _setup({
        "verdict": "refuted", "reasoning": "The step assumes what it must show.",
        "evidence_ids": ["p1/c-1#proof_sketch"],
        "earliest_gap": "The first equality uses an unproved cancellation rule.",
        "counterexample": "For n=1 under the proposed altered operation, 1+0 differs from 1.",
        "repair": "Prove the cancellation rule under the stated operation.",
        "missing_context": [],
    })
    result = run_critic_cycle(
        "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
    )
    assert result.commit.accepted, result.commit.rejections
    view.apply_journal_event(store, "p1", 1)
    assert view.node("p1/at-1").fields["earliest_gap"].startswith("The first")
    assert view.node("p1/at-1").fields["repair"].startswith("Prove")


def test_unestablished_dependency_cannot_receive_supported_verdict():
    view, store, llm, scheduler, gate = _setup({
        "verdict": "supported", "reasoning": "looks right",
        "evidence_ids": ["p1/c-1#proof_sketch"], "missing_context": [],
    })
    view.set_field("p1/c-2", "status", "conjectural")
    with pytest.raises(ValueError, match="established dependencies"):
        run_critic_cycle(
            "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
        )
    assert store.head("p1")[0] == 0
    assert store.active_leases("p1") == []


def test_linked_evidence_bytes_are_checked_and_sent_to_model():
    view, store, llm, scheduler, gate = _setup({
        "verdict": "pending", "reasoning": "the argument omits a case",
        "evidence_ids": ["p1/c-1#proof_sketch"],
        "missing_context": ["the omitted case"],
    })
    raw = b"Detailed derivation of the first step."
    digest = hash_content(raw)
    view.add_node(digest, "Artifact", {"artifact_hash": digest})
    view.add_edge("HAS_ARTIFACT", "p1/c-1", digest, "p1/e-artifact")
    class Reader:
        def get(self, key): return raw
    result = run_critic_cycle(
        "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm),
        gate=gate, artifacts=Reader(),
    )
    assert result.commit.accepted
    assert "Detailed derivation" in llm.history[0][1]
    assert digest in llm.history[0][1]


def test_wrong_artifact_bytes_stop_review_before_model_call():
    view, store, llm, scheduler, gate = _setup({"verdict": "supported"})
    digest = hash_content(b"original")
    view.add_node(digest, "Artifact", {"artifact_hash": digest})
    view.add_edge("HAS_ARTIFACT", "p1/c-1", digest, "p1/e-artifact")
    class Reader:
        def get(self, key): return b"changed"
    with pytest.raises(ValueError, match="integrity check"):
        run_critic_cycle(
            "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm),
            gate=gate, artifacts=Reader(),
        )
    assert llm.history == []
    assert store.head("p1")[0] == 0


def test_model_cannot_cite_evidence_outside_packet():
    _, store, llm, scheduler, gate = _setup({
        "verdict": "supported", "reasoning": "looks plausible",
        "evidence_ids": ["p2/secret"], "missing_context": [],
    })
    with pytest.raises(ValueError, match="cite supplied evidence"):
        run_critic_cycle(
            "p1", actor="critic-1", scheduler=scheduler, worker=CriticWorker(llm), gate=gate
        )
    assert store.head("p1")[0] == 0
