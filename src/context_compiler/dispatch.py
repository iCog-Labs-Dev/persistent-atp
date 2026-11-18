"""The runtime handoff produced by every context compiler.

``CompiledDispatch`` keeps the scheduler identity, canonical context packet,
provenance manifest, and worker-facing representation together.  Constructing
one validates that those pieces describe the same proof task and revision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from shared.vocab import WorkerClass

from .contracts import ContextPacket
from .errors import ContextValidationError

__all__ = ["CompiledDispatch", "CompilationLike", "LeaseLike"]

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


@runtime_checkable
class LeaseLike(Protocol):
    """The scheduler fields needed to bind context to one lease generation."""

    lease_id: str
    proof_id: str
    worker_class: str
    fencing_token: int
    base_revision: int
    selected_move_id: str


@runtime_checkable
class CompilationLike(Protocol):
    """The common surface exposed by research/formal/obstruction results."""

    packet: ContextPacket
    packet_digest: str
    manifest: Mapping[str, object]


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class CompiledDispatch:
    """One lease-bound, auditable input ready to be handed to a worker."""

    task_id: str
    proof_id: str
    lease_id: str
    fencing_token: int
    selected_move_id: str
    worker_class: WorkerClass
    base_revision: int
    packet: ContextPacket
    packet_digest: str
    manifest: Mapping[str, object]
    worker_input: str | Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("task_id", "proof_id", "lease_id", "selected_move_id"):
            _require_non_empty_string(name, getattr(self, name))

        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token <= 0
        ):
            raise ContextValidationError("fencing_token must be a positive integer")
        if (
            isinstance(self.base_revision, bool)
            or not isinstance(self.base_revision, int)
            or self.base_revision < 0
        ):
            raise ContextValidationError("base_revision must be a non-negative integer")
        if not isinstance(self.worker_class, WorkerClass):
            raise ContextValidationError("worker_class must be a WorkerClass")
        if not isinstance(self.packet, ContextPacket):
            raise ContextValidationError("packet must be a ContextPacket")
        if not isinstance(self.packet_digest, str) or _SHA256_RE.fullmatch(
            self.packet_digest
        ) is None:
            raise ContextValidationError(
                "packet_digest must be sha256: followed by 64 lowercase "
                "hexadecimal characters"
            )
        if not isinstance(self.manifest, Mapping):
            raise ContextValidationError("manifest must be a mapping")

        expected_packet = {
            "task_id": self.task_id,
            "proof_id": self.proof_id,
            "base_revision": self.base_revision,
        }
        for name, expected in expected_packet.items():
            actual = getattr(self.packet, name)
            if actual != expected:
                raise ContextValidationError(
                    f"packet {name} {actual!r} does not match dispatch {expected!r}"
                )

        expected_manifest: dict[str, object] = {
            **expected_packet,
            "packet_kind": self.packet.packet_kind.value,
            "packet_digest": self.packet_digest,
        }
        for name, expected in expected_manifest.items():
            actual = self.manifest.get(name)
            if actual != expected:
                raise ContextValidationError(
                    f"manifest {name} {actual!r} does not match dispatch {expected!r}"
                )

        if isinstance(self.worker_input, str):
            if not self.worker_input.strip():
                raise ContextValidationError("worker_input must not be empty")
        elif isinstance(self.worker_input, Mapping):
            if not self.worker_input:
                raise ContextValidationError("worker_input must not be empty")
        else:
            raise ContextValidationError("worker_input must be text or a mapping")

    @classmethod
    def from_compilation(
        cls,
        *,
        lease: LeaseLike,
        compilation: CompilationLike,
        worker_input: str | Mapping[str, Any],
    ) -> "CompiledDispatch":
        """Bind an existing compiler result to the lease that requested it."""
        if not isinstance(lease, LeaseLike):
            raise ContextValidationError("lease does not provide the dispatch lease fields")
        if not isinstance(compilation, CompilationLike):
            raise ContextValidationError(
                "compilation does not provide packet, manifest, and packet_digest"
            )
        try:
            worker_class = WorkerClass(lease.worker_class)
        except (TypeError, ValueError) as exc:
            raise ContextValidationError("lease worker_class is not recognized") from exc

        return cls(
            task_id=compilation.packet.task_id,
            proof_id=lease.proof_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            selected_move_id=lease.selected_move_id,
            worker_class=worker_class,
            base_revision=lease.base_revision,
            packet=compilation.packet,
            packet_digest=compilation.packet_digest,
            manifest=compilation.manifest,
            worker_input=worker_input,
        )
