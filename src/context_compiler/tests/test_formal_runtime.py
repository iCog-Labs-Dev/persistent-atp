import json
from dataclasses import replace

import pytest

from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.contracts import FormalContextLimits, FormalExecutionLimits
from context_compiler.errors import (
    ContextValidationError,
    FormalRunPreparationRequired,
)
from context_compiler.formal import CheckpointPolicy, PremiseAccessibility
from context_compiler.formal_runtime import (
    FormalLeaseCompiler,
    FormalRuntimeConfig,
    compile_formal_for_lease,
)
from context_compiler.snapshot import StaleSnapshotError
from context_compiler.snapshot_provider import LeaseSnapshotProvider
from mathproof.scheduler import GlobalScheduler
from shared.vocab import WorkerClass


DIGEST = "sha256:" + "a" * 64


class NoArtifacts:
    def exists(self, artifact_hash):
        return False

    def get(self, artifact_hash):
        raise KeyError(artifact_hash)


def append_graph(store: JournalStore, *, run=True) -> None:
    seed_lease = store.issue_lease(
        "p1",
        "p1/ls-1",
        actor="seed",
        worker_class="coordinator",
        selected_move_id="p1/c-1",
    )
    ops = [
        UpsertNode(
            "Claim",
            "p1/c-1",
            {"status": "provisional", "statement": "For every n, P n."},
        ),
        UpsertNode(
            "FormalDeclaration",
            "p1/fd-1",
            {
                "status": "searching" if run else "aligned",
                "claim_id": "p1/c-1",
                "lean_name": "Example.main",
                "lean_type": "∀ n, P n",
            },
        ),
        UpsertNode(
            "Environment",
            "p1/env-1",
            {
                "environment_hash": DIGEST,
                "toolchain": "leanprover/lean4:v4.9.0",
            },
        ),
        AddEdge(
            "PINNED_ENVIRONMENT",
            "p1/fd-1",
            "p1/env-1",
            "p1/e-pin",
        ),
    ]
    if run:
        ops.extend(
            [
                UpsertNode("FormalRun", "p1/fr-1", {"status": "searching"}),
                UpsertNode(
                    "FormalState",
                    "p1/fs-1",
                    {
                        "status": "open",
                        "goal_text": "P n",
                        "exact_state_hash": DIGEST,
                        "semantic_signature": DIGEST,
                        "environment_hash": DIGEST,
                    },
                ),
                UpsertNode("FormalCheckpoint", "p1/fc-1", {}),
                AddEdge("SEARCHES", "p1/fr-1", "p1/fd-1", "p1/e-search"),
                AddEdge("HAS_ROOT", "p1/fr-1", "p1/fs-1", "p1/e-root"),
                AddEdge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-env"),
                AddEdge(
                    "HAS_CHECKPOINT",
                    "p1/fr-1",
                    "p1/fc-1",
                    "p1/e-checkpoint",
                ),
                AddEdge(
                    "CHECKPOINT_FRONTIER",
                    "p1/fc-1",
                    "p1/fs-1",
                    "p1/e-frontier",
                ),
            ]
        )
    store.append(
        Proposal(
            proof_id="p1",
            actor="seed",
            worker_class="coordinator",
            base_revision=0,
            lease_id=seed_lease.lease_id,
            fencing_token=seed_lease.fencing_token,
            selected_move_id=seed_lease.selected_move_id,
            ops=tuple(ops),
        ).to_dict()
    )
    store.release_lease(
        "p1", seed_lease.lease_id, fencing_token=seed_lease.fencing_token
    )


def config(**overrides):
    values = {
        "execution_limits": FormalExecutionLimits(100, 60_000),
        "accessibility": PremiseAccessibility(frozenset()),
        "checkpoint_policy": CheckpointPolicy(10),
        "search_policy": "best-first",
        "context_limits": FormalContextLimits(10, 3, 3),
        "max_tactic_steps": 50,
    }
    values.update(overrides)
    return FormalRuntimeConfig(**values)


def runtime_case(*, run=True):
    view = MemoryView()
    store = JournalStore()
    append_graph(store, run=run)
    lease = GlobalScheduler(view, store).lease_next(
        "p1", WorkerClass.FORMAL_ATP.value, actor="formal-1"
    )
    snapshot = LeaseSnapshotProvider(view, store).for_lease(lease)
    return view, store, lease, snapshot


def test_formal_runtime_compiles_real_scheduler_lease_to_atp_request():
    _, _, lease, snapshot = runtime_case()

    dispatch = compile_formal_for_lease(
        lease, snapshot, NoArtifacts(), config()
    )

    assert lease.selected_move_id == "p1/fr-1"
    assert dispatch.worker_class is WorkerClass.FORMAL_ATP
    assert dispatch.worker_input["proof_id"] == "p1"
    assert dispatch.worker_input["formal_declaration_id"] == "p1/fd-1"
    assert dispatch.worker_input["run_id"] == "p1/fr-1"
    assert dispatch.worker_input["goal_text"] == "P n"
    assert dispatch.worker_input["environment_hash"] == DIGEST
    assert dispatch.worker_input["lease_id"] == lease.lease_id
    assert dispatch.worker_input["fencing_token"] == lease.fencing_token
    assert json.loads(dispatch.packet.content)["context"]["resume"] is True
    assert dispatch.packet_digest == dispatch.manifest["packet_digest"]


def test_formal_lease_compiler_is_one_call_boundary():
    view, store, lease, _ = runtime_case()

    dispatch = FormalLeaseCompiler(
        view, store, NoArtifacts(), config()
    ).compile(lease)

    assert dispatch.selected_move_id == "p1/fr-1"
    assert dispatch.base_revision == lease.base_revision
    assert dispatch.worker_input["run_id"] == lease.selected_move_id


def test_formal_runtime_is_deterministic():
    _, _, lease, snapshot = runtime_case()

    first = compile_formal_for_lease(lease, snapshot, NoArtifacts(), config())
    second = compile_formal_for_lease(lease, snapshot, NoArtifacts(), config())

    assert first.packet_digest == second.packet_digest
    assert first.worker_input == second.worker_input


def test_formal_runtime_rejects_wrong_worker_class():
    _, _, lease, snapshot = runtime_case()
    wrong = replace(lease, worker_class=WorkerClass.LLM_RESEARCH.value)

    with pytest.raises(ContextValidationError, match="formal-atp"):
        compile_formal_for_lease(wrong, snapshot, NoArtifacts(), config())


def test_formal_runtime_rejects_stale_snapshot():
    _, _, lease, snapshot = runtime_case()

    with pytest.raises(StaleSnapshotError, match="snapshot is at revision"):
        compile_formal_for_lease(
            lease, replace(snapshot, revision=0), NoArtifacts(), config()
        )


def test_formal_runtime_reports_declaration_preparation_requirement():
    view, store, lease, _ = runtime_case(run=False)

    assert lease.selected_move_id == "p1/fd-1"
    with pytest.raises(FormalRunPreparationRequired) as raised:
        FormalLeaseCompiler(view, store, NoArtifacts(), config()).compile(lease)

    assert raised.value.declaration_id == "p1/fd-1"


def test_formal_runtime_fails_closed_on_truncated_retrieval():
    _, _, lease, snapshot = runtime_case()

    with pytest.raises(ContextValidationError, match="retrieval is incomplete"):
        compile_formal_for_lease(
            lease,
            snapshot,
            NoArtifacts(),
            config(max_nodes=2),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_limits", object()),
        ("accessibility", object()),
        ("checkpoint_policy", object()),
        ("search_policy", ""),
        ("context_limits", object()),
        ("premise_ids", ["p1/fd-2"]),
        ("kernel_id", ""),
        ("max_tactic_steps", 0),
        ("max_depth", -1),
        ("max_nodes", 0),
        ("require_complete_retrieval", "yes"),
    ],
)
def test_formal_runtime_config_rejects_invalid_values(field, value):
    values = {
        "execution_limits": FormalExecutionLimits(100, 60_000),
        "accessibility": PremiseAccessibility(frozenset()),
        "checkpoint_policy": CheckpointPolicy(10),
        "search_policy": "best-first",
    }
    values[field] = value

    with pytest.raises(ContextValidationError, match=field):
        FormalRuntimeConfig(**values)
