from dataclasses import replace

import pytest

from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.errors import ContextValidationError
from context_compiler.runtime import (
    ResearchLeaseCompiler,
    ResearchRuntimeConfig,
    compile_research_for_lease,
)
from context_compiler.snapshot import StaleSnapshotError
from context_compiler.snapshot_provider import LeaseSnapshotProvider
from mathproof.scheduler import GlobalScheduler
from shared.vocab import WorkerClass


class NoArtifacts:
    def exists(self, artifact_hash):
        return False

    def get(self, artifact_hash):
        raise KeyError(artifact_hash)


def seed_research_graph(store: JournalStore) -> None:
    lease = store.issue_lease(
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
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            selected_move_id=lease.selected_move_id,
            ops=(
                UpsertNode(
                    "Claim",
                    "p1/c-1",
                    {
                        "status": "conjectural",
                        "statement": "Every even square has an even root.",
                    },
                ),
                UpsertNode(
                    "ResearchState",
                    "p1/rs-1",
                    {
                        "status": "open",
                        "description": "Develop a parity argument.",
                        "open_goals": ["Connect square parity to root parity."],
                    },
                ),
                # Deliberately omit state_id: PROPOSES is the canonical owner.
                UpsertNode(
                    "ResearchMove",
                    "p1/rm-1",
                    {
                        "status": "queued",
                        "description": "Prove the contrapositive.",
                        "required_claim_ids": [],
                    },
                ),
                AddEdge(
                    "PROPOSES", "p1/rs-1", "p1/rm-1", "p1/e-proposes"
                ),
                AddEdge(
                    "MOVE_TARGETS", "p1/rm-1", "p1/c-1", "p1/e-targets"
                ),
            ),
        ).to_dict()
    )
    store.release_lease("p1", lease.lease_id, fencing_token=lease.fencing_token)


def runtime_case():
    view = MemoryView()
    store = JournalStore()
    seed_research_graph(store)
    lease = GlobalScheduler(view, store).lease_next(
        "p1", WorkerClass.LLM_RESEARCH.value, actor="research-1"
    )
    snapshot = LeaseSnapshotProvider(view, store).for_lease(lease)
    config = ResearchRuntimeConfig(
        instructions="Advance the selected proof strategy.",
        output_schema="Return a structured research result.",
        tool_policy="Use only the supplied context.",
        max_tokens=2_000,
    )
    return view, store, lease, snapshot, config


def test_research_runtime_compiles_a_real_scheduler_lease_from_journal_overlay():
    _, _, lease, snapshot, config = runtime_case()

    dispatch = compile_research_for_lease(
        lease, snapshot, NoArtifacts(), config
    )

    assert dispatch.task_id == f"{lease.lease_id}@{lease.fencing_token}"
    assert dispatch.proof_id == lease.proof_id
    assert dispatch.selected_move_id == lease.selected_move_id
    assert dispatch.base_revision == lease.base_revision == 1
    assert dispatch.worker_class is WorkerClass.LLM_RESEARCH
    assert dispatch.worker_input == dispatch.packet.content
    assert dispatch.packet_digest == dispatch.manifest["packet_digest"]
    assert "Every even square has an even root" in dispatch.packet.content
    assert "Prove the contrapositive" in dispatch.packet.content
    assert "Advance the selected proof strategy" in dispatch.packet.content


def test_research_runtime_is_deterministic_for_the_same_lease_and_snapshot():
    _, _, lease, snapshot, config = runtime_case()

    first = compile_research_for_lease(lease, snapshot, NoArtifacts(), config)
    second = compile_research_for_lease(lease, snapshot, NoArtifacts(), config)

    assert first.packet_digest == second.packet_digest
    assert first.worker_input == second.worker_input


def test_research_runtime_rejects_the_wrong_worker_class():
    _, _, lease, snapshot, config = runtime_case()
    wrong_lease = replace(lease, worker_class=WorkerClass.CRITIC.value)

    with pytest.raises(ContextValidationError, match="llm-research"):
        compile_research_for_lease(
            wrong_lease, snapshot, NoArtifacts(), config
        )


def test_research_runtime_rejects_an_object_without_lease_fields():
    _, _, _, snapshot, config = runtime_case()

    with pytest.raises(ContextValidationError, match="lease fields"):
        compile_research_for_lease(
            object(), snapshot, NoArtifacts(), config
        )


def test_research_runtime_rejects_an_invalid_artifact_reader():
    _, _, lease, snapshot, config = runtime_case()

    with pytest.raises(ContextValidationError, match="artifacts"):
        compile_research_for_lease(lease, snapshot, object(), config)


def test_research_runtime_rejects_a_snapshot_at_another_revision():
    _, _, lease, snapshot, config = runtime_case()
    stale_snapshot = replace(snapshot, revision=0)

    with pytest.raises(StaleSnapshotError, match="snapshot is at revision"):
        compile_research_for_lease(
            lease, stale_snapshot, NoArtifacts(), config
        )


def test_research_runtime_fails_closed_on_truncated_retrieval():
    _, _, lease, snapshot, config = runtime_case()
    tiny = replace(config, max_nodes=2)

    with pytest.raises(ContextValidationError, match="retrieval is incomplete"):
        compile_research_for_lease(
            lease, snapshot, NoArtifacts(), tiny
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instructions", ""),
        ("output_schema", " "),
        ("tool_policy", ""),
        ("max_tokens", 0),
        ("max_depth", -1),
        ("max_nodes", 0),
        ("require_complete_retrieval", "yes"),
    ],
)
def test_research_runtime_config_rejects_invalid_values(field, value):
    base = {
        "instructions": "Do the task.",
        "output_schema": "Return JSON.",
        "tool_policy": "No tools.",
    }
    base[field] = value

    with pytest.raises(ContextValidationError, match=field):
        ResearchRuntimeConfig(**base)


def test_lease_compiler_is_the_one_call_scheduler_integration_boundary():
    view, store, lease, _, config = runtime_case()
    compiler = ResearchLeaseCompiler(view, store, NoArtifacts(), config)

    dispatch = compiler.compile(lease)

    assert dispatch.lease_id == lease.lease_id
    assert dispatch.fencing_token == lease.fencing_token
    assert dispatch.base_revision == lease.base_revision
    assert dispatch.selected_move_id == lease.selected_move_id
    assert dispatch.worker_input == dispatch.packet.content
    assert dispatch.packet_digest == dispatch.manifest["packet_digest"]


def test_lease_compiler_rejects_a_stale_lease_before_compilation():
    view, store, lease, _, config = runtime_case()
    compiler = ResearchLeaseCompiler(view, store, NoArtifacts(), config)
    stale_lease = replace(lease, base_revision=lease.base_revision - 1)

    with pytest.raises(StaleSnapshotError, match="journal head"):
        compiler.compile(stale_lease)


def test_lease_compiler_rejects_the_wrong_worker_class():
    view, store, lease, _, config = runtime_case()
    compiler = ResearchLeaseCompiler(view, store, NoArtifacts(), config)
    wrong_lease = replace(lease, worker_class=WorkerClass.CRITIC.value)

    with pytest.raises(ContextValidationError, match="llm-research"):
        compiler.compile(wrong_lease)


def test_lease_compiler_fails_closed_on_incomplete_retrieval():
    view, store, lease, _, config = runtime_case()
    compiler = ResearchLeaseCompiler(
        view,
        store,
        NoArtifacts(),
        replace(config, max_nodes=2),
    )

    with pytest.raises(ContextValidationError, match="retrieval is incomplete"):
        compiler.compile(lease)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"view": object()}, "view"),
        ({"journal": object()}, "journal"),
        ({"artifacts": object()}, "artifacts"),
        ({"config": object()}, "config"),
    ],
)
def test_lease_compiler_validates_dependencies(replacement, message):
    view, store, _, _, config = runtime_case()
    values = {
        "view": view,
        "journal": store,
        "artifacts": NoArtifacts(),
        "config": config,
    }
    values.update(replacement)

    with pytest.raises(ContextValidationError, match=message):
        ResearchLeaseCompiler(**values)
