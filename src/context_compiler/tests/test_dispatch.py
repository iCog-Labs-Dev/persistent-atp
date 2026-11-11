from dataclasses import replace

import pytest

from context_compiler.contracts import (
    ContextPacket,
    PacketKind,
    SelectionDecision,
)
from context_compiler.dispatch import CompiledDispatch
from context_compiler.errors import ContextValidationError
from context_compiler.finalize import finalize_packet
from shared.vocab import ClaimStatus, WorkerClass


class FakeLease:
    lease_id = "p1/ls-4"
    proof_id = "p1"
    worker_class = WorkerClass.LLM_RESEARCH.value
    fencing_token = 7
    base_revision = 12
    selected_move_id = "p1/rm-3"


def compilation():
    packet = ContextPacket(
        "p1/ls-4@7",
        "p1",
        12,
        PacketKind.RESEARCH,
        "Work on the selected induction move.",
    )
    return finalize_packet(
        packet,
        (
            SelectionDecision(
                "p1/c-1",
                "12",
                ClaimStatus.CONJECTURAL.value,
                True,
                "theorem kernel",
            ),
        ),
        compiler_version="context/1",
        rendering_version="text/1",
    )


def make_dispatch(**overrides):
    result = compilation()
    values = {
        "task_id": result.packet.task_id,
        "proof_id": "p1",
        "lease_id": "p1/ls-4",
        "fencing_token": 7,
        "selected_move_id": "p1/rm-3",
        "worker_class": WorkerClass.LLM_RESEARCH,
        "base_revision": 12,
        "packet": result.packet,
        "packet_digest": result.packet_digest,
        "manifest": result.manifest,
        "worker_input": result.packet.content,
    }
    values.update(overrides)
    return CompiledDispatch(**values)


def test_valid_dispatch_binds_lease_compilation_and_worker_input():
    dispatch = make_dispatch()

    assert dispatch.lease_id == "p1/ls-4"
    assert dispatch.selected_move_id == "p1/rm-3"
    assert dispatch.packet_digest == dispatch.manifest["packet_digest"]
    assert dispatch.worker_input == dispatch.packet.content


def test_from_compilation_copies_both_identities():
    result = compilation()

    dispatch = CompiledDispatch.from_compilation(
        lease=FakeLease(),
        compilation=result,
        worker_input={"prompt": result.packet.content},
    )

    assert dispatch.task_id == result.packet.task_id
    assert dispatch.proof_id == FakeLease.proof_id
    assert dispatch.base_revision == FakeLease.base_revision
    assert dispatch.worker_class is WorkerClass.LLM_RESEARCH
    assert dispatch.worker_input == {"prompt": result.packet.content}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("lease_id", " ", "lease_id"),
        ("selected_move_id", "", "selected_move_id"),
        ("fencing_token", 0, "fencing_token"),
        ("fencing_token", True, "fencing_token"),
        ("base_revision", -1, "base_revision"),
        ("worker_class", "llm-research", "worker_class"),
        ("packet_digest", "sha256:123", "packet_digest"),
        ("worker_input", " ", "worker_input"),
        ("worker_input", {}, "worker_input"),
        ("worker_input", None, "worker_input"),
    ],
)
def test_dispatch_rejects_invalid_fields(field, value, message):
    with pytest.raises(ContextValidationError, match=message):
        make_dispatch(**{field: value})


def test_dispatch_rejects_packet_identity_mismatch():
    result = compilation()
    wrong_packet = replace(result.packet, proof_id="p2")

    with pytest.raises(ContextValidationError, match="packet proof_id"):
        make_dispatch(packet=wrong_packet)


def test_dispatch_rejects_packet_revision_mismatch():
    result = compilation()
    wrong_packet = replace(result.packet, base_revision=11)

    with pytest.raises(ContextValidationError, match="packet base_revision"):
        make_dispatch(packet=wrong_packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "another-task"),
        ("proof_id", "p2"),
        ("base_revision", 11),
        ("packet_kind", "formal"),
        ("packet_digest", "sha256:" + "b" * 64),
    ],
)
def test_dispatch_rejects_manifest_identity_mismatch(field, value):
    result = compilation()
    manifest = {**result.manifest, field: value}

    with pytest.raises(ContextValidationError, match=f"manifest {field}"):
        make_dispatch(manifest=manifest)


def test_from_compilation_rejects_unknown_worker_class():
    result = compilation()
    lease = FakeLease()
    lease.worker_class = "unknown-worker"

    with pytest.raises(ContextValidationError, match="worker_class"):
        CompiledDispatch.from_compilation(
            lease=lease,
            compilation=result,
            worker_input=result.packet.content,
        )
