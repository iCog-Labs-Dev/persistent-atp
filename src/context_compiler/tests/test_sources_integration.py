"""The reader's output must plug straight into an existing compiler."""

import unittest

from commit_gate.state import MemoryView
from context_compiler import CompileRequest, PacketKind, TextBudget
from context_compiler.research import ResearchTask, compile_research
from context_compiler.snapshot import ProofSnapshot
from context_compiler.sources import RetrievalPlan, collect_sources
from shared.vocab import WorkerClass


class _NoArtifacts:
    def exists(self, artifact_hash):
        return False

    def get(self, artifact_hash):
        raise KeyError(artifact_hash)


class TestReaderFeedsResearchCompiler(unittest.TestCase):
    def _collect(self):
        view = MemoryView()
        view.add_node("p1/c0", "Claim", {"statement": "Every X is Y.", "status": "provisional"})
        view.add_node("p1/c1", "Claim", {"statement": "Lemma about X.", "status": "critic-accepted"})
        view.add_edge("DEPENDS_ON", "p1/c0", "p1/c1", "p1/e0")
        view.add_node("p1/rs1", "ResearchState", {"status": "open"})
        view.add_node(
            "p1/rm1", "ResearchMove",
            {"status": "queued", "state_id": "p1/rs1", "description": "Try induction on X."},
        )
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
        result = self._compile(request, collection)
        self.assertRegex(result.packet_digest, r"^sha256:[0-9a-f]{64}$")

    def test_compilation_is_reproducible_from_the_same_snapshot(self):
        request, first = self._collect()
        _, second = self._collect()
        self.assertEqual(
            self._compile(request, first).packet_digest,
            self._compile(request, second).packet_digest,
        )


if __name__ == "__main__":
    unittest.main()