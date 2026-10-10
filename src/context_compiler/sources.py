"""Shared source reader for the context compilers.

Collects the records a compiler needs from ONE proof snapshot and returns
them in the shape the compilers already consume: a ``SourceBundle`` of
``SourceRef`` values plus a ``fetch(source_id)`` body lookup.

The reader only reads. IDs, statuses and field values are copied from the
committed records exactly as they are; nothing is ranked, summarised or
filtered by relevance (that is the compilers' job). Anything it could not
retrieve is reported explicitly, never dropped silently.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from commit_gate.canon import content_hash
from shared.vocab import ClaimStatus

from .contracts import (
    CompileRequest,
    PacketKind,
    SourceBundle,
    SourceRef,
    TheoremKernel,
)
from .errors import ContextValidationError
from .snapshot import ProofSnapshot

__all__ = [
    "ArtifactSource",
    "DEFAULT_RELATIONS",
    "LABEL_TO_SOURCE_TYPE",
    "MissingSource",
    "RetrievalPlan",
    "SourceCollection",
    "Truncation",
    "collect_sources",
]

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")

LABEL_TO_SOURCE_TYPE: Mapping[str, str] = {
    "Claim": "claim",
    "ResearchState": "research-state",
    "ResearchMove": "research-move",
    "FormalState": "formal-state",
    "FormalRun": "formal-run",
    "Environment": "environment",
    "FormalDeclaration": "formal-declaration",
    "Attempt": "attempt",
    "Obstruction": "obstruction",
}
"""Graph label -> the ``source_type`` string the compilers expect. Labels not
listed here are walked through but never emitted as sources."""

_IDENTITY_KEY: Mapping[str, str] = {
    "research-state": "state_id",
    "research-move": "move_id",
    "formal-state": "state_id",
    "formal-run": "run_id",
    "attempt": "attempt_id",
    "environment": "environment_id",
    "formal-declaration": "declaration_id",
}
"""A body names its own record under this key; added only when absent."""

_DIAGNOSTIC_FIELDS = ("diagnostic", "diagnostic_artifact")

DEFAULT_RELATIONS: Mapping[PacketKind, tuple[str, ...]] = {
    PacketKind.RESEARCH: (
        "HAS_TARGET", "DEPENDS_ON", "PROVED_BY", "ALIGNS_CLAIM",
        "RESOLVES", "RAISED_OBSTRUCTION", "AT_STATE",
    ),
    PacketKind.FORMAL: (
        "HAS_ROOT", "HAS_TACTIC", "FORMAL_REQUIRES", "RAN_UNDER",
        "PINNED_ENVIRONMENT", "SEARCHES", "ALIGNS_DECLARATION",
    ),
    PacketKind.OBSTRUCTION: (
        "RAISED_OBSTRUCTION", "AT_STATE", "RESOLVES", "RAN_UNDER", "HAS_ROOT",
    ),
}
"""Explicit relationships followed per packet kind (both directions)."""


class ArtifactSource(Protocol):
    """Read-only artifact access. ``artifacts.store.ArtifactStore`` fits."""

    def exists(self, artifact_hash: str) -> bool: ...

    def get(self, artifact_hash: str) -> bytes: ...


@dataclass(frozen=True)
class RetrievalPlan:
    kernel_id: str
    seed_ids: tuple[str, ...] = ()
    max_depth: int = 3
    max_nodes: int = 200
    relations: Mapping[PacketKind, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kernel_id, str) or not self.kernel_id.strip():
            raise ContextValidationError("kernel_id must be a non-empty string")
        if not isinstance(self.seed_ids, tuple) or not all(
            isinstance(s, str) and s.strip() for s in self.seed_ids
        ):
            raise ContextValidationError("seed_ids must be a tuple of non-empty strings")
        for name in ("max_depth", "max_nodes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextValidationError(f"{name} must be a non-negative integer")
        if self.max_nodes < 1:
            raise ContextValidationError("max_nodes must be at least 1")


@dataclass(frozen=True)
class MissingSource:
    source_id: str
    reason: str
    referenced_by: str | None = None


@dataclass(frozen=True)
class Truncation:
    reason: str  # "max-depth" | "max-nodes"
    unexpanded: tuple[str, ...]


@dataclass(frozen=True)
class SourceCollection:
    revision: int
    kernel: TheoremKernel | None
    bundle: SourceBundle | None
    bodies: Mapping[str, Mapping[str, Any]]
    missing: tuple[MissingSource, ...] = ()
    truncations: tuple[Truncation, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.truncations

    def fetch(self, source_id: str) -> Mapping[str, Any]:
        """The compilers' ``ArtifactReader`` contract."""
        if source_id not in self.bodies:
            raise ContextValidationError(f"missing source artifact: {source_id}")
        return self.bodies[source_id]


def _make_ref(source_type: str, source_id: str, version: str, payload: Any) -> SourceRef:
    return SourceRef(
        source_type=source_type,
        source_id=source_id,
        source_version=version,
        content_hash=content_hash(payload),
    )


def _kernel_from(
    node_id: str, label: str, fields: Mapping[str, Any]
) -> tuple[TheoremKernel | None, str | None]:
    if label != "Claim":
        return None, "kernel-not-a-claim"
    statement = next(
        (fields[k] for k in ("statement", "claim_text") if isinstance(fields.get(k), str) and fields[k].strip()),
        None,
    )
    if statement is None:
        return None, "kernel-has-no-statement"
    try:
        status = ClaimStatus(fields.get("status"))
    except ValueError:
        return None, "kernel-status-invalid"
    raw = fields.get("assumptions", ())
    if not isinstance(raw, (list, tuple)) or not all(isinstance(a, str) and a.strip() for a in raw):
        return None, "kernel-assumptions-invalid"
    return TheoremKernel(node_id, statement, tuple(raw), status), None


def collect_sources(
    request: CompileRequest,
    snapshot: ProofSnapshot,
    artifacts: ArtifactSource,
    plan: RetrievalPlan,
) -> SourceCollection:
    """Collect every source a compiler needs from ``snapshot``."""
    snapshot.require_matches(request)
    view = snapshot.view
    version = str(snapshot.revision)
    relations = (plan.relations or DEFAULT_RELATIONS)[request.packet_kind]

    refs: dict[str, SourceRef] = {}
    bodies: dict[str, Mapping[str, Any]] = {}
    missing: list[MissingSource] = []
    truncations: list[Truncation] = []
    kernel: TheoremKernel | None = None

    def note_missing(source_id: str, reason: str, by: str | None) -> None:
        entry = MissingSource(source_id, reason, by)
        if entry not in missing:
            missing.append(entry)

    # ---- bounded breadth-first walk over explicit relationships ----------
    seeds = (plan.kernel_id, *plan.seed_ids)
    queue: deque[tuple[str, int, str | None]] = deque((s, 0, None) for s in sorted(set(seeds)))
    visited: set[str] = set()
    depth_cut: set[str] = set()
    node_cut: set[str] = set()

    while queue:
        node_id, depth, parent = queue.popleft()
        if node_id in visited:
            continue
        if len(visited) >= plan.max_nodes:
            node_cut.add(node_id)
            continue
        visited.add(node_id)

        node = view.node(node_id)
        if node is None:
            note_missing(node_id, "node-not-found", parent)
            continue

        source_type = LABEL_TO_SOURCE_TYPE.get(node.label)
        if source_type is not None:
            fields = dict(node.fields)
            if source_type in _IDENTITY_KEY:
                fields.setdefault(_IDENTITY_KEY[source_type], node_id)
            try:
                refs[node_id] = _make_ref(
                    source_type, node_id, version, {"label": node.label, "fields": node.fields}
                )
            except (TypeError, ValueError):
                note_missing(node_id, "unhashable-fields", parent)
            else:
                bodies[node_id] = fields
                if node_id == plan.kernel_id:
                    kernel, why = _kernel_from(node_id, node.label, node.fields)
                    if why:
                        note_missing(node_id, why, None)

        if depth >= plan.max_depth:
            neighbours = [
                e for rel in relations
                for e in (*view.edges_from(node_id, rel), *view.edges_to(node_id, rel))
            ]
            depth_cut.update(
                (e.dst_id if e.src_id == node_id else e.src_id)
                for e in neighbours
            )
            continue

        edges = sorted(
            (e for rel in relations
             for e in (*view.edges_from(node_id, rel), *view.edges_to(node_id, rel))),
            key=lambda e: (e.rel_type, e.edge_id),
        )
        for e in edges:
            other = e.dst_id if e.src_id == node_id else e.src_id
            if other not in visited:
                queue.append((other, depth + 1, node_id))

    if plan.kernel_id not in visited or view.node(plan.kernel_id) is None:
        kernel = None

    # ---- diagnostics: content-addressed artifacts named by records -------
    for owner_id in sorted(list(bodies)):
        owner = bodies[owner_id]
        for key in _DIAGNOSTIC_FIELDS:
            digest = owner.get(key)
            if not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
                continue
            if digest in bodies:
                continue
            if not artifacts.exists(digest):
                note_missing(digest, "artifact-not-found", owner_id)
                continue
            raw = artifacts.get(digest)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                note_missing(digest, "artifact-not-utf8", owner_id)
                continue
            body: dict[str, Any] = {"artifact_hash": digest, "text": text}
            if isinstance(owner.get("failure_family"), str):
                body["failure_family"] = owner["failure_family"]
            refs[digest] = SourceRef("diagnostic", digest, digest, digest)
            bodies[digest] = body

    # ---- explicit incompleteness ----------------------------------------
    depth_only = sorted(n for n in depth_cut if n not in visited)
    if depth_only:
        truncations.append(Truncation("max-depth", tuple(depth_only)))
    if node_cut:
        truncations.append(Truncation("max-nodes", tuple(sorted(node_cut))))

    ordered = tuple(sorted(refs.values(), key=lambda r: (r.source_type, r.source_id)))
    bundle = (
        SourceBundle(f"{snapshot.proof_id}@{snapshot.revision}:{request.task_id}", ordered)
        if ordered else None
    )
    return SourceCollection(
        revision=snapshot.revision,
        kernel=kernel,
        bundle=bundle,
        bodies=bodies,
        missing=tuple(missing),
        truncations=tuple(truncations),
    )