"""Soundness hardening of formal-search results (Section 6.12).

Each test maps to one named invariant:

* C1  omitted subgoal -> rejection, counted for the omission-rate metric.
* C2  heuristic scores cannot close a state (strong score and fallback score).
* C3  no replay result -> no certificate promotion; stub replay gates it.
* C4  the current environment pin, rather than historical certificates,
  determines whether a result is stale.
* C5  only proved-pending-replay may carry a candidate certificate.
"""

import inspect
import unittest

from commit_gate.gate import CommitGate
from commit_gate.apply import apply_ops
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore

from mathproof.formal_atp import FakeFormalATP, build_result, stub_replay
from mathproof.soundness import (
    SoundnessReason,
    check_environment_staleness,
    validate_formal_search_result,
    violation_counts,
)

ENV_A = "sha256:" + "c3" * 32
ENV_B = "sha256:" + "9d" * 32


def acquire_test_lease(
    store: JournalStore, proof_id: str, lease_id: str, *, actor: str, worker_class: str
) -> int:
    """Run the parallel branch tests before and after the gate lease upgrade."""
    if "actor" in inspect.signature(store.acquire_lease).parameters:
        return store.acquire_lease(
            proof_id, lease_id, actor=actor, worker_class=worker_class
        )
    return store.acquire_lease(proof_id, lease_id)


def multi_child_result() -> dict:
    """The clean three-child trace from the A4 corpus."""
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "formal_traces"
        / "multi_child.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


class TestC1OmittedSubgoal(unittest.TestCase):
    """Invariant 2: five Lean-produced goals require five child states."""

    def test_dropping_one_child_from_three_is_rejected(self):
        result = multi_child_result()
        edge = result["tactic_edges"][0]
        self.assertEqual(edge["subgoal_count"], 3)
        edge["produced_goal_ids"] = edge["produced_goal_ids"][:2]

        violations = validate_formal_search_result(result)
        reasons = {v.reason for v in violations}
        self.assertIn(SoundnessReason.OMITTED_SUBGOAL, reasons)

    def test_undercounting_the_declaration_is_rejected(self):
        result = multi_child_result()
        edge = result["tactic_edges"][0]
        # The list stays complete but lies about what Lean produced.
        edge["subgoal_count"] = 2

        violations = validate_formal_search_result(result)
        self.assertIn(
            SoundnessReason.OMITTED_SUBGOAL, {v.reason for v in violations}
        )

    def test_listing_a_child_without_its_state_is_rejected(self):
        result = multi_child_result()
        result["states"] = [s for s in result["states"] if s["state_id"] != "p1/fs-7"]

        violations = validate_formal_search_result(result)
        self.assertIn(
            SoundnessReason.UNKNOWN_CHILD_STATE, {v.reason for v in violations}
        )

    def test_omissions_feed_the_section_16_2_metric_counter(self):
        result = multi_child_result()
        result["tactic_edges"][0]["produced_goal_ids"] = []
        counts = violation_counts(validate_formal_search_result(result))
        self.assertGreaterEqual(counts.get("omitted-subgoal", 0), 1)


class TestC2HeuristicClosure(unittest.TestCase):
    """Invariant 3: a score never closes a state. Two score variants."""

    @staticmethod
    def _scored_closure(annotations: dict) -> tuple:
        result = build_result("budget-exhausted", {"run_id": "p1/fr-3", "proof_id": "p1"})
        victim = result["states"][1]  # an open frontier leaf
        victim["status"] = "formally-closed"
        victim["annotations"] = annotations
        return validate_formal_search_result(result)

    def test_strong_pln_gnn_scores_cannot_close(self):
        violations = self._scored_closure({"pln_strength": 1.0, "gnn_tactic_prior": 1.0})
        self.assertIn(
            SoundnessReason.HEURISTIC_CLOSURE_ATTEMPT,
            {v.reason for v in violations},
        )

    def test_fallback_random_score_cannot_close_either(self):
        violations = self._scored_closure(
            {"derived_priority": 9999.0, "state_novelty": 0.99}
        )
        self.assertIn(
            SoundnessReason.HEURISTIC_CLOSURE_ATTEMPT,
            {v.reason for v in violations},
        )

    def test_zero_goal_lean_transition_does_close(self):
        """Control: the same closure is legitimate when Lean said zero goals."""
        result = build_result("proved-pending-replay", {"run_id": "p1/fr-1", "proof_id": "p1"})
        root = next(s for s in result["states"] if s["state_id"] == result["root_state_id"])
        root["status"] = "formally-closed"
        root["annotations"] = {"pln_strength": 1.0}
        self.assertEqual(validate_formal_search_result(result), ())


class TestC3ReplayGateStub(unittest.TestCase):
    """Invariant 4: certificate-produced cannot become valid without a replay."""

    REQUEST = {
        "run_id": "p1/fr-1",
        "proof_id": "p1",
        "environment_hash": ENV_A,
        "goal_text": "a + b = b + a",
    }

    def test_proved_pending_replay_requires_a_certificate(self):
        result = build_result("proved-pending-replay", self.REQUEST)
        del result["certificate"]
        self.assertIn(
            SoundnessReason.CERTIFICATE_REQUIRED,
            {v.reason for v in validate_formal_search_result(result)},
        )

    def test_accepted_status_without_replay_result_is_rejected(self):
        result = build_result("proved-pending-replay", self.REQUEST)
        result["certificate"]["status"] = "replay-accepted"

        violations = validate_formal_search_result(result)
        self.assertIn(
            SoundnessReason.PROMOTION_WITHOUT_REPLAY,
            {v.reason for v in violations},
        )

    def test_stub_replay_pass_attaches_and_promotes(self):
        atp = FakeFormalATP(replay_fn=stub_replay("verified"))
        result = build_result("proved-pending-replay", self.REQUEST)
        certificate = dict(result["certificate"])

        verdict = atp.formal_replay(certificate, self.REQUEST["environment_hash"])
        certificate["status"] = "replay-accepted"
        certificate["replay_result"] = verdict
        result["certificate"] = certificate

        self.assertEqual(validate_formal_search_result(result), ())
        self.assertEqual(verdict["status"], "verified")

    def test_rejected_replay_matches_rejected_certificate(self):
        result = build_result("proved-pending-replay", self.REQUEST)
        result["certificate"]["status"] = "replay-rejected"
        result["certificate"]["replay_result"] = stub_replay("rejected")(
            result["certificate"], ENV_A
        )
        self.assertEqual(validate_formal_search_result(result), ())

    def test_stub_replay_fail_contradicts_accepted_status(self):
        atp = FakeFormalATP(replay_fn=stub_replay("rejected"))
        result = build_result("proved-pending-replay", self.REQUEST)
        certificate = dict(result["certificate"])
        certificate["replay_result"] = atp.formal_replay(certificate, ENV_A)
        certificate["status"] = "replay-accepted"  # lying about the outcome
        result["certificate"] = certificate

        violations = validate_formal_search_result(result)
        self.assertIn(
            SoundnessReason.PROMOTION_WITHOUT_REPLAY,
            {v.reason for v in violations},
        )

    def test_replay_environment_must_be_present_and_match(self):
        result = build_result("proved-pending-replay", self.REQUEST)
        result["certificate"]["status"] = "replay-accepted"
        result["certificate"]["replay_result"] = {"status": "verified"}
        self.assertIn(
            SoundnessReason.REPLAY_ENVIRONMENT_MISMATCH,
            {v.reason for v in validate_formal_search_result(result)},
        )
        result["certificate"]["replay_result"]["environment_hash"] = ENV_B
        self.assertIn(
            SoundnessReason.REPLAY_ENVIRONMENT_MISMATCH,
            {v.reason for v in validate_formal_search_result(result)},
        )

    def test_gate_state_machine_blocks_the_direct_jump(self):
        """The same contract enforced by the commit gate's transition table."""
        view = MemoryView()
        store = JournalStore()
        gate = CommitGate(view, store)
        token = acquire_test_lease(
            store, "p1", "lease-1", actor="coordinator", worker_class="coordinator"
        )

        # The newer gate accepts a candidate only with a complete declaration,
        # producing-run, and pinned-environment binding.
        create = Proposal(
            proof_id="p1",
            actor="coordinator",
            worker_class="coordinator",
            ops=(
                UpsertNode("FormalDeclaration", "p1/decl-1", {}),
                UpsertNode("Environment", "p1/env-1", {"environment_hash": ENV_A}),
                UpsertNode(
                    "FormalRun", "p1/run-1",
                    {"status": "proved-pending-replay", "environment_hash": ENV_A},
                ),
                UpsertNode(
                    "Certificate", "p1/cert-1",
                    {
                        "status": "candidate",
                        "artifact_hash": "sha256:" + "18" * 32,
                        "environment_hash": ENV_A,
                        "producer_run_id": "p1/run-1",
                    },
                ),
                AddEdge("CERTIFIES", "p1/cert-1", "p1/decl-1", "p1/certifies-1"),
                AddEdge(
                    "PRODUCED_CERTIFICATE", "p1/run-1", "p1/cert-1", "p1/produced-1"
                ),
                AddEdge("SEARCHES", "p1/run-1", "p1/decl-1", "p1/searches-1"),
                AddEdge(
                    "PINNED_ENVIRONMENT", "p1/decl-1", "p1/env-1", "p1/pin-1"
                ),
                AddEdge(
                    "CERTIFICATE_ENVIRONMENT", "p1/cert-1", "p1/env-1", "p1/cert-env-1"
                ),
                AddEdge("RAN_UNDER", "p1/run-1", "p1/env-1", "p1/run-env-1"),
            ),
            base_revision=0,
            lease_id="lease-1",
            fencing_token=token,
        )
        accepted = gate.commit(create)
        self.assertTrue(accepted.accepted, accepted.rejections)
        apply_ops(view, create.ops)

        skip = Proposal(
            proof_id="p1",
            actor="coordinator",
            worker_class="coordinator",
            ops=(
                SetField(
                    "Certificate", "p1/cert-1", "status",
                    "replay-accepted", prior="candidate",
                ),
            ),
            base_revision=1,
            lease_id="lease-1",
            fencing_token=token,
        )
        result = gate.commit(skip)
        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections], [Reason.ILLEGAL_STATUS_TRANSITION]
        )


class TestC4EnvironmentDrift(unittest.TestCase):
    """Sections 6.12 / 11.4: the current pin controls acceptance."""

    @staticmethod
    def _certificate(env_hash: str) -> dict:
        return {
            "declaration_id": "p1/fd-1",
            "artifact_hash": "sha256:" + "18" * 32,
            "environment_hash": env_hash,
        }

    def test_result_under_current_pin_is_current(self):
        current = self._certificate(ENV_B)
        self.assertIsNone(check_environment_staleness(current, ENV_B))

    def test_result_under_old_pin_is_stale(self):
        old = self._certificate(ENV_A)
        violation = check_environment_staleness(old, ENV_B)

        self.assertIsNotNone(violation)
        self.assertEqual(violation.reason, SoundnessReason.ENVIRONMENT_STALE)

    def test_old_certificate_does_not_stale_result_under_new_pin(self):
        # A historical ENV_A certificate may still exist after a legitimate
        # pin change. The new ENV_B result must be judged against ENV_B.
        prior = self._certificate(ENV_A)
        current = {**prior, "environment_hash": ENV_B}
        self.assertIsNone(check_environment_staleness(current, ENV_B))

    def test_validator_uses_supplied_current_pin(self):
        result = build_result("proved-pending-replay", {
            "run_id": "p1/fr-1", "proof_id": "p1", "environment_hash": ENV_A,
        })
        self.assertIn(
            SoundnessReason.ENVIRONMENT_STALE,
            {v.reason for v in validate_formal_search_result(
                result, pinned_environment_hash=ENV_B
            )},
        )


class TestC5StaleFencingToken(unittest.TestCase):
    """Invariants 8/10 via the real gate: stale token rejected *and* journalled.

    The scenario: worker A holds the lease, commits, then its lease is
    renewed (a second acquire bumps the fencing token). A result still in
    flight under the old token arrives -- it must be refused, leave committed
    state untouched, and survive in the rejection audit log.
    """

    LEASE = "lease-worker-a"

    def setUp(self):
        self.view = MemoryView()
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)

        self.token_one = acquire_test_lease(
            self.store, "p1", self.LEASE,
            actor="worker-a", worker_class="formal-atp",
        )
        first = Proposal(
            proof_id="p1",
            actor="worker-a",
            worker_class="formal-atp",
            ops=(UpsertNode("FormalState", "p1/fs-1", {"status": "open"}),),
            base_revision=0,
            lease_id=self.LEASE,
            fencing_token=self.token_one,
        )
        self.assertTrue(self.gate.commit(first).accepted)
        apply_ops(self.view, first.ops)

        # The lease is renewed; token_one is now obsolete.
        self.token_two = acquire_test_lease(
            self.store, "p1", self.LEASE,
            actor="worker-a", worker_class="formal-atp",
        )
        self.assertGreater(self.token_two, self.token_one)

    def _late_proposal(self) -> Proposal:
        return Proposal(
            proof_id="p1",
            actor="worker-a",
            worker_class="formal-atp",
            ops=(
                SetField(
                    "FormalState", "p1/fs-1", "status",
                    "expanded", prior="open",
                ),
            ),
            base_revision=1,
            lease_id=self.LEASE,
            fencing_token=self.token_one,
        )

    def test_superseded_token_is_rejected_with_typed_reason(self):
        result = self.gate.commit(self._late_proposal())
        self.assertFalse(result.accepted)
        self.assertEqual(
            [r.reason for r in result.rejections],
            [Reason.FENCING_TOKEN_SUPERSEDED],
        )

    def test_rejected_write_changes_no_committed_state(self):
        head_before = self.store.head("p1")
        self.gate.commit(self._late_proposal())
        self.assertEqual(self.store.head("p1"), head_before)
        self.assertEqual(len(self.store.read_events("p1")), 1)

    def test_rejection_remains_journaled_for_audit(self):
        proposal = self._late_proposal()
        self.gate.commit(proposal)

        audit = self.store.read_rejections("p1")
        self.assertEqual(len(audit), 1)
        entry = audit[0]
        self.assertEqual(entry["reason"], Reason.FENCING_TOKEN_SUPERSEDED.value)
        self.assertIsNotNone(entry["payload"])
        self.assertEqual(entry["payload"]["fencing_token"], self.token_one)
        self.assertEqual(
            entry["payload"]["ops"][0]["field"], "status"
        )

    def test_current_token_commits_where_the_stale_one_could_not(self):
        self.gate.commit(self._late_proposal())
        fresh = Proposal(
            proof_id="p1",
            actor="worker-a",
            worker_class="formal-atp",
            ops=(
                SetField(
                    "FormalState", "p1/fs-1", "status",
                    "expanded", prior="open",
                ),
            ),
            base_revision=1,
            lease_id=self.LEASE,
            fencing_token=self.token_two,
        )
        self.assertTrue(self.gate.commit(fresh).accepted)


class TestC5CertificateDisposition(unittest.TestCase):
    def test_non_proof_dispositions_cannot_carry_certificate(self):
        for disposition in ("budget-exhausted", "stagnated", "internal-error"):
            with self.subTest(disposition=disposition):
                result = build_result(disposition, {
                    "run_id": "p1/fr-1", "proof_id": "p1", "environment_hash": ENV_A,
                })
                result["certificate"] = {
                    "artifact_hash": "sha256:" + "18" * 32,
                    "status": "candidate",
                }
                self.assertIn(
                    SoundnessReason.CERTIFICATE_DISPOSITION_MISMATCH,
                    {v.reason for v in validate_formal_search_result(result)},
                )

    def test_certificate_requires_closed_root(self):
        result = build_result("proved-pending-replay", {
            "run_id": "p1/fr-1", "proof_id": "p1", "environment_hash": ENV_A,
        })
        result["tactic_edges"][0]["tactic_label"] = "PLN_fallback"
        self.assertIn(
            SoundnessReason.CERTIFICATE_WITHOUT_KERNEL_PROOF,
            {v.reason for v in validate_formal_search_result(result)},
        )


if __name__ == "__main__":
    unittest.main()
