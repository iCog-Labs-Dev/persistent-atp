import hashlib
import json
import unittest
from dataclasses import replace

try:
    from mathproof.schemas import validate as validate_schema
except ImportError:  # pragma: no cover
    validate_schema = None

from shared.vocab import WorkerClass

from context_compiler.contracts import (
    CompileRequest,
    FormalExecutionLimits,
    PacketKind,
    SourceBundle,
    SourceRef,
    TheoremKernel,
)
from context_compiler.errors import ContextValidationError
from context_compiler.obstruction import MappingArtifactReader
from context_compiler.formal import (
    CheckpointPolicy,
    CorpusManifest,
    FormalTask,
    ModelBundle,
    PremiseAccessibility,
    compile_formal,
)


P = "p1"
DECL, ROOT, RUN = f"{P}/fd-1", f"{P}/fs-1", f"{P}/fr-1"


def h(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class MapReader:
    def __init__(self, bodies):
        self.bodies = bodies

    def fetch(self, source_id):
        return self.bodies[source_id]


def base_bodies():
    return {
        DECL: {"declaration_id": DECL, "status": "searching", "lean_type": "∀ n, P n",
               "lean_value": "by sorry", "claim_id": f"{P}/c-1"},
        ROOT: {"state_id": ROOT, "status": "open", "kind": "goal", "goal_text": "P n",
             "context_digest": h("ctx"), "environment_hash": h("lake"),
            "serialization_version": 1, "exact_state_hash": h("root"),
            "semantic_signature": h("sem-root")},
        "env-1": {"toolchain": "leanprover/lean4:v4.9.0", "lake_manifest_hash": "ab12",
                  "mathlib_commit": "deadbeef", "environment_hash": h("lake")},
        "prem-v": {"lean_name": "Nat.add_comm", "statement": "a + b = b + a",
                   "status": "lean-verified"},
        "prem-s": {"lean_name": "My.hint", "statement": "P 0", "status": "conjectural",
                   "evidence_kind": "model-score"},
        "prem-c": {"lean_name": "Critic.ok", "statement": "Q", "status": "critic-accepted"},
        "prem-x": {"lean_name": "Dead.lemma", "statement": "R", "status": "refuted"},
        "prem-na": {"lean_name": "Hidden.lemma", "statement": "S", "status": "lean-verified"},
        "prem-un": {"lean_name": "Unreported.lemma", "statement": "T", "status": "lean-verified"},
        f"{P}/fr-2": {"run_id": f"{P}/fr-2", "declaration_id": DECL,
                      "disposition": "stagnated", "environment_hash": h("lake"),
                      "frontier_state_ids": [ROOT], "summary": "stuck at root"},
        f"{P}/fr-3": {"run_id": f"{P}/fr-3", "declaration_id": f"{P}/fd-9",
                      "disposition": "stagnated"},
        f"{P}/fr-4": {"run_id": f"{P}/fr-4", "declaration_id": DECL,
                      "disposition": "stagnated", "environment_hash": h("other")},
        "tr-1": {"from_state_id": f"{P}/fs-2", "to_state_id": ROOT, "accepted": True},
        "tr-2": {"from_state_id": f"{P}/fs-3", "to_state_id": ROOT, "accepted": False},
        f"{P}/fs-5": {"state_id": f"{P}/fs-5", "status": "failed",
            "exact_state_hash": h("other-state"),
            "semantic_signature": h("sem-other")},
    }


TYPES = {DECL: "formal-declaration", ROOT: "formal-state", "env-1": "environment",
         "prem-v": "premise", "prem-s": "premise", "prem-c": "premise",
         "prem-x": "premise", "prem-na": "premise", "prem-un": "premise",
         f"{P}/fr-2": "formal-run", f"{P}/fr-3": "formal-run", f"{P}/fr-4": "formal-run",
         "tr-1": "transposition", "tr-2": "transposition", f"{P}/fs-5": "formal-state"}

ACCESS = PremiseAccessibility(
    accessible=frozenset({"Nat.add_comm", "My.hint", "Critic.ok"}),
    unavailable={"Hidden.lemma": "not imported by the pinned environment"},
)


def make(extra=None, remove=(), run_id=RUN):
    bodies = base_bodies()
    types = dict(TYPES)
    for key, value in (extra or {}).items():
        types[key] = value.pop("_type", types.get(key))
        bodies[key] = value
    for key in remove:
        bodies.pop(key)
    refs = tuple(SourceRef(types[i], i, "v1", h(i)) for i in bodies)
    request = CompileRequest("task-1", P, 7, PacketKind.FORMAL, WorkerClass.FORMAL_ATP,
                             formal_limits=FormalExecutionLimits(500, 60000))
    task = FormalTask(
        TheoremKernel("kern", "For all n, P(n)"), "kv1", DECL, ROOT, run_id,
        "lease-1", 4, "gnn-pln-best-first-v1", CheckpointPolicy(100),
        max_tactic_steps=40,
        models=ModelBundle(tactic_model_artifact=h("tactic")),
        corpus=CorpusManifest(premise_corpus_artifact=h("corpus"),
                              retrieval_policy="top-k"))
    return request, SourceBundle("b1", refs), MapReader(bodies), task, ACCESS


def decisions(out):
    return {d.source_id: d for d in out.result.selection_decisions}


class FormalCompilerTests(unittest.TestCase):
    def test_request_contents(self):
        out = compile_formal(*make())
        p = out.payload
        self.assertEqual(p.lean_type, "∀ n, P n")
        self.assertEqual(p.lean_value, "by sorry")
        self.assertEqual(p.declaration_hash, h(DECL))
        self.assertEqual(p.root_state["exact_state_hash"], h("root"))
        self.assertEqual(p.environment["environment_hash"], h("lake"))
        self.assertEqual(p.environment["mathlib_commit"], "deadbeef")
        self.assertEqual(p.environment["toolchain"], "leanprover/lean4:v4.9.0")
        self.assertEqual(p.model_bundle, {"tactic_model_artifact": h("tactic")})
        self.assertEqual(p.corpus_manifest,
                         {"premise_corpus_artifact": h("corpus"), "retrieval_policy": "top-k"})
        self.assertEqual(p.search_policy, "gnn-pln-best-first-v1")
        self.assertEqual(dict(p.limits), {"max_steps": 500, "wall_clock_ms": 60000})
        self.assertEqual(p.checkpoint_policy.every_steps, 100)
        self.assertEqual((p.lease_id, p.fencing_token, p.base_revision), ("lease-1", 4, 7))
        self.assertEqual(p.lean_source_artifact, h(DECL))
        self.assertEqual(p.claim_id, f"{P}/c-1")
        self.assertFalse(p.resume)

    def test_packet_is_envelope_of_request_and_context(self):
        out = compile_formal(*make())
        envelope = json.loads(out.packet.content)
        self.assertEqual(envelope["request"], out.payload.to_schema_dict())
        self.assertEqual(envelope["context"], out.payload.to_context_dict())
        names = [e["lean_name"] for e in envelope["context"]["verified_premises"]]
        self.assertEqual(names, ["Nat.add_comm"])

    def test_schema_request_has_expected_fields(self):
        req = compile_formal(*make()).payload.to_schema_dict()
        self.assertEqual(req["formal_declaration_id"], DECL)
        self.assertEqual(req["lean_source_artifact"], h(DECL))  # declaration hash by default
        self.assertEqual(req["environment_hash"], h("lake"))
        self.assertEqual(req["search_policy"],
                         {"policy_name": "gnn-pln-best-first-v1", "deny_sorry": True,
                          "max_tactic_steps": 40})
        self.assertEqual(req["budget"], {"max_steps": 500, "wall_clock_ms": 60000})

    def test_schema_request_validates(self):
        if validate_schema is None:
            self.skipTest("mathproof.schemas not importable")
        request, bundle, reader, task, access = make()
        full = compile_formal(request, bundle, reader, task, access)
        self.assertEqual(validate_schema("formal-search-request", full.payload.to_schema_dict()), [])
        minimal_task = replace(task, models=None, corpus=None, max_tactic_steps=None)
        minimal = compile_formal(request, bundle, reader, minimal_task, access)
        self.assertEqual(
            validate_schema("formal-search-request", minimal.payload.to_schema_dict()), [])

    def test_optional_schema_sections_omitted_when_unset(self):
        request, bundle, reader, task, access = make()
        task = replace(task, models=None, corpus=None, max_tactic_steps=None)
        req = compile_formal(request, bundle, reader, task, access).payload.to_schema_dict()
        self.assertNotIn("model_bundle", req)
        self.assertNotIn("corpus_manifest", req)
        self.assertEqual(req["search_policy"],
                         {"policy_name": "gnn-pln-best-first-v1", "deny_sorry": True})

    def test_lean_source_artifact_can_come_from_declaration_body(self):
        extra = {DECL: {"declaration_id": DECL, "status": "searching", "lean_type": "T",
                        "lean_value": "v", "claim_id": f"{P}/c-1",
                        "lean_source_artifact": h("lean-text")}}
        req = compile_formal(*make(extra)).payload.to_schema_dict()
        self.assertEqual(req["lean_source_artifact"], h("lean-text"))

    def test_claim_id_required_and_checked(self):
        no_claim = {DECL: {"declaration_id": DECL, "status": "searching",
                           "lean_type": "T", "lean_value": "v"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(no_claim))
        request, bundle, reader, task, access = make(no_claim)
        out = compile_formal(request, bundle, reader,
                             replace(task, claim_id=f"{P}/c-7"), access)
        self.assertEqual(out.payload.claim_id, f"{P}/c-7")

    def test_claim_id_disagreement_rejected(self):
        request, bundle, reader, task, access = make()  # declaration says c-1
        with self.assertRaises(ContextValidationError):
            compile_formal(request, bundle, reader,
                           replace(task, claim_id=f"{P}/c-9"), access)

    def test_bad_claim_id_rejected(self):
        with self.assertRaises(ContextValidationError):
            replace(make()[3], claim_id="nope")

    def test_deterministic_and_manifest(self):
        a, b = compile_formal(*make()), compile_formal(*make())
        self.assertEqual(a.packet_digest, b.packet_digest)
        self.assertEqual(a.manifest["packet_kind"], "formal")
        self.assertEqual(a.manifest["budgets"]["formal_execution"],
                         {"max_steps": 500, "wall_clock_ms": 60000})

    def test_one_decision_per_source_plus_kernel(self):
        request, bundle, reader, task, access = make()
        out = compile_formal(request, bundle, reader, task, access)
        ids = [d.source_id for d in out.result.selection_decisions]
        self.assertEqual(sorted(ids), sorted([s.source_id for s in bundle.sources] + ["kern"]))

    def test_verified_and_suggested_premises_are_separate(self):
        p = compile_formal(*make()).payload
        self.assertEqual([e.lean_name for e in p.verified_premises], ["Nat.add_comm"])
        self.assertEqual([e.lean_name for e in p.suggested_premises], ["Critic.ok", "My.hint"])
        by_name = {e.lean_name: e for e in p.suggested_premises}
        self.assertEqual(by_name["My.hint"].basis.value, "model-score")
        self.assertEqual(by_name["Critic.ok"].status, "critic-accepted")  # not verified

    def test_premise_exclusions(self):
        d = decisions(compile_formal(*make()))
        self.assertIn("not usable", d["prem-x"].reason)
        self.assertIn("not accessible", d["prem-na"].reason)
        self.assertIn("accessibility not reported", d["prem-un"].reason)

    def test_verified_premise_supersedes_suggestion_with_same_name(self):
        extra = {"prem-dup": {"lean_name": "Nat.add_comm", "statement": "hint",
                              "status": "conjectural", "_type": "premise"}}
        out = compile_formal(*make(extra))
        d = decisions(out)
        self.assertIn("superseded by verified premise prem-v", d["prem-dup"].reason)
        self.assertEqual(len(out.payload.verified_premises), 1)

    def test_prior_runs_filtered_by_declaration_and_environment(self):
        out = compile_formal(*make())
        self.assertEqual([r.run_id for r in out.payload.prior_runs], [f"{P}/fr-2"])
        d = decisions(out)
        self.assertIn("different or unnamed declaration", d[f"{P}/fr-3"].reason)
        self.assertIn("different environment", d[f"{P}/fr-4"].reason)

    def test_resume_of_existing_run(self):
        out = compile_formal(*make(run_id=f"{P}/fr-2"))
        self.assertTrue(out.payload.resume)
        self.assertEqual(decisions(out)[f"{P}/fr-2"].reason, "resumed run")

    def test_resume_with_other_environment_rejected(self):
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(run_id=f"{P}/fr-4"))

    def test_resume_of_other_declaration_rejected(self):
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(run_id=f"{P}/fr-3"))

    def test_transpositions(self):
        out = compile_formal(*make())
        self.assertEqual([t.source_id for t in out.payload.transpositions], ["tr-1"])
        self.assertFalse(decisions(out)["tr-2"].included)

    def test_other_states_excluded(self):
        d = decisions(compile_formal(*make()))
        self.assertFalse(d[f"{P}/fs-5"].included)
        self.assertTrue(d[ROOT].included)

    def test_works_with_mapping_artifact_reader(self):
        request, bundle, reader, task, access = make()
        out = compile_formal(
            request, bundle, MappingArtifactReader(reader.bodies), task, access)
        self.assertEqual(out.packet_digest, compile_formal(
            request, bundle, reader, task, access).packet_digest)

    def test_missing_source_body_raises(self):
        request, bundle, reader, task, access = make()
        with self.assertRaises(ContextValidationError):
            compile_formal(request, bundle, MappingArtifactReader({}), task, access)

    def test_wrong_packet_kind(self):
        request, *rest = make()
        with self.assertRaises(ContextValidationError):
            compile_formal(replace(request, packet_kind=PacketKind.RESEARCH), *rest)

    def test_missing_limits(self):
        request, *rest = make()
        with self.assertRaises(ContextValidationError):
            compile_formal(replace(request, formal_limits=None), *rest)

    def test_missing_declaration(self):
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(remove=[DECL]))

    def test_declaration_needs_lean_type(self):
        extra = {DECL: {"status": "searching", "lean_name": "t", "claim_id": f"{P}/c-1"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))

    def test_real_shaped_declaration_without_lean_value(self):
        extra = {DECL: {"status": "draft", "lean_name": "add_comm_nat_trivial",
                        "lean_type": "∀ (a b : Nat), a + b = b + a",
                        "module_path": "Fixtures.AddComm", "claim_id": f"{P}/c-1"}}
        p = compile_formal(*make(extra)).payload
        self.assertIsNone(p.lean_value)
        self.assertEqual((p.lean_name, p.module_path),
                         ("add_comm_nat_trivial", "Fixtures.AddComm"))

    def test_environment_hash_preferred_over_lake_manifest_hash(self):
        extra = {"env-1": {"toolchain": "t", "lake_manifest_hash": h("other"),
                           "environment_hash": h("lake")}}
        self.assertEqual(
            compile_formal(*make(extra)).payload.to_schema_dict()["environment_hash"],
            h("lake"))

    def test_other_declarations_are_premise_candidates(self):
        extra = {
            "d-ok": {"status": "replay-accepted", "lean_name": "Lib.ok",
                     "lean_type": "T1", "_type": "formal-declaration"},
            "d-draft": {"status": "draft", "lean_name": "Lib.draft",
                        "lean_type": "T2", "_type": "formal-declaration"},
            "d-bad": {"status": "replay-rejected", "lean_name": "Lib.bad",
                      "lean_type": "T3", "_type": "formal-declaration"},
            "d-anon": {"status": "replay-accepted", "lean_type": "T4",
                       "_type": "formal-declaration"},
        }
        request, bundle, reader, task, access = make(extra)
        access = PremiseAccessibility(
            access.accessible | {"Lib.ok", "Lib.draft", "Lib.bad"}, access.unavailable)
        out = compile_formal(request, bundle, reader, task, access)
        verified = [e.lean_name for e in out.payload.verified_premises]
        suggested = [e.lean_name for e in out.payload.suggested_premises]
        self.assertIn("Lib.ok", verified)
        self.assertIn("Lib.draft", suggested)
        self.assertNotIn("Lib.draft", verified)
        d = decisions(out)
        self.assertIn("not usable", d["d-bad"].reason)
        self.assertIn("no lean_name", d["d-anon"].reason)
        self.assertTrue(d[DECL].included)
        self.assertEqual(d[DECL].reason, "selected formal declaration")

    def test_stale_declaration_rejected(self):
        extra = {DECL: {"declaration_id": DECL, "status": "stale",
                        "lean_type": "T", "lean_value": "v"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))

    def test_bad_environment_hash(self):
        extra = {"env-1": {"environment_id": "env-1", "lake_manifest_hash": "nope",
                           "toolchain": "t"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))

    def test_ambiguous_environment(self):
        extra = {"env-2": {"environment_id": "env-2", "lake_manifest_hash": h("x"),
                           "toolchain": "t", "_type": "environment"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))

    def test_declaration_can_name_its_environment(self):
        extra = {
            "env-2": {"environment_id": "env-2", "lake_manifest_hash": h("x"),
                      "toolchain": "t", "_type": "environment"},
            DECL: {"declaration_id": DECL, "status": "searching", "lean_type": "T",
                   "lean_value": "v", "claim_id": f"{P}/c-1", "environment_id": "env-2"},
             ROOT: {"state_id": ROOT, "status": "open", "exact_state_hash": h("root"),
                   "semantic_signature": h("sem-root"), "environment_hash": h("x")},
        }
        out = compile_formal(*make(extra))
        self.assertEqual(out.payload.environment["environment_id"], "env-2")
        self.assertFalse(decisions(out)["env-1"].included)

    def test_unknown_source_type(self):
        extra = {"x": {"foo": 1, "_type": "mystery"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))

    def test_premise_text_may_come_from_claim_text(self):
        extra = {"prem-v": {"lean_name": "Nat.add_comm", "claim_text": "a + b = b + a",
                            "status": "lean-verified"}}
        out = compile_formal(*make(extra))
        self.assertEqual(out.payload.verified_premises[0].statement, "a + b = b + a")

    def test_premise_without_status_rejected(self):
        extra = {"prem-v": {"lean_name": "Nat.add_comm", "statement": "s"}}
        with self.assertRaises(ContextValidationError):
            compile_formal(*make(extra))


if __name__ == "__main__":
    unittest.main()