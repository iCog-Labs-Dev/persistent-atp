"""The 15 domain query operations against fixture graphs (spec section 18).

Fixtures are written straight through `MorkView` with the gate's canonical
labels and relations; diagnostics, cycle-safety, environment consistency,
and the no-merge invariant are pinned here.
"""

import unittest

from mork.backend.ffi import MorkSpace, MorkUnavailable
from mork.backend.view import MorkView

try:
    _SPACE = MorkSpace()
except MorkUnavailable as exc:
    _SPACE = None
    _WHY = str(exc)

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")

ALIGNED = {"lifecycle": "reviewed", "verdict": "aligned"}


@needs_mork
class TestQueryOps(unittest.TestCase):
    def setUp(self):
        self.view = MorkView(_SPACE)
        self.proof = f"q-{self._testMethodName}"
        self.add_node(f"{self.proof}/proof", "Proof")
        self.add_node(f"{self.proof}/c1", "Claim")
        self.add_edge("HAS_TARGET", f"{self.proof}/proof", f"{self.proof}/c1", "e-target")

        # c1 -> aligned -> d1; c1 depends on c2 -> aligned -> d2
        self.add_node(f"{self.proof}/al1", "Alignment", ALIGNED)
        self.add_node(f"{self.proof}/d1", "FormalDeclaration")
        self.add_edge("ALIGNS_CLAIM", f"{self.proof}/al1", f"{self.proof}/c1", "e-al1c")
        self.add_edge("ALIGNS_DECLARATION", f"{self.proof}/al1", f"{self.proof}/d1", "e-al1d")
        self.add_node(f"{self.proof}/c2", "Claim")
        self.add_node(f"{self.proof}/al2", "Alignment", ALIGNED)
        self.add_node(f"{self.proof}/d2", "FormalDeclaration")
        self.add_edge("DEPENDS_ON", f"{self.proof}/c1", f"{self.proof}/c2", "e-dep")
        self.add_edge("ALIGNS_CLAIM", f"{self.proof}/al2", f"{self.proof}/c2", "e-al2c")
        self.add_edge("ALIGNS_DECLARATION", f"{self.proof}/al2", f"{self.proof}/d2", "e-al2d")

        # run r1 over d1 with root fs0 and one selected tactic
        self.add_node(f"{self.proof}/r1", "FormalRun")
        self.add_node(f"{self.proof}/cp1", "FormalCheckpoint")
        self.add_node(f"{self.proof}/env1", "Environment")
        self.add_edge("SEARCHES", f"{self.proof}/r1", f"{self.proof}/d1", "e-search")
        self.add_edge("RAN_UNDER", f"{self.proof}/r1", f"{self.proof}/env1", "e-runenv")
        self.add_edge("HAS_CHECKPOINT", f"{self.proof}/r1", f"{self.proof}/cp1", "e-cp")
        self.add_node(f"{self.proof}/fs0", "FormalState")
        self.add_edge("HAS_ROOT", f"{self.proof}/r1", f"{self.proof}/fs0", "e-root")

        self.add_node(f"{self.proof}/t1", "TacticApplication",
                      {"selected": True, "executor_result": "lean-accepted"})
        self.add_edge("HAS_TACTIC", f"{self.proof}/fs0", f"{self.proof}/t1", "e-t1")
        self.add_edge("RUN_OF", f"{self.proof}/t1", f"{self.proof}/r1", "e-runof")
        self.add_node(f"{self.proof}/fs1", "FormalState",
                      {"semantic_signature": "sig-a", "exact_state_hash": "h1"})
        self.add_node(f"{self.proof}/fs2", "FormalState")
        self.add_edge("FORMAL_REQUIRES", f"{self.proof}/t1", f"{self.proof}/fs2",
                      "e-ch1", {"child_index": 1})
        self.add_edge("FORMAL_REQUIRES", f"{self.proof}/t1", f"{self.proof}/fs1",
                      "e-ch0", {"child_index": 0})

        # rejected tactic -> fs3 must NOT be a closure
        self.add_node(f"{self.proof}/t3", "TacticApplication",
                      {"selected": True, "executor_result": "lean-rejected"})
        self.add_node(f"{self.proof}/fsX", "FormalState")
        self.add_edge("HAS_TACTIC", f"{self.proof}/fsX", f"{self.proof}/t3", "e-t3")
        self.add_node(f"{self.proof}/fs3", "FormalState")
        self.add_edge("FORMAL_REQUIRES", f"{self.proof}/t3", f"{self.proof}/fs3",
                      "e-ch3", {"child_index": 0})

        # obstruction at fs1
        self.add_node(f"{self.proof}/ob1", "Obstruction")
        self.add_edge("AT_STATE", f"{self.proof}/ob1", f"{self.proof}/fs1", "e-ob")

        # certificates + replay under the same environment (consistent chain)
        self.add_node(f"{self.proof}/cert1", "Certificate",
                      {"status": "replay-pending", "root_state_id": f"{self.proof}/fs0"})
        self.add_node(f"{self.proof}/cert2", "Certificate",
                      {"status": "candidate", "root_state_id": f"{self.proof}/fs0"})
        for cert, tag in (("cert1", "e-pc1"), ("cert2", "e-pc2")):
            self.add_edge("PRODUCED_CERTIFICATE", f"{self.proof}/r1",
                          f"{self.proof}/{cert}", tag)
            self.add_edge("CERTIFIES", f"{self.proof}/{cert}",
                          f"{self.proof}/d1", f"e-certifies-{cert}")
            self.add_edge("CERTIFICATE_ENVIRONMENT", f"{self.proof}/{cert}",
                          f"{self.proof}/env1", f"e-certenv-{cert}")
        self.add_node(f"{self.proof}/rp1", "LeanReplay", {"status": "replay-accepted"})
        self.add_edge("REPLAYED_BY", f"{self.proof}/cert1", f"{self.proof}/rp1", "e-rp1")
        self.add_edge("REPLAY_ENVIRONMENT", f"{self.proof}/rp1",
                      f"{self.proof}/env1", "e-rp1env")
        self.add_edge("PINNED_ENVIRONMENT", f"{self.proof}/d1",
                      f"{self.proof}/env1", "e-pinenv")

        # semantic peers of fs1: same signature + different exact hash
        self.add_node(f"{self.proof}/fsA", "FormalState",
                      {"semantic_signature": "sig-a", "exact_state_hash": "hA"})
        # same signature AND same exact hash: not a peer, never merged
        self.add_node(f"{self.proof}/fsB", "FormalState",
                      {"semantic_signature": "sig-a", "exact_state_hash": "h1"})

    def add_node(self, node_id: str, label: str, fields: dict | None = None):
        self.view.add_node(node_id, label, fields or {})

    def add_edge(self, rel: str, src: str, dst: str, edge_id: str,
                 fields: dict | None = None):
        self.view.add_edge(rel, src, dst, f"{self.proof}/{edge_id}", fields or {})

    def test_root_lookup_chain(self):
        claim = self.view.get_claim(self.proof)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.label, "Claim")
        self.assertIsNotNone(self.view.get_alignment(claim.node_id))
        declaration = self.view.get_declaration(claim.node_id)
        self.assertIsNotNone(declaration)
        self.assertEqual(declaration.label, "FormalDeclaration")

    def test_run_and_derived_membership(self):
        run = self.view.get_run(f"{self.proof}/r1")
        self.assertEqual(run["run"].label, "FormalRun")
        self.assertEqual([c.node_id for c in run["checkpoints"]], [f"{self.proof}/cp1"])
        self.assertEqual([r.node_id for r in run["roots"]], [f"{self.proof}/fs0"])

        states = self.view.get_run_states(f"{self.proof}/r1")
        self.assertEqual(
            [s.node_id for s in states],
            [f"{self.proof}/fs0", f"{self.proof}/fs1", f"{self.proof}/fs2"],
        )

    def test_tactics_children_obstructions(self):
        tactics = self.view.get_outgoing_tactics(f"{self.proof}/fs0")
        self.assertEqual([t.dst_id for t in tactics], [f"{self.proof}/t1"])
        children = self.view.get_tactic_children(f"{self.proof}/t1")
        self.assertEqual(
            [c.dst_id for c in children],
            [f"{self.proof}/fs1", f"{self.proof}/fs2"],
        )
        obstructions = self.view.get_obstructions(f"{self.proof}/fs1")
        self.assertEqual([o.node_id for o in obstructions], [f"{self.proof}/ob1"])

    def test_dependencies_and_diagnostics(self):
        result = self.view.get_dependencies(f"{self.proof}/d1")
        self.assertEqual(result.declarations, (f"{self.proof}/d2",))
        self.assertIsNone(result.diagnostic)

        aligned_without_deps = self.view.get_dependencies(f"{self.proof}/d2")
        self.assertEqual(aligned_without_deps.declarations, ())
        self.assertIsNone(aligned_without_deps.diagnostic)

        self.add_node(f"{self.proof}/d8", "FormalDeclaration")
        lonely = self.view.get_dependencies(f"{self.proof}/d8")
        self.assertEqual(lonely.declarations, ())
        self.assertEqual(lonely.diagnostic, "missing-alignment")

        self.add_node(f"{self.proof}/al1b", "Alignment", ALIGNED)
        self.add_edge("ALIGNS_DECLARATION", f"{self.proof}/al1b",
                      f"{self.proof}/d1", "e-al1bd")
        ambiguous = self.view.get_dependencies(f"{self.proof}/d1")
        self.assertEqual(ambiguous.diagnostic, "ambiguous-alignment")

        # stale: only a superseded alignment -> never silently resolved
        self.add_node(f"{self.proof}/d9", "FormalDeclaration")
        self.add_node(f"{self.proof}/al9", "Alignment",
                      {"lifecycle": "superseded", "verdict": "aligned"})
        self.add_edge("ALIGNS_DECLARATION", f"{self.proof}/al9",
                      f"{self.proof}/d9", "e-al9d")
        stale = self.view.get_dependencies(f"{self.proof}/d9")
        self.assertEqual(stale.declarations, ())
        self.assertEqual(stale.diagnostic, "stale-alignment")

    def test_transitive_closure_is_cycle_safe(self):
        self.add_node(f"{self.proof}/c3", "Claim")
        self.add_node(f"{self.proof}/al3", "Alignment", ALIGNED)
        self.add_node(f"{self.proof}/d3", "FormalDeclaration")
        self.add_edge("DEPENDS_ON", f"{self.proof}/c2", f"{self.proof}/c3", "e-dep2")
        self.add_edge("ALIGNS_CLAIM", f"{self.proof}/al3", f"{self.proof}/c3", "e-al3c")
        self.add_edge("ALIGNS_DECLARATION", f"{self.proof}/al3", f"{self.proof}/d3", "e-al3d")
        # a cycle back to d1 must not hang or duplicate
        self.add_edge("DEPENDS_ON", f"{self.proof}/c3", f"{self.proof}/c1", "e-cycle")

        closure = self.view.get_transitive_dependencies(f"{self.proof}/d1")
        self.assertEqual(
            closure,
            (f"{self.proof}/d2", f"{self.proof}/d3"),
        )

    def test_materialized_closure_is_invalidated_by_another_wrapper(self):
        declaration = f"{self.proof}/d1"
        self.assertEqual(self.view.get_transitive_dependencies(declaration), (f"{self.proof}/d2",))
        self.assertIn(declaration, _SPACE.closure_cache)
        other = MorkView(MorkSpace())
        other.remove_edge(f"{self.proof}/e-dep")
        self.assertNotIn(declaration, _SPACE.closure_cache)
        self.assertEqual(self.view.get_transitive_dependencies(declaration), ())

    def test_unrelated_proof_write_preserves_cached_dependency_result(self):
        declaration = f"{self.proof}/d1"
        expected = self.view.get_transitive_dependencies(declaration)
        MorkView(MorkSpace()).add_node("unrelated-index-proof/s", "FormalState")
        self.assertIn(declaration, _SPACE.closure_cache)
        self.assertIs(self.view.get_transitive_dependencies(declaration), expected)

    def test_dependency_index_tracks_alignment_changes_and_parallel_edges(self):
        declaration = f"{self.proof}/d1"
        expected = (f"{self.proof}/d2",)
        self.assertEqual(self.view.get_transitive_dependencies(declaration), expected)
        self.add_edge("DEPENDS_ON", f"{self.proof}/c1", f"{self.proof}/c2", "e-parallel")
        self.view.remove_edge(f"{self.proof}/e-dep")
        self.assertEqual(self.view.get_transitive_dependencies(declaration), expected)
        self.view.set_field(f"{self.proof}/al2", "lifecycle", "superseded")
        self.assertEqual(self.view.get_transitive_dependencies(declaration), ())
        self.view.set_field(f"{self.proof}/al2", "lifecycle", "reviewed")
        self.assertEqual(self.view.get_transitive_dependencies(declaration), expected)
        self.view.remove_edge(f"{self.proof}/e-parallel")
        self.assertEqual(self.view.get_transitive_dependencies(declaration), ())

    def test_pending_replay_distinct(self):
        pending = self.view.get_pending_replay(self.proof)
        self.assertEqual([c.node_id for c in pending["candidates"]], [f"{self.proof}/cert2"])
        self.assertEqual([c.node_id for c in pending["replay_pending"]], [f"{self.proof}/cert1"])

    def test_historical_certificate_does_not_infer_witness_selection(self):
        # Shared legacy selected flags are never promoted to certificate input.
        self.assertIsNone(self.view.get_certificate_witness(f"{self.proof}/cert1"))

    def test_claim_evidence_and_environment_consistency(self):
        evidence = self.view.get_claim_evidence(f"{self.proof}/c1")
        self.assertIsNotNone(evidence.claim)
        self.assertIsNotNone(evidence.alignment)
        self.assertEqual(evidence.declaration.node_id, f"{self.proof}/d1")
        self.assertEqual([r.node_id for r in evidence.runs], [f"{self.proof}/r1"])
        self.assertEqual(
            sorted(c.node_id for c in evidence.certificates),
            [f"{self.proof}/cert1", f"{self.proof}/cert2"],
        )
        self.assertEqual([r.node_id for r in evidence.replays], [f"{self.proof}/rp1"])
        self.assertTrue(evidence.consistent)

        # environment drift must not derive alignment-evidence-consistent
        self.add_node(f"{self.proof}/env2", "Environment")
        self.add_edge("REPLAY_ENVIRONMENT", f"{self.proof}/rp1",
                      f"{self.proof}/env2", "e-drift")
        drifted = self.view.get_claim_evidence(f"{self.proof}/c1")
        self.assertFalse(drifted.consistent)

    def test_certificate_environment_drift_and_incomplete_chains_are_inconsistent(self):
        self.add_node(f"{self.proof}/env2", "Environment")
        self.add_edge("CERTIFICATE_ENVIRONMENT", f"{self.proof}/cert1",
                      f"{self.proof}/env2", "e-cert-drift")
        self.assertFalse(self.view.get_claim_evidence(f"{self.proof}/c1").consistent)
        self.assertFalse(self.view.get_claim_evidence(f"{self.proof}/missing").consistent)

    def test_semantic_peers_never_merge_exact_states(self):
        peers = self.view.get_semantic_peers(f"{self.proof}/fs1")
        self.assertEqual([p.node_id for p in peers], [f"{self.proof}/fsA"])
        # fsB shares the exact hash with fs1, so it is not a peer of fs1;
        # fsA is similar-but-not-exact to both.
        self.assertEqual(
            [p.node_id for p in self.view.get_semantic_peers(f"{self.proof}/fsB")],
            [f"{self.proof}/fsA"],
        )

    def test_projector_watermark(self):
        self.assertEqual(
            self.view.get_projector_watermark(self.proof),
            {"revision": 0, "schema_version": 0},
        )
        self.view.record_projected(self.proof, 1)
        self.assertEqual(
            self.view.get_projector_watermark(self.proof),
            {"revision": 1, "schema_version": 1},
        )


if __name__ == "__main__":
    unittest.main()


@needs_mork
class TestAmbiguousDeclaration(unittest.TestCase):
    def test_multiple_declarations_are_never_silently_selected(self):
        view = MorkView(_SPACE)
        p = 'query-ambiguous-decl'
        for label, local in [('Claim', 'c'), ('Alignment', 'al'), ('FormalDeclaration', 'd1'),
                             ('FormalDeclaration', 'd2')]:
            view.add_node(f'{p}/{local}', label, ALIGNED if label == 'Alignment' else {})
        for rel, src, dst, edge in [('ALIGNS_CLAIM', 'al', 'c', 'ec'),
                                    ('ALIGNS_DECLARATION', 'al', 'd1', 'e1'),
                                    ('ALIGNS_DECLARATION', 'al', 'd2', 'e2')]:
            view.add_edge(rel, f'{p}/{src}', f'{p}/{dst}', f'{p}/{edge}')
        assert view.get_declaration(f'{p}/c') is None
        result = view.get_dependencies(f'{p}/d1')
        assert result.declarations == ()
        assert result.diagnostic == 'ambiguous-declaration'
        assert view.get_transitive_dependencies(f'{p}/d1') == ()
