import hashlib
import unittest
from dataclasses import replace

from shared.vocab import WorkerClass

from context_compiler.contracts import (
    CompileRequest,
    PacketKind,
    SourceBundle,
    SourceRef,
    TextBudget,
    TheoremKernel,
)
from context_compiler.errors import ContextValidationError
from context_compiler.research import ResearchTask, compile_research

CLAIM_A, CLAIM_B = "conjectural", "empirical"
ATTEMPT = "refuted"
MOVE = "queued"
STATE = "open"


class MapReader:
    def __init__(self, bodies):
        self.bodies = bodies

    def fetch(self, source_id):
        return self.bodies[source_id]


def ref(source_type, source_id, version="v1"):
    digest = "sha256:" + hashlib.sha256(source_id.encode()).hexdigest()
    return SourceRef(source_type, source_id, version, digest)


def make(extra=None, budget=2000):
    bodies = {
        "s1": {"status": STATE, "goal": "Bound the core dimension",
               "open_goals": ["goal one", "goal two"], "assumptions": ["n large"]},
        "m1": {"status": "leased", "description": "Apply Cayley-Bacharach",
               "state_id": "s1", "required_claim_ids": ["c1"]},
        "m2": {"status": MOVE, "description": "Try spectral shield",
               "state_id": "s1", "required_claim_ids": []},
        "m3": {"status": MOVE, "description": "Other branch move",
               "state_id": "s9", "required_claim_ids": []},
        "c1": {"status": CLAIM_A, "statement": "Core lemma holds"},
        "c2": {"status": CLAIM_A, "statement": "Reusable lemma", "reusable": True,
               "state_id": "s1"},
        "c3": {"status": CLAIM_A, "statement": "Private lemma"},
        "f1": {"status": ATTEMPT, "summary": "Union bound saturates", "state_id": "s1"},
        "f2": {"status": ATTEMPT, "summary": "Unrelated failure", "state_id": "s9"},
        "k1": {"status": ATTEMPT, "summary": "Critic: quantifier order unclear",
               "move_id": "m1"},
    }
    types = {"s1": "research-state", "m1": "research-move", "m2": "research-move",
             "m3": "research-move", "c1": "claim", "c2": "claim", "c3": "claim",
             "f1": "attempt", "f2": "attempt", "k1": "critique"}
    bodies.update(extra or {})
    refs = tuple(ref(types.get(i) or (extra or {})[i]["_type"], i) for i in bodies)
    request = CompileRequest("task-1", "proof-1", 3, PacketKind.RESEARCH,
                             next(iter(WorkerClass)), TextBudget(budget))
    task = ResearchTask(
        TheoremKernel("kern", "For all n, P(n)", ("n integer",)), "kv1",
        "s1", "m1", "Do the move.", "Return JSON.", "No network.")
    return request, SourceBundle("b1", refs), MapReader(bodies), task


def decisions(compiled):
    return {d.source_id: d for d in compiled.result.selection_decisions}


class ResearchCompilerTests(unittest.TestCase):
    def test_mandatory_blocks_present(self):
        out = compile_research(*make())
        text = out.packet.content
        for needle in ("Do the move.", "For all n, P(n)", "Bound the core dimension",
                       "Apply Cayley-Bacharach", "Core lemma holds", "goal one",
                       "goal two", "Return JSON.", "No network."):
            self.assertIn(needle, text)

    def test_deterministic(self):
        self.assertEqual(compile_research(*make()).result.packet_digest,
                         compile_research(*make()).result.packet_digest)

    def test_every_source_has_one_decision_plus_kernel(self):
        request, bundle, reader, task = make()
        out = compile_research(request, bundle, reader, task)
        ids = [d.source_id for d in out.result.selection_decisions]
        self.assertEqual(sorted(ids), sorted([s.source_id for s in bundle.sources] + ["kern"]))
        self.assertEqual(len(ids), len(set(ids)))

    def test_selection_reasons(self):
        d = decisions(compile_research(*make()))
        self.assertTrue(d["c2"].included)       # reusable lemma
        self.assertFalse(d["c3"].included)      # not reusable
        self.assertTrue(d["f1"].included)
        self.assertFalse(d["f2"].included)      # unrelated failure
        self.assertTrue(d["k1"].included)
        self.assertTrue(d["m2"].included)       # alternative on same state
        self.assertFalse(d["m3"].included)      # other state

    def test_priority_order_in_render(self):
        text = compile_research(*make()).packet.content
        order = [text.index(x) for x in ("Failure f1", "Feedback k1",
                                         "Reusable lemma c2", "Alternative strategy m2")]
        self.assertEqual(order, sorted(order))

    def test_dedupe_keeps_distinct_statuses(self):
        extra = {
            "f3": {"status": ATTEMPT, "summary": "Union bound  saturates",
                   "state_id": "s1", "_type": "attempt"},
            "c4": {"status": CLAIM_B, "statement": "Reusable lemma", "reusable": True,
                   "state_id": "s1", "_type": "claim"},
        }
        d = decisions(compile_research(*make(extra)))
        self.assertFalse(d["f3"].included)
        self.assertIn("duplicate of f1", d["f3"].reason)
        self.assertTrue(d["c4"].included)       # same text, different status

    def test_dedupe_preserves_distinct_obligations(self):
        extra = {"m4": {"status": MOVE, "description": "Try spectral shield",
                        "state_id": "s1", "required_claim_ids": ["c2"],
                        "_type": "research-move"}}
        d = decisions(compile_research(*make(extra)))
        self.assertTrue(d["m4"].included)

    def test_budget_drops_lowest_priority_first_and_fits(self):
        full = compile_research(*make(budget=5000))
        tight_budget = full.token_count - 10
        out = compile_research(*make(budget=tight_budget))
        self.assertLessEqual(out.token_count, tight_budget)
        d = decisions(out)
        self.assertTrue(d["f1"].included)
        self.assertFalse(d["m2"].included)
        self.assertEqual(d["m2"].reason, "dropped: over text budget")

    def test_manifest_matches_decisions(self):
        out = compile_research(*make())
        self.assertEqual(len(out.manifest["sources"]), len(out.result.selection_decisions))
        self.assertEqual(out.manifest["packet_kind"], "research")
        self.assertEqual(out.manifest["budgets"]["text_tokens"], 2000)

    def test_wrong_packet_kind(self):
        request, bundle, reader, task = make()
        with self.assertRaises(ContextValidationError):
            compile_research(replace(request, packet_kind=PacketKind.FORMAL),
                             bundle, reader, task)

    def test_missing_required_claim(self):
        extra = {"m1": {"status": MOVE, "description": "x", "state_id": "s1",
                        "required_claim_ids": ["nope"]}}
        with self.assertRaises(ContextValidationError):
            compile_research(*make(extra))

    def test_unknown_source_type(self):
        extra = {"x1": {"status": CLAIM_A, "statement": "s", "_type": "mystery"}}
        with self.assertRaises(ContextValidationError):
            compile_research(*make(extra))

    def test_missing_status(self):
        extra = {"c2": {"statement": "no status", "reusable": True}}
        with self.assertRaises(ContextValidationError):
            compile_research(*make(extra))

    def test_pruned_move_not_offered_as_alternative(self):
        extra = {"m5": {"status": "refuted", "description": "Dead end",
                        "state_id": "s1", "required_claim_ids": [],
                        "_type": "research-move"}}
        d = decisions(compile_research(*make(extra)))
        self.assertFalse(d["m5"].included)
        self.assertIn("not on the frontier", d["m5"].reason)

    def test_unusable_lemma_excluded(self):
        extra = {"c5": {"status": "tainted", "statement": "Tainted lemma",
                        "reusable": True, "state_id": "s1", "_type": "claim"}}
        d = decisions(compile_research(*make(extra)))
        self.assertFalse(d["c5"].included)

    def test_refuted_required_claim_rejected(self):
        extra = {"c1": {"status": "refuted", "statement": "Core lemma holds"}}
        with self.assertRaises(ContextValidationError):
            compile_research(*make(extra))

    def test_dead_active_state_rejected(self):
        extra = {"s1": {"status": "superseded", "goal": "g",
                        "open_goals": [], "assumptions": []}}
        with self.assertRaises(ContextValidationError):
            compile_research(*make(extra))

    def test_move_text_may_come_from_detail(self):
        extra = {"m1": {"status": "leased", "detail": "Bridge-lemma request",
                        "state_id": "s1", "required_claim_ids": ["c1"]}}
        out = compile_research(*make(extra))
        self.assertIn("Bridge-lemma request", out.packet.content)

    def test_real_shaped_state_without_prose(self):
        extra = {"s1": {"status": "open", "origin_state": "p/fs-1"}}
        out = compile_research(*make(extra))
        self.assertIn("Opened from formal state p/fs-1.", out.packet.content)

    def test_obstruction_without_status_is_related_via_origin_state(self):
        extra = {
            "s1": {"status": "open", "origin_state": "p/fs-1"},
            "o1": {"kind": "missing-lemma", "formal_state_ids": ["p/fs-1"],
                   "evidence": [{"kind": "heuristic-optimizer", "note": "needs bridge"}],
                   "_type": "obstruction"},
            "o2": {"kind": "unknown", "formal_state_ids": ["p/fs-9"],
                   "_type": "obstruction"},
        }
        d = decisions(compile_research(*make(extra)))
        self.assertTrue(d["o1"].included)
        self.assertEqual(d["o1"].status, "pending")
        self.assertFalse(d["o2"].included)

    def test_non_failure_attempt_excluded(self):
        extra = {"f4": {"status": "supported", "summary": "Worked fine",
                        "state_id": "s1", "_type": "attempt"}}
        d = decisions(compile_research(*make(extra)))
        self.assertFalse(d["f4"].included)
        self.assertIn("not a recorded failure", d["f4"].reason)

    def test_no_text_budget_includes_everything_eligible(self):
        request, bundle, reader, task = make()
        out = compile_research(replace(request, text_budget=None), bundle, reader, task)
        self.assertNotIn("text_tokens", out.manifest["budgets"])
        self.assertTrue(decisions(out)["m2"].included)


    def test_custom_versions_reach_manifest(self):
        request, bundle, reader, task = make()
        out = compile_research(request, bundle, reader, task,
                               compiler_version="c9", rendering_version="r9")
        self.assertEqual(out.manifest["compiler_version"], "c9")
        self.assertEqual(out.manifest["rendering_version"], "r9")
        self.assertEqual(out.packet_digest, out.manifest["packet_digest"])

    def test_mandatory_overflow(self):
        with self.assertRaises(ContextValidationError):
            compile_research(*make(budget=20))


if __name__ == "__main__":
    unittest.main()