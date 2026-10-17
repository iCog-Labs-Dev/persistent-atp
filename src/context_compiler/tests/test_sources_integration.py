"""The reader's output must plug straight into an existing compiler."""

import unittest

from commit_gate.state import MemoryView
from context_compiler import (
    CompileRequest,
    FormalExecutionLimits,
    PacketKind,
    TextBudget,
)
from context_compiler.formal import (
    CheckpointPolicy,
    FormalTask,
    PremiseAccessibility,
    compile_formal,
)
from context_compiler.obstruction import compile_obstruction
from context_compiler.research import ResearchTask, compile_research
from context_compiler.snapshot import ProofSnapshot
from context_compiler.sources import RetrievalPlan, collect_sources
from shared.vocab import WorkerClass


class _NoArtifacts:
    def exists(self, artifact_hash):
        return False

    def get(self, artifact_hash):
        raise KeyError(artifact_hash)


class _Artifacts:
    def __init__(self, blobs):
        self.blobs = dict(blobs)

    def exists(self, artifact_hash):
        return artifact_hash in self.blobs

    def get(self, artifact_hash):
        return self.blobs[artifact_hash]


class TestReaderFeedsResearchCompiler(unittest.TestCase):
    def _collect(self):
        view = MemoryView()
        view.add_node("p1/c0", "Claim", {"statement": "Every X is Y.", "status": "provisional"})
        view.add_node("p1/c1", "Claim", {"statement": "Lemma about X.", "status": "critic-accepted"})
        view.add_edge("DEPENDS_ON", "p1/c0", "p1/c1", "p1/e0")
        view.add_node(
            "p1/rs1",
            "ResearchState",
            {"status": "open", "origin_state": "p1/fs1"},
        )
        view.add_node(
            "p1/rm1", "ResearchMove",
            {"status": "queued", "state_id": "p1/rs1", "description": "Try induction on X."},
        )
        view.add_node(
            "p1/obs1",
            "Obstruction",
            {
                "obstruction_id": "p1/obs1",
                "kind": "missing-lemma",
                "formal_state_ids": ["p1/fs1"],
            },
        )
        view.add_node("p1/fs1", "FormalState", {"status": "open"})
        view.add_edge("RESOLVES", "p1/c0", "p1/obs1", "p1/e1")
        view.add_edge("AT_STATE", "p1/obs1", "p1/fs1", "p1/e2")
        request = CompileRequest(
            "t1", "p1", 5, PacketKind.RESEARCH, WorkerClass.LLM_RESEARCH,
            text_budget=TextBudget(4000),
        )
        plan = RetrievalPlan("p1/c0", seed_ids=("p1/rs1", "p1/rm1"))
        collection = collect_sources(request, ProofSnapshot("p1", 5, view), _NoArtifacts(), plan)
        return request, collection

    def _compile(self, request, collection):
        task = ResearchTask(
            collection.kernel, "5", "p1/rs1", "p1/rm1",
            "Advance the selected move.", "free-text", "no tools",
        )
        return compile_research(request, collection.bundle, collection, task)

    def test_collection_is_complete_and_compiles(self):
        request, collection = self._collect()
        self.assertTrue(collection.is_complete)
        emitted = {
            source.source_id: source.source_type
            for source in collection.bundle.sources
        }
        self.assertEqual(collection.kernel.kernel_id, "p1/c0")
        self.assertNotIn("p1/c0", emitted)
        self.assertNotIn("p1/fs1", emitted)
        self.assertEqual(emitted["p1/obs1"], "obstruction")
        result = self._compile(request, collection)
        self.assertRegex(result.packet_digest, r"^sha256:[0-9a-f]{64}$")

    def test_compilation_is_reproducible_from_the_same_snapshot(self):
        request, first = self._collect()
        _, second = self._collect()
        self.assertEqual(
            self._compile(request, first).packet_digest,
            self._compile(request, second).packet_digest,
        )


class TestReaderFeedsFormalCompiler(unittest.TestCase):
    def test_collection_compiles_without_manually_filtering_sources(self):
        digest = "sha256:" + "1" * 64
        view = MemoryView()
        view.add_node(
            "p1/c-1",
            "Claim",
            {"statement": "For every n, P n.", "status": "provisional"},
        )
        view.add_node(
            "p1/fr-1",
            "FormalRun",
            {
                "run_id": "p1/fr-1",
                "declaration_id": "p1/fd-1",
                "disposition": "stagnated",
                "environment_hash": digest,
            },
        )
        view.add_node(
            "p1/fd-1",
            "FormalDeclaration",
            {
                "declaration_id": "p1/fd-1",
                "status": "searching",
                "lean_name": "Example.theorem",
                "lean_type": "∀ n, P n",
                "claim_id": "p1/c-1",
            },
        )
        view.add_node(
            "p1/fs-1",
            "FormalState",
            {
                "state_id": "p1/fs-1",
                "status": "open",
                "goal_text": "P n",
                "exact_state_hash": digest,
                "semantic_signature": digest,
                "environment_hash": digest,
            },
        )
        view.add_node(
            "p1/fd-2",
            "FormalDeclaration",
            {
                "declaration_id": "p1/fd-2",
                "status": "replay-accepted",
                "lean_name": "Example.helper",
                "lean_type": "∀ n, P n → P (n + 1)",
            },
        )
        view.add_node(
            "p1/env-1",
            "Environment",
            {
                "environment_id": "p1/env-1",
                "environment_hash": digest,
                "toolchain": "leanprover/lean4:v4.9.0",
            },
        )
        view.add_edge("SEARCHES", "p1/fr-1", "p1/fd-1", "p1/e-searches")
        view.add_edge("HAS_ROOT", "p1/fr-1", "p1/fs-1", "p1/e-root")
        view.add_edge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-environment")

        request = CompileRequest(
            "t1",
            "p1",
            5,
            PacketKind.FORMAL,
            WorkerClass.FORMAL_ATP,
            formal_limits=FormalExecutionLimits(100, 60_000),
        )
        collection = collect_sources(
            request,
            ProofSnapshot("p1", 5, view),
            _NoArtifacts(),
            RetrievalPlan("p1/c-1", seed_ids=("p1/fr-1", "p1/fd-2")),
        )
        task = FormalTask(
            collection.kernel,
            "5",
            "p1/fd-1",
            "p1/fs-1",
            "p1/fr-1",
            "lease-1",
            1,
            "best-first",
            CheckpointPolicy(10),
        )

        compilation = compile_formal(
            request,
            collection.bundle,
            collection,
            task,
            PremiseAccessibility(frozenset({"Example.helper"})),
        )

        self.assertRegex(compilation.packet_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            [premise.lean_name for premise in compilation.payload.verified_premises],
            ["Example.helper"],
        )


class TestReaderFeedsObstructionCompiler(unittest.TestCase):
    def test_collection_compiles_without_manually_filtering_sources(self):
        digest = "sha256:" + "1" * 64
        diagnostic = "sha256:" + "2" * 64
        view = MemoryView()
        view.add_node(
            "p1/c-1",
            "Claim",
            {"statement": "For every n, P n.", "status": "provisional"},
        )
        view.add_node(
            "p1/fr-1",
            "FormalRun",
            {
                "run_id": "p1/fr-1",
                "disposition": "stagnated",
                "environment_hash": digest,
                "frontier_state_ids": ["p1/fs-1"],
            },
        )
        view.add_node(
            "p1/obs-1",
            "Obstruction",
            {
                "obstruction_id": "p1/obs-1",
                "kind": "missing-premise",
                "formal_run_id": "p1/fr-1",
                "formal_state_ids": ["p1/fs-1"],
                "diagnostic_artifacts": [diagnostic],
                "environment_hash": digest,
            },
        )
        view.add_node(
            "p1/fs-1",
            "FormalState",
            {
                "state_id": "p1/fs-1",
                "status": "open",
                "hypotheses": ["h : P n"],
                "target": "P (n + 1)",
                "exact_state_hash": digest,
                "semantic_signature": digest,
            },
        )
        view.add_node(
            "p1/at-1",
            "Attempt",
            {
                "attempt_id": "p1/at-1",
                "state_id": "p1/fs-1",
                "tactic_family": "simp",
                "premises": ["Nat.succ_eq_add_one"],
                "executor_result": "lean-rejected",
                "diagnostic": diagnostic,
                "failure_family": "missing-identifier",
            },
        )
        view.add_node(
            "p1/env-1",
            "Environment",
            {
                "environment_id": "p1/env-1",
                "environment_hash": digest,
                "toolchain": "leanprover/lean4:v4.9.0",
            },
        )
        view.add_edge(
            "RAISED_OBSTRUCTION", "p1/fr-1", "p1/obs-1", "p1/e-obstruction"
        )
        view.add_edge("AT_STATE", "p1/obs-1", "p1/fs-1", "p1/e-state")
        view.add_edge("RESOLVES", "p1/c-1", "p1/obs-1", "p1/e-resolves")
        view.add_edge("HAS_ROOT", "p1/fr-1", "p1/fs-1", "p1/e-root")
        view.add_edge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-environment")

        request = CompileRequest(
            "t2",
            "p1",
            5,
            PacketKind.OBSTRUCTION,
            WorkerClass.COORDINATOR,
            text_budget=TextBudget(1200),
        )
        collection = collect_sources(
            request,
            ProofSnapshot("p1", 5, view),
            _Artifacts({diagnostic: b"unknown identifier Nat.succ_eq_add_one"}),
            RetrievalPlan(
                "p1/c-1",
                seed_ids=("p1/fr-1", "p1/at-1"),
            ),
        )
        emitted = {
            source.source_id: source.source_type
            for source in collection.bundle.sources
        }
        self.assertEqual(collection.kernel.kernel_id, "p1/c-1")
        self.assertNotIn("p1/c-1", emitted)
        self.assertNotIn("p1/env-1", emitted)
        self.assertEqual(
            set(emitted.values()),
            {"formal-run", "obstruction", "formal-state", "attempt", "diagnostic"},
        )

        compilation = compile_obstruction(
            request,
            collection.bundle,
            collection,
        )

        self.assertRegex(compilation.packet_digest, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
