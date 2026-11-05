"""Discover and durably pin the Lean environment used by formal work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from artifacts.hashing import hash_content
from commit_gate.canon import content_hash
from commit_gate.gate import CommitGate, CommitResult
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView
from commit_gate.store import JournalStore

__all__ = ["LeanEnvironment", "pin_aligned_declarations"]


@dataclass(frozen=True, slots=True)
class LeanEnvironment:
    """Content identity of one reproducible Lake project environment."""

    toolchain: str
    lake_manifest_hash: str
    environment_hash: str
    mathlib_commit: str | None = None

    @classmethod
    def discover(cls, project_dir: str | Path) -> "LeanEnvironment":
        """Read the files that fix a Lake project's toolchain and dependencies."""
        project = Path(project_dir).resolve()
        toolchain_path = project / "lean-toolchain"
        manifest_path = project / "lake-manifest.json"
        if not toolchain_path.is_file():
            raise ValueError(f"Lean project has no lean-toolchain: {project}")
        if not manifest_path.is_file():
            raise ValueError(
                f"Lean project has no lake-manifest.json: {project}; run `lake update` first"
            )

        toolchain_bytes = toolchain_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        toolchain = toolchain_bytes.decode("utf-8").strip()
        if not toolchain:
            raise ValueError(f"Lean project has an empty lean-toolchain: {project}")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid lake-manifest.json in {project}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"lake-manifest.json must contain an object: {project}")

        project_files: dict[str, str] = {}
        for name in ("lakefile.toml", "lakefile.lean"):
            path = project / name
            if path.is_file():
                project_files[name] = hash_content(path.read_bytes())
        if not project_files:
            raise ValueError(f"Lean project has no lakefile.toml or lakefile.lean: {project}")

        manifest_hash = hash_content(manifest_bytes)
        environment_hash = content_hash({
            "format": "lean-environment-v1",
            "toolchain": toolchain,
            "lake_manifest_hash": manifest_hash,
            "project_files": project_files,
        })
        return cls(
            toolchain=toolchain,
            lake_manifest_hash=manifest_hash,
            environment_hash=environment_hash,
            mathlib_commit=_mathlib_commit(manifest),
        )

    def fields(self) -> dict[str, str]:
        fields = {
            "toolchain": self.toolchain,
            "lake_manifest_hash": self.lake_manifest_hash,
            "environment_hash": self.environment_hash,
        }
        if self.mathlib_commit:
            fields["mathlib_commit"] = self.mathlib_commit
        return fields


def _mathlib_commit(manifest: dict[str, Any]) -> str | None:
    packages = manifest.get("packages", [])
    if not isinstance(packages, list):
        return None
    for package in packages:
        if isinstance(package, dict) and package.get("name") == "mathlib":
            revision = package.get("rev") or package.get("revision")
            if isinstance(revision, str) and revision:
                return revision
    return None


def pin_aligned_declarations(
    proof_id: str,
    environment: LeanEnvironment,
    *,
    view: ReadView,
    store: JournalStore,
) -> CommitResult | None:
    """Attach one immutable environment to every newly aligned declaration."""
    declarations = [
        node
        for node in view.list_nodes(proof_id, "FormalDeclaration")
        if node.fields.get("status") in {"aligned", "searching"}
        and not view.edges_from(node.node_id, "PINNED_ENVIRONMENT")
    ]
    if not declarations:
        return None

    matching = [
        node
        for node in view.list_nodes(proof_id, "Environment")
        if node.fields.get("environment_hash") == environment.environment_hash
    ]
    if len(matching) > 1:
        raise ValueError("proof has duplicate Environment nodes for the same hash")
    if matching:
        environment_id = matching[0].node_id
        ops: list[Any] = []
    else:
        used = {node.node_id for node in view.list_nodes(proof_id, "Environment")}
        serial = 1
        while f"{proof_id}/env-{serial}" in used:
            serial += 1
        environment_id = f"{proof_id}/env-{serial}"
        ops = [UpsertNode("Environment", environment_id, environment.fields())]

    ops.extend(
        AddEdge(
            "PINNED_ENVIRONMENT",
            declaration.node_id,
            environment_id,
            f"{declaration.node_id}-pinned-{environment_id.rsplit('/', 1)[-1]}",
        )
        for declaration in declarations
    )
    revision = store.head(proof_id)[0]
    lease_id = f"{proof_id}/environment-pin-{revision}"
    token = store.acquire_lease(
        proof_id, lease_id, actor="environment-pinner", worker_class="maintenance"
    )
    try:
        return CommitGate(view, store).commit(Proposal(
            proof_id=proof_id,
            actor="environment-pinner",
            worker_class="maintenance",
            base_revision=revision,
            lease_id=lease_id,
            fencing_token=token,
            ops=tuple(ops),
        ))
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=token)
