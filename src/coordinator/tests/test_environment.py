"""Lean environment discovery and journal-backed declaration pinning."""

import json

from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from coordinator.environment import LeanEnvironment, pin_aligned_declarations
from coordinator.workspace import project


def write_project(path, *, revision="abc123"):
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n", encoding="utf-8")
    (path / "lakefile.toml").write_text(
        'name = "certificate_test"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (path / "lake-manifest.json").write_text(json.dumps({
        "version": "1.1.0",
        "packages": [{"name": "mathlib", "rev": revision}],
    }), encoding="utf-8")


def seed_declarations(store, proof_id):
    lease_id = f"{proof_id}/seed"
    token = store.acquire_lease(
        proof_id, lease_id, actor="seed", worker_class="maintenance"
    )
    try:
        result = CommitGate(MemoryView(), store).commit(Proposal(
            proof_id=proof_id,
            actor="seed",
            worker_class="maintenance",
            base_revision=0,
            lease_id=lease_id,
            fencing_token=token,
            ops=(
                UpsertNode("FormalDeclaration", f"{proof_id}/fd-1", {"status": "aligned"}),
                UpsertNode("FormalDeclaration", f"{proof_id}/fd-2", {"status": "draft"}),
            ),
        ))
    finally:
        store.release_lease(proof_id, lease_id, fencing_token=token)
    assert result.accepted, result.rejections


def test_discovery_hashes_toolchain_manifest_and_project_configuration(tmp_path):
    write_project(tmp_path)
    first = LeanEnvironment.discover(tmp_path)

    assert first.toolchain == "leanprover/lean4:v4.19.0"
    assert first.mathlib_commit == "abc123"
    assert first.environment_hash.startswith("sha256:")
    assert first.lake_manifest_hash.startswith("sha256:")

    write_project(tmp_path, revision="def456")
    second = LeanEnvironment.discover(tmp_path)
    assert second.environment_hash != first.environment_hash
    assert second.lake_manifest_hash != first.lake_manifest_hash


def test_pin_is_durable_scoped_and_idempotent(tmp_path):
    write_project(tmp_path)
    environment = LeanEnvironment.discover(tmp_path)
    store = JournalStore(str(tmp_path / "journal.sqlite"))
    seed_declarations(store, "p1")
    view = MemoryView()
    project(view, store, "p1")

    result = pin_aligned_declarations("p1", environment, view=view, store=store)
    assert result is not None and result.accepted, result.rejections if result else None
    project(view, store, "p1")

    env = view.node("p1/env-1")
    assert env is not None
    assert env.fields["environment_hash"] == environment.environment_hash
    assert env.fields["lake_manifest_hash"] == environment.lake_manifest_hash
    assert view.edges_from("p1/fd-1", "PINNED_ENVIRONMENT")[0].dst_id == env.node_id
    assert not view.edges_from("p1/fd-2", "PINNED_ENVIRONMENT")
    assert pin_aligned_declarations("p1", environment, view=view, store=store) is None
    assert store.verify_chain("p1") == 2


def test_discovery_requires_a_reproducible_lake_project(tmp_path):
    try:
        LeanEnvironment.discover(tmp_path)
    except ValueError as exc:
        assert "lean-toolchain" in str(exc)
    else:
        raise AssertionError("an unpinned project must be rejected")
