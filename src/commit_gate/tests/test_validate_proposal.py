"""Proposal-only validator tests.

Includes the hardening tests from 6.12 that are decidable without reading
committed state: subgoal conservation, scores not closing states, and
executor failures not passing as zero-goal successes.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.validate import validate_proposal
from commit_gate.vocab import ExecutorResult, FormalStateStatus, TacticStatus, WorkerClass

PROOF = "p17"


def propose(*ops) -> Proposal:
    return Proposal(
        proof_id=PROOF,
        actor="atp-worker-3",
        worker_class=WorkerClass.FORMAL_ATP,
        ops=tuple(ops),
        base_revision=41,
        lease_id="lease-9",
        fencing_token=7,
    )


def tactic(
    node_id: str = "p17/ta8",
    *,
    result: str = ExecutorResult.LEAN_ACCEPTED,
    subgoals: int | None = 2,
    status: str = TacticStatus.PENDING,
    diagnostic: str | None = None,
) -> UpsertNode:
    fields: dict[str, object] = {
        "tactic_label": "induction n",
        "tactic_family": "induction",
        "executor_result": str(result),
        "status": str(status),
    }
    if subgoals is not None:
        fields["subgoal_count"] = subgoals
    if diagnostic is not None:
        fields["diagnostic_artifact"] = diagnostic
    return UpsertNode("TacticApplication", node_id, fields)


def state(node_id: str, **extra) -> UpsertNode:
    return UpsertNode(
        "FormalState",
        node_id,
        {"goal_text": f"goal for {node_id}", "exact_hash": f"sha256:{node_id}", **extra},
    )


def requires(tactic_id: str, child_id: str, position: int, edge: str) -> AddEdge:
    return AddEdge("FORMAL_REQUIRES", tactic_id, child_id, edge, {"child_index": position})


def expansion(count: int) -> Proposal:
    """A well-formed expansion of p17/fs1 into `count` required children."""
    ops: list[object] = [
        tactic(subgoals=count),
        AddEdge("HAS_TACTIC", "p17/fs1", "p17/ta8", "p17/e0"),
        SetField(
            "FormalState",
            "p17/fs1",
            "status",
            FormalStateStatus.EXPANDED,
            prior=FormalStateStatus.OPEN,
        ),
    ]
    for position in range(count):
        child = f"p17/fs{position + 2}"
        ops.append(state(child))
        ops.append(requires("p17/ta8", child, position, f"p17/e{position + 1}"))
    return propose(*ops)


def reasons_of(proposal: Proposal) -> list[Reason]:
    return [finding.reason for finding in validate_proposal(proposal)]


class WellFormedProposals(unittest.TestCase):
    def test_five_goals_create_five_required_children(self):
        """6.12: a tactic returning five goals yields five required children."""
        self.assertEqual(validate_proposal(expansion(5)), [])

    def test_zero_goal_closure_is_accepted(self):
        proposal = propose(
            tactic(result=ExecutorResult.LEAN_ACCEPTED, subgoals=0, status=TacticStatus.CLOSED),
            AddEdge("HAS_TACTIC", "p17/fs1", "p17/ta8", "p17/e0"),
            AddEdge("CLOSES_STATE", "p17/ta8", "p17/fs1", "p17/e1"),
            SetField(
                "FormalState",
                "p17/fs1",
                "status",
                FormalStateStatus.FORMALLY_CLOSED,
                prior=FormalStateStatus.OPEN,
            ),
        )
        self.assertEqual(validate_proposal(proposal), [])

    def test_annotation_write_needs_no_prior(self):
        proposal = propose(SetField("FormalState", "p17/fs1", "pln_strength", 0.7))
        self.assertEqual(validate_proposal(proposal), [])

    def test_content_addressed_endpoint_is_exempt_from_proof_scope(self):
        proposal = propose(
            AddEdge("HAS_ARTIFACT", "p17/fs1", "sha256:" + "a" * 64, "p17/e2")
        )
        self.assertEqual(validate_proposal(proposal), [])


class SubgoalConservation(unittest.TestCase):
    def test_omitted_child_is_rejected(self):
        """6.12: a proof with one omitted child is rejected.

        Lean returned five goals; the proposal records only four children.
        """
        proposal = propose(
            tactic(subgoals=5),
            state("p17/fs2"),
            state("p17/fs3"),
            state("p17/fs4"),
            state("p17/fs5"),
            requires("p17/ta8", "p17/fs2", 0, "p17/e1"),
            requires("p17/ta8", "p17/fs3", 1, "p17/e2"),
            requires("p17/ta8", "p17/fs4", 2, "p17/e3"),
            requires("p17/ta8", "p17/fs5", 3, "p17/e4"),
        )
        self.assertIn(Reason.SUBGOAL_COUNT_MISMATCH, reasons_of(proposal))

    def test_duplicated_child_is_rejected(self):
        proposal = propose(
            tactic(subgoals=2),
            state("p17/fs2"),
            requires("p17/ta8", "p17/fs2", 0, "p17/e1"),
            requires("p17/ta8", "p17/fs2", 1, "p17/e2"),
        )
        self.assertIn(Reason.SUBGOAL_DUPLICATED, reasons_of(proposal))

    def test_gappy_child_index_is_rejected(self):
        proposal = propose(
            tactic(subgoals=2),
            state("p17/fs2"),
            state("p17/fs3"),
            requires("p17/ta8", "p17/fs2", 0, "p17/e1"),
            requires("p17/ta8", "p17/fs3", 2, "p17/e2"),
        )
        self.assertIn(Reason.SUBGOAL_INDEX_INVALID, reasons_of(proposal))

    def test_obligation_cannot_be_added_to_an_existing_tactic(self):
        proposal = propose(
            state("p17/fs9"),
            requires("p17/ta1", "p17/fs9", 0, "p17/e3"),
        )
        self.assertIn(Reason.ORPHAN_SUBGOAL_EDGE, reasons_of(proposal))

    def test_obligation_cannot_be_removed(self):
        proposal = propose(RemoveEdge("FORMAL_REQUIRES", "p17/e1"))
        self.assertIn(Reason.FORMAL_REQUIRES_REMOVAL, reasons_of(proposal))

    def test_missing_subgoal_count_is_rejected(self):
        proposal = propose(tactic(subgoals=None))
        self.assertIn(Reason.MISSING_REQUIRED_FIELD, reasons_of(proposal))


class ExecutorResultGating(unittest.TestCase):
    def test_failed_tactic_is_not_a_zero_goal_success(self):
        """6.12: a failed tactic must not be recorded as a zero-goal success."""
        proposal = propose(
            tactic(result=ExecutorResult.TIMEOUT, subgoals=0, status=TacticStatus.CLOSED)
        )
        self.assertIn(Reason.EXECUTOR_FAILURE_AS_SUCCESS, reasons_of(proposal))

    def test_missing_backend_cannot_close_a_state(self):
        proposal = propose(
            tactic(result=ExecutorResult.BACKEND_MISSING, subgoals=0),
            AddEdge("CLOSES_STATE", "p17/ta8", "p17/fs1", "p17/e1"),
        )
        self.assertIn(Reason.CLOSURE_WITHOUT_LEAN_ACCEPTED, reasons_of(proposal))

    def test_infrastructure_failure_carries_no_mathematical_diagnostic(self):
        proposal = propose(
            tactic(result=ExecutorResult.CRASH, subgoals=0, diagnostic="sha256:" + "b" * 64)
        )
        self.assertIn(Reason.FAILURE_WITH_MATHEMATICAL_DIAGNOSTIC, reasons_of(proposal))

    def test_lean_rejection_requires_a_diagnostic(self):
        proposal = propose(tactic(result=ExecutorResult.LEAN_REJECTED, subgoals=0))
        self.assertIn(Reason.DEAD_EDGE_MISSING_DIAGNOSTIC, reasons_of(proposal))

    def test_closure_with_remaining_subgoals_is_rejected(self):
        proposal = propose(
            tactic(result=ExecutorResult.LEAN_ACCEPTED, subgoals=2),
            state("p17/fs2"),
            state("p17/fs3"),
            requires("p17/ta8", "p17/fs2", 0, "p17/e1"),
            requires("p17/ta8", "p17/fs3", 1, "p17/e2"),
            AddEdge("CLOSES_STATE", "p17/ta8", "p17/fs1", "p17/e3"),
        )
        self.assertIn(Reason.CLOSURE_WITHOUT_ZERO_GOALS, reasons_of(proposal))

    def test_closure_claiming_an_absent_tactic_is_rejected(self):
        proposal = propose(AddEdge("CLOSES_STATE", "p17/ta1", "p17/fs1", "p17/e1"))
        self.assertIn(Reason.ORPHAN_CLOSURE_EDGE, reasons_of(proposal))


class AnnotationSeparation(unittest.TestCase):
    def test_a_score_of_one_cannot_close_a_state(self):
        """6.12: a score of 1.0 must not close a state."""
        proposal = propose(
            SetField("FormalState", "p17/fs1", "gnn_tactic_prior", 1.0, prior=0.4),
            SetField(
                "FormalState",
                "p17/fs1",
                "status",
                FormalStateStatus.FORMALLY_CLOSED,
                prior=FormalStateStatus.OPEN,
            ),
        )
        self.assertIn(Reason.HEURISTIC_CLOSURE_ATTEMPT, reasons_of(proposal))

    def test_status_write_must_state_its_expected_prior(self):
        proposal = propose(
            SetField("FormalState", "p17/fs1", "status", FormalStateStatus.EXPANDED)
        )
        self.assertIn(Reason.MISSING_PRIOR_VALUE, reasons_of(proposal))

    def test_expected_null_prior_is_distinct_from_no_expectation(self):
        proposal = propose(SetField("Alignment", "p17/al1", "verdict", "aligned", prior=None))
        self.assertEqual(validate_proposal(proposal), [])


class Vocabulary(unittest.TestCase):
    def test_invented_state_status_is_rejected(self):
        proposal = propose(
            SetField("FormalState", "p17/fs1", "status", "probably-closed", prior="open")
        )
        self.assertIn(Reason.UNKNOWN_STATUS_VALUE, reasons_of(proposal))

    def test_invented_executor_result_is_rejected(self):
        proposal = propose(tactic(result="worked-i-think", subgoals=0))
        self.assertIn(Reason.UNKNOWN_STATUS_VALUE, reasons_of(proposal))

    def test_stagnated_run_disposition_is_legal(self):
        proposal = propose(
            SetField("FormalRun", "p17/fr1", "status", "stagnated", prior="searching")
        )
        self.assertEqual(validate_proposal(proposal), [])


class Namespacing(unittest.TestCase):
    def test_foreign_node_is_rejected(self):
        proposal = propose(state("p22/fs4"))
        self.assertIn(Reason.NAMESPACE_MISMATCH, reasons_of(proposal))

    def test_foreign_edge_target_is_rejected(self):
        proposal = propose(AddEdge("HAS_TACTIC", "p17/fs1", "p22/ta1", "p17/e1"))
        self.assertIn(Reason.NAMESPACE_MISMATCH, reasons_of(proposal))


class ConcurrencyTokens(unittest.TestCase):
    """Concurrency control is mandatory, not something a proposer may omit."""

    @staticmethod
    def _status_op() -> SetField:
        return SetField(
            "FormalState",
            "p17/fs1",
            "status",
            FormalStateStatus.EXPANDED,
            prior=FormalStateStatus.OPEN,
        )

    def test_omitting_the_base_revision_is_rejected(self):
        proposal = replace(propose(state("p17/fs2")), base_revision=None)
        self.assertEqual(reasons_of(proposal), [Reason.MISSING_CONCURRENCY_TOKEN])

    def test_a_structural_op_needs_no_lease(self):
        proposal = replace(
            propose(state("p17/fs2")), lease_id=None, fencing_token=None
        )
        self.assertEqual(reasons_of(proposal), [])

    def test_a_status_op_without_the_lease_is_rejected(self):
        proposal = replace(
            propose(self._status_op()), lease_id=None, fencing_token=None
        )
        self.assertIn(Reason.MISSING_CONCURRENCY_TOKEN, reasons_of(proposal))

    def test_half_a_lease_is_still_rejected(self):
        proposal = replace(propose(self._status_op()), fencing_token=None)
        self.assertIn(Reason.MISSING_CONCURRENCY_TOKEN, reasons_of(proposal))

    def test_a_status_op_with_the_lease_passes(self):
        self.assertNotIn(
            Reason.MISSING_CONCURRENCY_TOKEN, reasons_of(propose(self._status_op()))
        )

    def test_the_missing_lease_names_the_op_that_needed_it(self):
        proposal = replace(
            propose(state("p17/fs2"), self._status_op()),
            lease_id=None,
            fencing_token=None,
        )
        findings = [
            f
            for f in validate_proposal(proposal)
            if f.reason is Reason.MISSING_CONCURRENCY_TOKEN
        ]
        self.assertEqual([f.op_index for f in findings], [1])


class Findings(unittest.TestCase):
    def test_all_violations_are_reported_not_just_the_first(self):
        proposal = propose(
            tactic(result="nonsense", subgoals=3),
            state("p22/fs2"),
            requires("p17/ta8", "p22/fs2", 0, "p17/e1"),
        )
        found = set(reasons_of(proposal))
        self.assertIn(Reason.UNKNOWN_STATUS_VALUE, found)
        self.assertIn(Reason.NAMESPACE_MISMATCH, found)
        self.assertIn(Reason.SUBGOAL_COUNT_MISMATCH, found)

    def test_findings_carry_the_offending_op_index(self):
        proposal = propose(state("p17/fs2"), state("p22/fs3"))
        findings = validate_proposal(proposal)
        self.assertEqual([f.op_index for f in findings], [1])
 
if __name__ == "__main__":
    unittest.main()
