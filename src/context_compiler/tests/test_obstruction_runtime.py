from dataclasses import replace

import pytest

from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.errors import ContextValidationError
from context_compiler.obstruction_runtime import (
    ObstructionLeaseCompiler,
    ObstructionRuntimeConfig,
    compile_obstruction_for_lease,
)
from context_compiler.snapshot import StaleSnapshotError
from context_compiler.snapshot_provider import LeaseSnapshotProvider
from mathproof.scheduler import GlobalScheduler
from shared.vocab import WorkerClass


ENVIRONMENT = "sha256:" + "1" * 64
STATE = "sha256:" + "2" * 64
DIAGNOSTIC = "sha256:" + "3" * 64


class Artifacts:
    def exists(self, artifact_hash):
        return artifact_hash == DIAGNOSTIC

    def get(self, artifact_hash):
        if artifact_hash != DIAGNOSTIC:
            raise KeyError(artifact_hash)
        return b"unknown identifier Nat.succ_eq_add_one"


def append_obstruction_graph(store: JournalStore, *, disposition="stagnated"):
    seed_lease = store.issue_lease(
        "p1",
        "p1/ls-1",
        actor="seed",
        worker_class="coordinator",
        selected_move_id="p1/c-1",
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
            ops=(
                UpsertNode(
                    "Claim",
                    "p1/c-1",
                    {
                        "status": "conjectural",
                        "statement": "For every n, P n.",
                    },
                ),
                UpsertNode(
                    "FormalRun",
                    "p1/fr-1",
                    {
                        "status": disposition,
                        "environment_hash": ENVIRONMENT,
                        "frontier_state_ids": ["p1/fs-1"],
                    },
                ),
                UpsertNode(
                    "Obstruction",
                    "p1/obs-1",
                    {
                        "kind": "missing-premise",
                        "formal_run_id": "p1/fr-1",
                        "formal_state_ids": ["p1/fs-1"],
                        "diagnostic_artifacts": [DIAGNOSTIC],
                        "environment_hash": ENVIRONMENT,
                        "suggested_escalation": "premise-expansion",
                    },
                ),
                UpsertNode(
                    "FormalState",
                    "p1/fs-1",
                    {
                        "status": "open",
                        "hypotheses": ["h : P n"],
                        "target": "P (n + 1)",
                        "exact_state_hash": STATE,
                        "semantic_signature": STATE,
                    },
                ),
                UpsertNode(
                    "Attempt",
                    "p1/at-1",
                    {
                        "state_id": "p1/fs-1",
                        "tactic_family": "simp",
                        "premises": ["Nat.succ_eq_add_one"],
                        "executor_result": "lean-rejected",
                        "diagnostic": DIAGNOSTIC,
                        "failure_family": "missing-identifier",
                    },
                ),
                UpsertNode(
                    "Environment",
                    "p1/env-1",
                    {
                        "environment_hash": ENVIRONMENT,
                        "toolchain": "leanprover/lean4:v4.9.0",
                    },
                ),
                AddEdge(
                    "RAISED_OBSTRUCTION",
                    "p1/fr-1",
                    "p1/obs-1",
                    "p1/e-raised",
                ),
                AddEdge("AT_STATE", "p1/obs-1", "p1/fs-1", "p1/e-state"),
                AddEdge("ON_STATE", "p1/at-1", "p1/fs-1", "p1/e-attempt"),
                AddEdge("RESOLVES", "p1/c-1", "p1/obs-1", "p1/e-kernel"),
                AddEdge("HAS_ROOT", "p1/fr-1", "p1/fs-1", "p1/e-root"),
                AddEdge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-env"),
            ),
        ).to_dict()
    )
    store.release_lease(
        "p1", seed_lease.lease_id, fencing_token=seed_lease.fencing_token
    )


def runtime_case(*, disposition="stagnated"):
    view = MemoryView()
    store = JournalStore()
    append_obstruction_graph(store, disposition=disposition)
    lease = GlobalScheduler(view, store).lease_next(
        "p1", WorkerClass.COORDINATOR.value, actor="coordinator-1"
    )
    snapshot = LeaseSnapshotProvider(view, store).for_lease(lease)
    return view, store, lease, snapshot


def test_obstruction_runtime_compiles_scheduler_failure_lease():
    _, _, lease, snapshot = runtime_case()

    dispatch = compile_obstruction_for_lease(
        lease,
        snapshot,
        Artifacts(),
        ObstructionRuntimeConfig(),
    )

    assert lease.selected_move_id == "p1/obs-1"
    assert dispatch.worker_class is WorkerClass.COORDINATOR
    assert dispatch.worker_input["obstruction_id"] == "p1/obs-1"
    assert dispatch.worker_input["formal_run_id"] == "p1/fr-1"
    assert dispatch.worker_input["formal_state_ids"] == ["p1/fs-1"]
    assert dispatch.worker_input["environment_hash"] == ENVIRONMENT
    assert dispatch.worker_input["diagnostic_artifacts"] == [DIAGNOSTIC]
    assert dispatch.packet_digest == dispatch.manifest["packet_digest"]


def test_obstruction_lease_compiler_is_one_call_boundary():
    view, store, lease, _ = runtime_case()

    dispatch = ObstructionLeaseCompiler(
        view, store, Artifacts(), ObstructionRuntimeConfig()
    ).compile(lease)

    assert dispatch.lease_id == lease.lease_id
    assert dispatch.fencing_token == lease.fencing_token
    assert dispatch.worker_input["obstruction_id"] == lease.selected_move_id


def test_obstruction_runtime_is_deterministic():
    _, _, lease, snapshot = runtime_case()
    config = ObstructionRuntimeConfig()

    first = compile_obstruction_for_lease(lease, snapshot, Artifacts(), config)
    second = compile_obstruction_for_lease(lease, snapshot, Artifacts(), config)

    assert first.packet_digest == second.packet_digest
    assert first.worker_input == second.worker_input


def test_obstruction_runtime_rejects_wrong_consumer_worker():
    _, _, lease, snapshot = runtime_case()
    wrong = replace(lease, worker_class=WorkerClass.LLM_RESEARCH.value)

    with pytest.raises(ContextValidationError, match="consumer_worker_class"):
        compile_obstruction_for_lease(
            wrong, snapshot, Artifacts(), ObstructionRuntimeConfig()
        )


def test_obstruction_runtime_rejects_stale_snapshot():
    _, _, lease, snapshot = runtime_case()

    with pytest.raises(StaleSnapshotError, match="snapshot is at revision"):
        compile_obstruction_for_lease(
            lease,
            replace(snapshot, revision=0),
            Artifacts(),
            ObstructionRuntimeConfig(),
        )


def test_obstruction_runtime_rejects_nonconfigured_disposition():
    _, _, lease, snapshot = runtime_case(disposition="counterexample")
    config = ObstructionRuntimeConfig(
        eligible_dispositions=frozenset({"stagnated"})
    )

    with pytest.raises(ContextValidationError, match="not eligible"):
        compile_obstruction_for_lease(lease, snapshot, Artifacts(), config)


def test_obstruction_runtime_fails_closed_on_missing_diagnostic():
    _, _, lease, snapshot = runtime_case()

    with pytest.raises(ContextValidationError, match="retrieval is incomplete"):
        compile_obstruction_for_lease(
            lease,
            snapshot,
            object_with_missing_artifacts(),
            ObstructionRuntimeConfig(),
        )


def object_with_missing_artifacts():
    class MissingArtifacts:
        def exists(self, artifact_hash):
            return False

        def get(self, artifact_hash):
            raise KeyError(artifact_hash)

    return MissingArtifacts()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consumer_worker_class", "coordinator"),
        ("eligible_dispositions", set()),
        ("eligible_dispositions", frozenset({"searching"})),
        ("eligible_dispositions", frozenset({"mystery"})),
        ("max_tokens", 0),
        ("max_failed_states", 0),
        ("max_depth", -1),
        ("max_nodes", 0),
        ("kernel_id", ""),
        ("require_complete_retrieval", "yes"),
    ],
)
def test_obstruction_runtime_config_rejects_invalid_values(field, value):
    values = {field: value}

    with pytest.raises(ContextValidationError, match=field):
        ObstructionRuntimeConfig(**values)
