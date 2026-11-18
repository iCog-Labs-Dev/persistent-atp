from dataclasses import dataclass
from unittest.mock import patch

import pytest

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.contracts import FormalExecutionLimits
from context_compiler.errors import ContextValidationError
from context_compiler.formal import CheckpointPolicy, PremiseAccessibility
from context_compiler.formal_runtime import FormalRuntimeConfig
from context_compiler.lease_runtime import ContextLeaseCompiler
from context_compiler.obstruction_runtime import ObstructionRuntimeConfig
from context_compiler.runtime import ResearchRuntimeConfig
from shared.vocab import WorkerClass


class NoArtifacts:
    def exists(self, artifact_hash):
        return False

    def get(self, artifact_hash):
        raise KeyError(artifact_hash)


@dataclass(frozen=True)
class FakeLease:
    lease_id: str = "p1/ls-1"
    proof_id: str = "p1"
    worker_class: str = WorkerClass.LLM_RESEARCH.value
    fencing_token: int = 1
    base_revision: int = 0
    selected_move_id: str = "p1/rm-1"


def configs():
    research = ResearchRuntimeConfig(
        instructions="Advance the proof.",
        output_schema="Return a structured result.",
        tool_policy="Use supplied context.",
    )
    formal = FormalRuntimeConfig(
        execution_limits=FormalExecutionLimits(100, 60_000),
        accessibility=PremiseAccessibility(frozenset()),
        checkpoint_policy=CheckpointPolicy(10),
        search_policy="best-first",
    )
    obstruction = ObstructionRuntimeConfig()
    return research, formal, obstruction


def router_with(label, node_id):
    view = MemoryView()
    view.add_node(node_id, label, {})
    research, formal, obstruction = configs()
    router = ContextLeaseCompiler(
        view,
        JournalStore(),
        NoArtifacts(),
        research,
        formal,
        obstruction,
    )
    return router


@pytest.mark.parametrize(
    ("label", "node_id", "worker_class", "target"),
    [
        (
            "ResearchMove",
            "p1/rm-1",
            WorkerClass.LLM_RESEARCH.value,
            "compile_research_for_lease",
        ),
        (
            "FormalRun",
            "p1/fr-1",
            WorkerClass.FORMAL_ATP.value,
            "compile_formal_for_lease",
        ),
        (
            "FormalDeclaration",
            "p1/fd-1",
            WorkerClass.FORMAL_ATP.value,
            "compile_formal_for_lease",
        ),
        (
            "Obstruction",
            "p1/obs-1",
            WorkerClass.COORDINATOR.value,
            "compile_obstruction_for_lease",
        ),
    ],
)
def test_router_selects_compiler_from_committed_node_label(
    label, node_id, worker_class, target
):
    router = router_with(label, node_id)
    lease = FakeLease(worker_class=worker_class, selected_move_id=node_id)
    sentinel = object()

    with patch(
        f"context_compiler.lease_runtime.{target}", return_value=sentinel
    ) as compiler:
        result = router.compile(lease)

    assert result is sentinel
    called_lease, snapshot, artifacts, config = compiler.call_args.args
    assert called_lease is lease
    assert snapshot.proof_id == "p1"
    assert snapshot.revision == 0
    assert artifacts is router.artifacts
    assert any(
        config is candidate
        for candidate in (router.research, router.formal, router.obstruction)
    )


def test_router_rejects_unsupported_scheduler_task():
    router = router_with("Claim", "p1/c-1")
    lease = FakeLease(
        worker_class=WorkerClass.CRITIC.value,
        selected_move_id="p1/c-1",
    )

    with pytest.raises(ContextValidationError, match="unsupported label"):
        router.compile(lease)


def test_router_rejects_missing_selected_work():
    router = router_with("Claim", "p1/c-1")

    with pytest.raises(ContextValidationError, match="is not committed"):
        router.compile(FakeLease(selected_move_id="p1/rm-99"))


class FakeScheduler:
    def __init__(self, lease):
        self.lease = lease
        self.request = None
        self.releases = []

    def lease_next(
        self, proof_id, worker_class, ttl_seconds=600.0, *, actor
    ):
        self.request = (proof_id, worker_class, ttl_seconds, actor)
        return self.lease

    def release(self, proof_id, lease_id, *, fencing_token):
        self.releases.append((proof_id, lease_id, fencing_token))
        return True


def test_lease_and_compile_returns_none_when_scheduler_is_idle():
    router = router_with("ResearchMove", "p1/rm-1")
    scheduler = FakeScheduler(None)

    result = router.lease_and_compile(
        scheduler,
        "p1",
        WorkerClass.LLM_RESEARCH.value,
        actor="research-1",
        ttl_seconds=30,
    )

    assert result is None
    assert scheduler.request == (
        "p1",
        WorkerClass.LLM_RESEARCH.value,
        30,
        "research-1",
    )
    assert scheduler.releases == []


def test_lease_and_compile_keeps_successful_lease_active():
    router = router_with("ResearchMove", "p1/rm-1")
    lease = FakeLease()
    scheduler = FakeScheduler(lease)
    sentinel = object()

    with patch.object(ContextLeaseCompiler, "compile", return_value=sentinel):
        result = router.lease_and_compile(
            scheduler,
            "p1",
            WorkerClass.LLM_RESEARCH.value,
            actor="research-1",
        )

    assert result is sentinel
    assert scheduler.releases == []


def test_lease_and_compile_releases_lease_when_compilation_fails():
    router = router_with("ResearchMove", "p1/rm-1")
    lease = FakeLease()
    scheduler = FakeScheduler(lease)

    with patch.object(
        ContextLeaseCompiler,
        "compile",
        side_effect=ContextValidationError("bad context"),
    ):
        with pytest.raises(ContextValidationError, match="bad context"):
            router.lease_and_compile(
                scheduler,
                "p1",
                WorkerClass.LLM_RESEARCH.value,
                actor="research-1",
            )

    assert scheduler.releases == [("p1", "p1/ls-1", 1)]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"view": object()}, "view"),
        ({"journal": object()}, "journal"),
        ({"artifacts": object()}, "artifacts"),
        ({"research": object()}, "research"),
        ({"formal": object()}, "formal"),
        ({"obstruction": object()}, "obstruction"),
    ],
)
def test_router_validates_dependencies(replacement, message):
    research, formal, obstruction = configs()
    values = {
        "view": MemoryView(),
        "journal": JournalStore(),
        "artifacts": NoArtifacts(),
        "research": research,
        "formal": formal,
        "obstruction": obstruction,
    }
    values.update(replacement)

    with pytest.raises(ContextValidationError, match=message):
        ContextLeaseCompiler(**values)
