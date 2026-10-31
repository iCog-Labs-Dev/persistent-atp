"""Focused checks for compiled formalization and draft-only authority."""

import pytest

from alignment.formalizer import FormalizerWorker
from alignment.llm_client import MockLLMClient
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal
from context_compiler import CompileRequest, PacketKind, TextBudget
from context_compiler.errors import ContextValidationError
from context_compiler.formalization import compile_formalization
from shared.vocab import WorkerClass


def _compile(dependencies=()):
    request = CompileRequest(
        task_id="p1/task-1",
        proof_id="p1",
        base_revision=3,
        packet_kind=PacketKind.FORMALIZATION,
        worker_class=WorkerClass.FORMALIZER,
        text_budget=TextBudget(2048),
    )
    return compile_formalization(
        request,
        claim={
            "id": "p1/c-1",
            "label": "Claim",
            "fields": {"claim_text": "Every natural number equals itself", "status": "conjectural"},
        },
        dependencies=dependencies,
        theorem={"statement": "Reflexivity of equality"},
    )


def test_formalizer_emits_one_draft_for_compiled_claim():
    compilation = _compile()
    client = MockLLMClient(default_json={
        "lean_name": "nat_self_eq",
        "lean_type": "∀ n : Nat, n = n",
        "notes": "Direct translation",
    })
    result = FormalizerWorker(client, actor="formalizer-1").run(
        compilation,
        declaration_id="p1/fd-1",
        claim_id="p1/c-1",
        base_revision=3,
        lease_id="p1/ls-1",
        fencing_token=1,
    )
    view = MemoryView()
    view.add_node("p1/c-1", "Claim", {"status": "conjectural"})
    assert validate_proposal(result.proposal, view) == []
    assert result.proposal.ops[0].fields["status"] == "draft"
    assert result.proposal.ops[1].dst_id == "p1/c-1"
    assert compilation.packet_digest.startswith("sha256:")


def test_formalizer_accepts_blank_optional_metadata_from_api():
    client = MockLLMClient(default_json={
        "lean_name": "nat_self_eq",
        "lean_type": "∀ n : Nat, n = n",
        "module_path": "",
        "universe_level": "",
        "notes": "No module or universe needed",
    })
    result = FormalizerWorker(client, actor="formalizer-1").run(
        _compile(), declaration_id="p1/fd-1", claim_id="p1/c-1",
        base_revision=3, lease_id="p1/ls-1", fencing_token=1,
    )
    assert result.module_path is None
    assert result.universe_level is None


def test_compiler_rejects_unverified_dependency():
    with pytest.raises(ContextValidationError, match="not Lean verified"):
        _compile(({"id": "p1/c-2", "label": "Claim", "fields": {
            "claim_text": "Unproved premise", "status": "critic-accepted",
        }},))


def test_gate_rejects_formalizer_status_promotion():
    proposal = Proposal(
        proof_id="p1",
        actor="formalizer-1",
        worker_class=WorkerClass.FORMALIZER.value,
        base_revision=3,
        lease_id="p1/ls-1",
        fencing_token=1,
        ops=(
            UpsertNode("FormalDeclaration", "p1/fd-1", {
                "status": "aligned", "lean_type": "∀ n : Nat, n = n",
            }),
            AddEdge("DRAFTS_CLAIM", "p1/fd-1", "p1/c-1", "p1/edge-1"),
        ),
    )
    assert Reason.WORKER_CLASS_OUT_OF_AUTHORITY in {
        finding.reason for finding in validate_proposal(proposal)
    }
