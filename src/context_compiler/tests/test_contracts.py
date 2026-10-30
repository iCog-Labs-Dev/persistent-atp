from dataclasses import FrozenInstanceError

import pytest

from context_compiler.contracts import (
    CompileRequest,
    ContextPacket,
    FormalExecutionLimits,
    ObstructionData,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from context_compiler.errors import ContextValidationError
from context_compiler.finalize import finalize_packet
from shared.vocab import ClaimStatus, ObstructionKind
from shared.vocab import WorkerClass


VALID_HASH = "sha256:" + "a" * 64


def make_request(**overrides):
    values = {
        "task_id": "task-7",
        "proof_id": "p1",
        "base_revision": 12,
        "packet_kind": PacketKind.RESEARCH,
        "worker_class": WorkerClass.LLM_RESEARCH,
    }
    values.update(overrides)
    return CompileRequest(**values)


def test_valid_compile_request():
    request = make_request()

    assert request.proof_id == "p1"


def test_compile_request_rejects_invalid_values():
    with pytest.raises(ContextValidationError, match="task_id"):
        make_request(task_id=" ")

    with pytest.raises(ContextValidationError, match="base_revision"):
        make_request(base_revision=-1)

    with pytest.raises(ContextValidationError, match="packet_kind"):
        make_request(packet_kind="research")

    with pytest.raises(ContextValidationError, match="worker_class"):
        make_request(worker_class="llm-research")


def test_valid_context_packet():
    packet = ContextPacket("task-7", "p1", 12, PacketKind.RESEARCH, "Claim: 2 + 2 = 4")

    assert packet.content == "Claim: 2 + 2 = 4"


def test_context_packet_rejects_empty_content():
    with pytest.raises(ContextValidationError, match="content"):
        ContextPacket("task-7", "p1", 12, PacketKind.RESEARCH, " ")


def test_context_packet_cannot_be_changed():
    packet = ContextPacket("task-7", "p1", 12, PacketKind.RESEARCH, "context")

    with pytest.raises(FrozenInstanceError):
        packet.content = "changed"


def test_shared_structures_validate_and_preserve_budgets():
    request = make_request(
        text_budget=TextBudget(800),
        formal_limits=FormalExecutionLimits(128, 5000),
    )
    kernel = TheoremKernel(
        "p1/k-1", "a + b = b + a", status=ClaimStatus.CONJECTURAL
    )
    source = SourceRef("claim", "p1/c-1", "revision-4", VALID_HASH)
    bundle = SourceBundle("p1/bundle-1", (source,))
    obstruction = ObstructionData(
        "p1/obs-1", ObstructionKind.MISSING_LEMMA, "A bridge lemma is needed"
    )

    assert request.text_budget.max_tokens == 800
    assert kernel.kernel_id == "p1/k-1"
    assert bundle.sources == (source,)
    assert obstruction.kind is ObstructionKind.MISSING_LEMMA


def test_finalize_packet_builds_deterministic_manifest():
    packet = ContextPacket(
        "task-7",
        "p1",
        12,
        PacketKind.FORMAL,
        "goal: a + b = b + a",
        formal_limits=FormalExecutionLimits(128, 5000),
    )
    decisions = (
        SelectionDecision(
            "p1/c-1", "revision-4", ClaimStatus.CRITIC_ACCEPTED, True, "relevant"
        ),
        SelectionDecision(
            "p1/c-2", "revision-4", ClaimStatus.STALE, False, "stale"
        ),
    )

    result = finalize_packet(
        packet,
        decisions,
        compiler_version="context/1",
        rendering_version="text/1",
    )
    repeated = finalize_packet(
        packet,
        decisions,
        compiler_version="context/1",
        rendering_version="text/1",
    )

    assert result.packet_digest == repeated.packet_digest
    assert result.manifest["packet_digest"] == result.packet_digest
    assert result.manifest["sources"][0]["source_id"] == "p1/c-1"
    assert result.manifest["budgets"] == {
        "formal_execution": {"max_steps": 128, "wall_clock_ms": 5000}
    }


def test_valid_reference():
    ref = SourceRef("claim", "p1/c-7", "revision-12", VALID_HASH)

    assert ref.source_id == "p1/c-7"


def test_empty_id_is_rejected():
    with pytest.raises(ContextValidationError, match="source_id"):
        SourceRef("claim", "", "revision-12", VALID_HASH)


def test_invalid_hash_is_rejected():
    with pytest.raises(ContextValidationError, match="content_hash"):
        SourceRef("claim", "p1/c-7", "revision-12", "invalid-hash")


def test_reference_cannot_be_changed():
    ref = SourceRef("claim", "p1/c-7", "revision-12", VALID_HASH)

    with pytest.raises(FrozenInstanceError):
        ref.source_id = "p1/c-8"
