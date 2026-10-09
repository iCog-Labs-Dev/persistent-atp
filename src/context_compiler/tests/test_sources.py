import unittest

from commit_gate.state import MemoryView
from context_compiler.contracts import CompileRequest, PacketKind, TextBudget
from context_compiler.errors import ContextValidationError
from context_compiler.snapshot import ProofSnapshot, StaleSnapshotError
from context_compiler.sources import RetrievalPlan, collect_sources
from shared.vocab import WorkerClass

H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


class FakeArtifacts:
    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})

    def exists(self, h):
        return h in self.blobs

    def get(self, h):
        return self.blobs[h]


def request(kind=PacketKind.RESEARCH, revision=5):
    return CompileRequest(
        task_id="t1", proof_id="p1", base_revision=revision, packet_kind=kind,
        worker_class=next(iter(WorkerClass)), text_budget=TextBudget(max_tokens=500),
    )


def claim(view, node_id, status="provisional", **extra):
    view.add_node(node_id, "Claim", {"statement": f"stmt {node_id}", "status": status, **extra})


def chain_view(length):
    """c0 <- c1 <- ... : each claim DEPENDS_ON the next one."""
    view = MemoryView()
    for i in range(length):
        claim(view, f"p1/c{i}")
    for i in range(length - 1):
        view.add_edge("DEPENDS_ON", f"p1/c{i}", f"p1/c{i + 1}", f"p1/e{i}")
    return view


def run(view, plan, artifacts=None, req=None):
    snap = ProofSnapshot("p1", 5, view)
    return collect_sources(req or request(), snap, artifacts or FakeArtifacts(), plan)


class TestCollection(unittest.TestCase):
    def test_kernel_and_dependencies_are_collected_with_ids_versions_statuses(self):
        view = chain_view(3)
        out = run(view, RetrievalPlan(kernel_id="p1/c0"))
        ids = [s.source_id for s in out.bundle.sources]
        self.assertEqual(ids, ["p1/c0", "p1/c1", "p1/c2"])
        self.assertEqual(out.kernel.kernel_id, "p1/c0")
        self.assertEqual(out.kernel.statement, "stmt p1/c0")
        self.assertTrue(all(s.source_version == "5" for s in out.bundle.sources))
        self.assertEqual(out.fetch("p1/c1")["status"], "provisional")
        self.assertTrue(out.is_complete)

    def test_bodies_are_the_committed_fields_unchanged(self):
        view = MemoryView()
        view.add_node("p1/fs1", "FormalState", {"status": "open", "goal_text": "g"})
        claim(view, "p1/c0")
        view.add_edge("HAS_TARGET", "p1/proof", "p1/c0", "p1/e0")
        out = run(view, RetrievalPlan(kernel_id="p1/c0", seed_ids=("p1/fs1",)))
        body = out.fetch("p1/fs1")
        self.assertEqual((body["status"], body["goal_text"]), ("open", "g"))
        self.assertEqual(body["state_id"], "p1/fs1")  # identity key added
        self.assertEqual(
            {s.source_id: s.source_type for s in out.bundle.sources}["p1/fs1"], "formal-state"
        )

    def test_committed_identity_key_is_never_overwritten(self):
        view = MemoryView()
        claim(view, "p1/c0")
        view.add_node("p1/fs1", "FormalState", {"status": "open", "state_id": "custom"})
        out = run(view, RetrievalPlan(kernel_id="p1/c0", seed_ids=("p1/fs1",)))
        self.assertEqual(out.fetch("p1/fs1")["state_id"], "custom")

    def test_same_inputs_give_identical_results(self):
        view = chain_view(4)
        plan = RetrievalPlan(kernel_id="p1/c0")
        a, b = run(view, plan), run(view, plan)
        self.assertEqual(a.bundle, b.bundle)
        self.assertEqual(dict(a.bodies), dict(b.bodies))

    def test_reader_does_not_mutate_the_view(self):
        view = chain_view(3)
        before = (dict(view.nodes), dict(view.edges))
        run(view, RetrievalPlan(kernel_id="p1/c0"))
        self.assertEqual((dict(view.nodes), dict(view.edges)), before)


class TestBoundsAndDuplicates(unittest.TestCase):
    def test_depth_limit_stops_and_is_reported(self):
        out = run(chain_view(6), RetrievalPlan(kernel_id="p1/c0", max_depth=2))
        self.assertEqual([s.source_id for s in out.bundle.sources], ["p1/c0", "p1/c1", "p1/c2"])
        self.assertFalse(out.is_complete)
        self.assertEqual(out.truncations[0].reason, "max-depth")
        self.assertIn("p1/c3", out.truncations[0].unexpanded)

    def test_node_limit_stops_and_is_reported(self):
        out = run(chain_view(6), RetrievalPlan(kernel_id="p1/c0", max_nodes=2, max_depth=10))
        self.assertEqual(len(out.bundle.sources), 2)
        self.assertEqual(out.truncations[0].reason, "max-nodes")

    def test_cycle_terminates_and_each_node_appears_once(self):
        view = chain_view(3)
        view.add_edge("DEPENDS_ON", "p1/c2", "p1/c0", "p1/back")
        out = run(view, RetrievalPlan(kernel_id="p1/c0", max_depth=9))
        ids = [s.source_id for s in out.bundle.sources]
        self.assertEqual(sorted(ids), ["p1/c0", "p1/c1", "p1/c2"])
        self.assertTrue(out.is_complete)

    def test_diamond_reaches_shared_node_once(self):
        view = MemoryView()
        for n in ("a", "b", "c", "d"):
            claim(view, f"p1/{n}")
        for i, (s, d) in enumerate([("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]):
            view.add_edge("DEPENDS_ON", f"p1/{s}", f"p1/{d}", f"p1/e{i}")
        out = run(view, RetrievalPlan(kernel_id="p1/a", max_depth=5))
        ids = [s.source_id for s in out.bundle.sources]
        self.assertEqual(ids.count("p1/d"), 1)

    def test_relations_outside_the_allowlist_are_not_followed(self):
        view = MemoryView()
        claim(view, "p1/c0")
        claim(view, "p1/c1")
        view.add_edge("CITES", "p1/c0", "p1/c1", "p1/x")  # not an allowlisted relation
        out = run(view, RetrievalPlan(kernel_id="p1/c0"))
        self.assertEqual([s.source_id for s in out.bundle.sources], ["p1/c0"])


class TestMissingAndIncomplete(unittest.TestCase):
    def test_dangling_edge_is_reported_not_raised(self):
        view = chain_view(2)
        view.add_edge("DEPENDS_ON", "p1/c1", "p1/ghost", "p1/eg")
        out = run(view, RetrievalPlan(kernel_id="p1/c0", max_depth=5))
        self.assertEqual(
            [(m.source_id, m.reason, m.referenced_by) for m in out.missing],
            [("p1/ghost", "node-not-found", "p1/c1")],
        )
        self.assertFalse(out.is_complete)

    def test_missing_kernel_gives_no_bundle_and_says_why(self):
        out = run(MemoryView(), RetrievalPlan(kernel_id="p1/nope"))
        self.assertIsNone(out.bundle)
        self.assertIsNone(out.kernel)
        self.assertEqual(out.missing[0].reason, "node-not-found")

    def test_kernel_that_is_not_a_claim_is_reported(self):
        view = MemoryView()
        view.add_node("p1/fs1", "FormalState", {"status": "open"})
        out = run(view, RetrievalPlan(kernel_id="p1/fs1"))
        self.assertIsNone(out.kernel)
        self.assertEqual(out.missing[0].reason, "kernel-not-a-claim")

    def test_kernel_with_invalid_status_is_reported(self):
        view = MemoryView()
        claim(view, "p1/c0", status="not-a-status")
        out = run(view, RetrievalPlan(kernel_id="p1/c0"))
        self.assertIsNone(out.kernel)
        self.assertEqual(out.missing[0].reason, "kernel-status-invalid")
        self.assertEqual(out.fetch("p1/c0")["status"], "not-a-status")  # preserved as committed


class TestDiagnostics(unittest.TestCase):
    def _view(self):
        view = MemoryView()
        claim(view, "p1/c0")
        view.add_node(
            "p1/at1", "Attempt",
            {"status": "failed", "diagnostic": H1, "failure_family": "rewrite"},
        )
        view.add_edge("RESOLVES", "p1/at1", "p1/c0", "p1/e0")
        return view

    def test_diagnostic_is_resolved_from_the_artifact_interface(self):
        arts = FakeArtifacts({H1: b"unsolved goals"})
        out = run(self._view(), RetrievalPlan(kernel_id="p1/c0"), arts)
        body = out.fetch(H1)
        self.assertEqual(body["text"], "unsolved goals")
        self.assertEqual(body["artifact_hash"], H1)
        self.assertEqual(body["failure_family"], "rewrite")
        ref = next(s for s in out.bundle.sources if s.source_id == H1)
        self.assertEqual((ref.source_type, ref.content_hash), ("diagnostic", H1))

    def test_absent_artifact_is_reported_missing(self):
        out = run(self._view(), RetrievalPlan(kernel_id="p1/c0"))
        self.assertEqual(
            [(m.source_id, m.reason, m.referenced_by) for m in out.missing],
            [(H1, "artifact-not-found", "p1/at1")],
        )

    def test_non_utf8_artifact_is_reported_missing(self):
        out = run(self._view(), RetrievalPlan(kernel_id="p1/c0"), FakeArtifacts({H1: b"\xff\xfe"}))
        self.assertEqual(out.missing[0].reason, "artifact-not-utf8")

    def test_artifact_infrastructure_errors_propagate(self):
        class Down(FakeArtifacts):
            def exists(self, h):
                raise ConnectionError("store down")

        with self.assertRaises(ConnectionError):
            run(self._view(), RetrievalPlan(kernel_id="p1/c0"), Down())


class TestGuards(unittest.TestCase):
    def test_stale_snapshot_is_refused(self):
        with self.assertRaises(StaleSnapshotError):
            run(chain_view(1), RetrievalPlan(kernel_id="p1/c0"), req=request(revision=6))

    def test_fetch_of_unknown_source_raises(self):
        out = run(chain_view(1), RetrievalPlan(kernel_id="p1/c0"))
        with self.assertRaises(ContextValidationError):
            out.fetch("p1/unknown")

    def test_plan_validation(self):
        for kwargs in ({"kernel_id": ""}, {"kernel_id": "k", "max_nodes": 0},
                       {"kernel_id": "k", "max_depth": -1}, {"kernel_id": "k", "seed_ids": ["x"]}):
            with self.subTest(kwargs), self.assertRaises(ContextValidationError):
                RetrievalPlan(**kwargs)


if __name__ == "__main__":
    unittest.main()