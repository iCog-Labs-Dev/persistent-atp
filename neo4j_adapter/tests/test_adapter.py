import unittest

from neo4j_adapter.adapter import Neo4jAdapter  # rename applied, per earlier fix


class TestNeo4jAdapter(unittest.TestCase):
    """Integration tests against a live local Neo4j instance.

    Uses a dedicated proof_id so runs never collide with real workspace
    data, and wipes that namespace in tearDown.
    """

    PROOF_ID = "test-proof-adapter"

    def setUp(self):
        self.adapter = Neo4jAdapter()
        self.adapter.init_proof(
            proof_id=self.PROOF_ID,
            theorem_kernel="test kernel",
            theorem_hash="hash-abc",
            event_id="ev-setup",
        )

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
        self.adapter.close()

    # -- Proof --------------------------------------------------------

    def test_init_proof_creates_proof_node_with_correct_properties(self):
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (p:Proof {proof_id: $pid, id: $pid}) RETURN p",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)
        self.assertEqual(record["p"]["theorem_kernel"], "test kernel")
        self.assertEqual(record["p"]["theorem_hash"], "hash-abc")
        self.assertEqual(record["p"]["active_revision"], 0)

    # -- States ---------------------------------------------------------

    def test_add_state_creates_state_and_links_to_proof(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "root goal", event_id="ev1")

        state = self.adapter.get_state("s1", self.PROOF_ID)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["description"], "root goal")

        with self.adapter._driver.session() as s:
            linked = s.run(
                "MATCH (:Proof {proof_id: $pid, id: $pid})-[:HAS_STATE]->"
                "(st:State {id: 's1'}) RETURN st",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(linked)

    def test_add_state_links_child_to_parent_via_child_of(self):
        self.adapter.add_state(self.PROOF_ID, "root", "root goal", event_id="ev1")
        self.adapter.add_state(
            self.PROOF_ID, "child1", "subgoal", parent_id="root", event_id="ev2"
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (c:State {proof_id: $pid, id: 'child1'})"
                "-[:CHILD_OF]->(p:State {proof_id: $pid, id: 'root'}) RETURN c",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    def test_add_state_rejects_invalid_kind(self):
        with self.assertRaises(ValueError):
            self.adapter.add_state(
                self.PROOF_ID, "s1", "goal", kind="not-a-real-kind"
            )
    def test_update_state_status_persists_and_records_reason(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.update_state_status(
            self.PROOF_ID, "s1", status="closed", reason="proved", event_id="ev2"
        )
        state = self.adapter.get_state("s1", self.PROOF_ID)
        self.assertEqual(state["status"], "formally-closed")  
        self.assertEqual(state["closed_reason"], "proved")

    def test_update_state_status_rejects_invalid_status(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        with self.assertRaises(ValueError):
            self.adapter.update_state_status(self.PROOF_ID, "s1", status="bogus")

    def test_get_state_returns_none_when_missing(self):
        self.assertIsNone(self.adapter.get_state("nope", self.PROOF_ID))

    # -- Claims -----------------------------------------------------------

    def test_add_claim_creates_claim_and_links_to_proof(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "n+0=n", event_id="ev1")
        claims = self.adapter.get_all_claims(self.PROOF_ID)
        ids = [c["id"] for c in claims]
        self.assertIn("c1", ids)
        c1 = next(c for c in claims if c["id"] == "c1")
        self.assertEqual(c1["status"], "conjectural")  # documented default

    def test_add_claim_rejects_invalid_status(self):
        with self.assertRaises(ValueError):
            self.adapter.add_claim(self.PROOF_ID, "c1", "stmt", status="bogus")

    def test_update_claim_status_persists(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt", event_id="ev1")
        self.adapter.update_claim_status(
            "c1", status="critic-accepted", proof_id=self.PROOF_ID, event_id="ev2"
        )
        claim = next(
            c for c in self.adapter.get_all_claims(self.PROOF_ID) if c["id"] == "c1"
        )
        self.assertEqual(claim["status"], "critic-accepted")  # Updated from "supported"

    def test_add_claim_dependency_creates_depends_on_edge(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt1", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "stmt2", event_id="ev2")
        self.adapter.add_claim_dependency(
            "c1", "c2", proof_id=self.PROOF_ID, event_id="ev3"
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Claim {proof_id: $pid, id: 'c1'})"
                "-[:DEPENDS_ON]->(:Claim {proof_id: $pid, id: 'c2'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    def test_add_claim_dependency_rejects_cycle_when_proof_id_given(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt1", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "stmt2", event_id="ev2")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID)

        with self.assertRaises(ValueError):
            # c2 -> c1 would close a cycle since c1 -> c2 already exists
            self.adapter.add_claim_dependency("c2", "c1", proof_id=self.PROOF_ID)

    def test_add_claim_dependency_skips_cycle_check_when_proof_id_omitted(self):
        """Known gap: cycle guard is bypassed if proof_id is left as ''.

        This test documents current behavior rather than asserting it's
        correct — worth raising with the team as a possible follow-up fix.
        """
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt1", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "stmt2", event_id="ev2")
        self.adapter.add_claim_dependency("c1", "c2", proof_id=self.PROOF_ID)

        # No proof_id passed here -- does NOT raise, even though it's a cycle.
        try:
            self.adapter.add_claim_dependency("c2", "c1")
        except ValueError:
            self.fail(
                "cycle check unexpectedly ran without proof_id -- "
                "if this now raises, the underlying bug has been fixed; "
                "update this test to assertRaises instead."
            )

    # -- Moves and subgoals -----------------------------------------------

    def test_add_move_creates_move_and_links_via_proposes(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.add_move(
            self.PROOF_ID, "m1", "s1", "try induction", event_id="ev2"
        )
        moves = self.adapter.get_moves_for_state("s1", self.PROOF_ID)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["move_summary"], "try induction")
        self.assertEqual(moves[0]["status"], "queued")

    def test_add_move_rejects_invalid_status(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        with self.assertRaises(ValueError):
            self.adapter.add_move(
                self.PROOF_ID, "m1", "s1", "try induction", status="bogus"
            )

    def test_add_required_subgoal_creates_state_and_links_via_requires(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "split into cases", event_id="ev2")
        self.adapter.add_required_subgoal(
            self.PROOF_ID, "m1", "sub1", "base case", event_id="ev3"
        )

        subgoals = self.adapter.get_subgoals_for_move("m1", self.PROOF_ID)
        self.assertEqual(len(subgoals), 1)
        self.assertEqual(subgoals[0]["id"], "sub1")
        self.assertEqual(subgoals[0]["kind"], "and")
        self.assertEqual(subgoals[0]["status"], "open")

    def test_update_move_status_persists(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "induction", event_id="ev2")
        self.adapter.update_move_status("m1", "leased", proof_id=self.PROOF_ID, event_id="ev3")
        move = self.adapter.get_moves_for_state("s1", self.PROOF_ID)[0]
        self.assertEqual(move["status"], "leased")

    # -- context_for --------------------------------------------------

    def test_context_for_returns_complete_dict(self):
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "induction", event_id="ev2")
        self.adapter.add_required_subgoal(self.PROOF_ID, "m1", "sub1", "base case", event_id="ev3")
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt", event_id="ev4")
        self.adapter.add_attempt(
            self.PROOF_ID, "a1", "s1", "tried induction", event_id="ev5"
        )

        ctx = self.adapter.context_for(self.PROOF_ID, "s1")

        self.assertEqual(ctx["state"]["id"], "s1")
        self.assertEqual(len(ctx["moves"]), 1)
        self.assertEqual(len(ctx["attempts"]), 1)
        self.assertEqual(len(ctx["claims"]), 1)
        self.assertEqual(len(ctx["subgoals"]), 1)
        self.assertIn("frontier", ctx)  # exercises eligible_frontier() indirectly

    # -- add_relation whitelist -------------------------------------------

    def test_add_relation_accepts_whitelisted_type(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt1", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "stmt2", event_id="ev2")
        # SUPERSEDES is in REL_WHITELIST
        self.adapter.add_relation(self.PROOF_ID, "SUPERSEDES", "c1", "c2", event_id="ev3")

        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Claim {proof_id: $pid, id: 'c1'})"
                "-[:SUPERSEDES]->(:Claim {proof_id: $pid, id: 'c2'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    def test_add_relation_rejects_non_whitelisted_type(self):
        self.adapter.add_claim(self.PROOF_ID, "c1", "stmt1", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c2", "stmt2", event_id="ev2")
        with self.assertRaises(ValueError):
            # DROP TABLE isn't a real Cypher risk here since it's an f-string
            # relationship *type*, not a full query -- but it's still not
            # whitelisted, which is what we're actually testing.
            self.adapter.add_relation(self.PROOF_ID, "NOT_A_REAL_REL", "c1", "c2")
class TestNeo4jAdapterFullCRUDCoverage(unittest.TestCase):
    """every CRUD method gets at least one test."""

    PROOF_ID = "test-proof-crud-coverage"

    def setUp(self):
        self.adapter = Neo4jAdapter()
        self.adapter.init_proof(self.PROOF_ID, "test kernel", event_id="ev0")
        self.adapter.add_state(self.PROOF_ID, "s1", "goal", event_id="ev1")
        self.adapter.add_claim(self.PROOF_ID, "c1", "some claim", event_id="ev2")
        self.adapter.add_attempt(self.PROOF_ID, "a1", "s1", "tried something", event_id="ev3")

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
        self.adapter.close()

    # -- link_state_claim --------------------------------------------------

    def test_link_state_claim_creates_uses_claim_edge(self):
        self.adapter.link_state_claim(self.PROOF_ID, "s1", "c1", event_id="ev4")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:State {proof_id: $pid, id: 's1'})-[:USES_CLAIM]->"
                "(:Claim {proof_id: $pid, id: 'c1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    # -- attempts -----------------------------------------------------------

    def test_add_attempt_creates_attempt_linked_to_state(self):
        attempts = self.adapter.get_attempts_for_state("s1", self.PROOF_ID)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["id"], "a1")
        self.assertEqual(attempts[0]["status"], "pending")

    def test_add_attempt_with_move_id_links_on_move(self):
        self.adapter.add_move(self.PROOF_ID, "m1", "s1", "a move", event_id="ev5")
        self.adapter.add_attempt(
            self.PROOF_ID, "a2", "s1", "second try", move_id="m1", event_id="ev6",
        )
        attempts = self.adapter.get_attempts_for_move("m1", self.PROOF_ID)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["id"], "a2")

    def test_update_attempt_persists_status_and_evidence(self):
        self.adapter.update_attempt(
            "a1", "supported", evidence="looks right", proof_id=self.PROOF_ID, event_id="ev7",
        )
        attempts = self.adapter.get_attempts_for_state("s1", self.PROOF_ID)
        self.assertEqual(attempts[0]["status"], "supported")
        self.assertEqual(attempts[0]["evidence"], "looks right")

    # -- routes ---------------------------------------------------------

    def test_add_route_creates_route_node(self):
        self.adapter.add_route(self.PROOF_ID, "r1", "root/strategy-1", event_id="ev8")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (r:Route {proof_id: $pid, id: 'r1'}) RETURN r.display_path AS p",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["p"], "root/strategy-1")

    def test_link_attempt_route_creates_via_route_edge(self):
        self.adapter.add_route(self.PROOF_ID, "r1", "root/strategy-1", event_id="ev8")
        self.adapter.link_attempt_route(self.PROOF_ID, "a1", "r1", event_id="ev9")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:VIA_ROUTE]->"
                "(:Route {proof_id: $pid, id: 'r1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    # -- contexts -------------------------------------------------------

    def test_add_context_creates_context_node_with_fields(self):
        self.adapter.add_context(
            self.PROOF_ID, "ctx1", packet_hash="abc123", compiler_version="0.1.0",
            token_budget=60000, token_count=4200, event_id="ev10",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (c:Context {proof_id: $pid, id: 'ctx1'}) "
                "RETURN c.token_budget AS budget, c.token_count AS count",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["budget"], 60000)
        self.assertEqual(record["count"], 4200)

    def test_link_attempt_context_creates_used_context_edge(self):
        self.adapter.add_context(self.PROOF_ID, "ctx1", event_id="ev10")
        self.adapter.link_attempt_context(self.PROOF_ID, "a1", "ctx1", event_id="ev11")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:USED_CONTEXT]->"
                "(:Context {proof_id: $pid, id: 'ctx1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    # -- link_produced_claim -----------------------------------------------

    def test_link_produced_claim_creates_produced_claim_edge(self):
        self.adapter.link_produced_claim(self.PROOF_ID, "a1", "c1", event_id="ev12")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:PRODUCED_CLAIM]->"
                "(:Claim {proof_id: $pid, id: 'c1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    # -- artifacts -----------------------------------------------------

    def test_add_artifact_creates_artifact_node(self):
        self.adapter.add_artifact(
            self.PROOF_ID, "art1", kind="lean_snippet", media_type="text/plain",
            sha256="deadbeef", filename="attempt-1-lean.txt", event_id="ev13",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (a:Artifact {proof_id: $pid, id: 'art1'}) "
                "RETURN a.kind AS kind, a.sha256 AS sha",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["kind"], "lean_snippet")
        self.assertEqual(record["sha"], "deadbeef")

    def test_link_artifact_creates_produced_artifact_edge(self):
        self.adapter.add_artifact(self.PROOF_ID, "art1", kind="lean_snippet", event_id="ev13")
        self.adapter.link_artifact(self.PROOF_ID, "a1", "art1", event_id="ev14")
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:PRODUCED_ARTIFACT]->"
                "(:Artifact {proof_id: $pid, id: 'art1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertIsNotNone(record)

    # -- critiques, experiments, verifications ------------------------------

    def test_add_critique_creates_critique_linked_to_attempt(self):
        self.adapter.add_critique(
            self.PROOF_ID, "cr1", "a1", verdict="no local defect found",
            reason="checked dependencies", critic_worker="critic-01", event_id="ev15",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:HAD_CRITIQUE]->"
                "(cr:Critique {proof_id: $pid, id: 'cr1'}) RETURN cr.verdict AS v",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["v"], "no local defect found")

    def test_add_experiment_creates_experiment_linked_to_attempt(self):
        self.adapter.add_experiment(
            self.PROOF_ID, "exp1", "a1", question="does the bound hold for n<=8?",
            status="ran", event_id="ev16",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:RAN]->"
                "(e:Experiment {proof_id: $pid, id: 'exp1'}) RETURN e.question AS q",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["q"], "does the bound hold for n<=8?")

    def test_add_verification_creates_verification_linked_to_attempt_and_claim(self):
        self.adapter.add_verification(
            self.PROOF_ID, "ver1", "a1", "c1", kind="lean", status="verified",
            lean_name="my_theorem", toolchain_hash="abc", event_id="ev17",
        )
        with self.adapter._driver.session() as s:
            to_verification = s.run(
                "MATCH (:Attempt {proof_id: $pid, id: 'a1'})-[:HAD_VERIFICATION]->"
                "(v:Verification {proof_id: $pid, id: 'ver1'}) RETURN v.status AS status",
                pid=self.PROOF_ID,
            ).single()
            to_claim = s.run(
                "MATCH (:Verification {proof_id: $pid, id: 'ver1'})-[:OF]->"
                "(:Claim {proof_id: $pid, id: 'c1'}) RETURN 1",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(to_verification["status"], "verified")
        self.assertIsNotNone(to_claim)

    # -- concepts and hypotheses ---------------------------------------

    def test_add_concept_creates_concept_node(self):
        self.adapter.add_concept(
            self.PROOF_ID, "concept1", name="rank-collapse", mechanism_tags="linear-algebra",
            event_id="ev18",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (c:Concept {proof_id: $pid, id: 'concept1'}) RETURN c.name AS name",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["name"], "rank-collapse")

    def test_add_hypothesis_creates_hypothesis_targeting_state(self):
        self.adapter.add_hypothesis(
            self.PROOF_ID, "h1", kind="analogy-transfer", target_state_id="s1",
            falsification_test="check n=3 case", novelty=0.8, abductive_strength=0.6,
            event_id="ev19",
        )
        with self.adapter._driver.session() as s:
            record = s.run(
                "MATCH (h:Hypothesis {proof_id: $pid, id: 'h1'})-[:TARGETS]->"
                "(:State {proof_id: $pid, id: 's1'}) "
                "RETURN h.kind AS kind, h.layer AS layer",
                pid=self.PROOF_ID,
            ).single()
        self.assertEqual(record["kind"], "analogy-transfer")
        self.assertEqual(record["layer"], "speculative")


if __name__ == "__main__":
    unittest.main()