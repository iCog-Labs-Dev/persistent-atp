"""Same-run retry attacks through the leased, journal-backed commit gate."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.formal_run import formal_run_ops
from mathproof.tests.test_maths_ai_http import example, setup_adapter


class RunCase:
    run_id = "example/fr-3"

    def __init__(self, root):
        self.path = str(root / "journal.sqlite")
        self.journal = JournalStore(self.path)
        self.token = self.journal.acquire_lease(
            "example", "lease", actor="worker", worker_class="formal-atp",
        )
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.journal)

    def snapshot(self, *, attempt=1, used=0, disposition=None, admitted=True):
        provider = deepcopy(example("11-status-stagnated")["response"]["body"])
        provider.pop("result")
        provider.pop("replays")
        provider.update(attempts=attempt, disposition=disposition,
                        status="running" if disposition is None else "finished",
                        termination=None if disposition is None else {
                            "stagnated": "frontier-exhausted", "budget-exhausted": "step-budget",
                            "proved-pending-replay": "certified", "counterexample": "search-closed",
                            "invalid-request": "invalid-input", "environment-error": "environment-mismatch",
                            "internal-error": "crash", "cancelled": "cancelled",
                        }[disposition], frontier_resume=False)
        provider["budget"].update(consumed_steps=used, remaining_steps=60-used)
        if disposition == "budget-exhausted" and used < 60:
            provider["termination"] = "node-budget"
        if not admitted:
            for key in ("input_artifact_hash", "source_artifact_hash", "source_origin"):
                provider[key] = None
        return {"run_id": self.run_id, "disposition": disposition or "searching",
                "environment_hash": provider["resolved"]["environment_hash"], "provider": provider}

    def ops(self, result):
        return list(formal_run_ops(result, self.view.node(self.run_id), actor="worker"))

    def proposal(self, ops):
        return Proposal(
            proof_id="example", actor="worker", worker_class="formal-atp", ops=tuple(ops),
            base_revision=self.journal.head("example")[0], lease_id="lease", fencing_token=self.token,
        )

    def project(self):
        while self.view.snapshot("example")[0] < self.journal.head("example")[0]:
            self.view.apply_journal_event(self.journal, "example", self.view.snapshot("example")[0]+1)

    def accept(self, ops):
        result = self.gate.commit(self.proposal(ops))
        assert result.accepted, result.rejections
        self.project()

    def start(self):
        self.accept(self.ops(self.snapshot()))

    def stop(self, disposition="stagnated", *, used=40, admitted=True):
        ops = self.ops(self.snapshot(disposition=disposition, used=used, admitted=admitted))
        if disposition == "stagnated":
            ops += [UpsertNode("Obstruction", "example/o-1", {"kind": "search-policy"}),
                    AddEdge("RAISED_OBSTRUCTION", self.run_id, "example/o-1", "example/obstruction")]
        self.accept(ops)

    def restart(self):
        self.journal.close()
        self.journal = JournalStore(self.path)
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.journal)
        self.project()

    def assert_rejected(self, ops=None, *, proposal=None, reason="formal-run-lifecycle-invalid"):
        before = (self.view.list_nodes("example"), self.view.list_edges("example"))
        head = self.journal.head("example")
        result = self.gate.commit(proposal or self.proposal(ops))
        assert not result.accepted, result
        assert reason in {str(r.reason) for r in result.rejections}, result.rejections
        assert self.journal.head("example") == head
        assert (self.view.list_nodes("example"), self.view.list_edges("example")) == before


@pytest.fixture
def case(tmp_path):
    value = RunCase(tmp_path)
    yield value
    value.journal.close()


def test_first_outcome_retry_second_outcome_and_restart_preserve_budget_and_history(case):
    case.start()
    case.stop()
    case.restart()
    case.accept(case.ops(case.snapshot(attempt=2, used=40)))
    run = case.view.node(case.run_id).fields
    assert run["status"] == "searching"
    assert run["provider"]["budget"]["remaining_steps"] == 20
    assert run["provider"]["frontier_resume"] is False
    assert run["attempt_history"] == [{
        "attempt": 1, "disposition": "stagnated", "termination": "frontier-exhausted",
        "consumed_steps": 40, "consumed_steps_exact": True, "remaining_steps": 20,
    }]
    case.accept(case.ops(case.snapshot(attempt=2, used=60, disposition="budget-exhausted")))
    case.restart()
    run = case.view.node(case.run_id).fields
    assert run["status"] == "budget-exhausted"
    assert run["provider"]["budget"]["consumed_steps"] == 60
    assert run["provider"]["budget"]["remaining_steps"] == 0
    assert [h["disposition"] for h in run["attempt_history"]] == ["stagnated", "budget-exhausted"]
    assert [h["consumed_steps"] for h in run["attempt_history"]] == [40, 60]


@pytest.mark.parametrize("attack", [
    "refund", "allowance-increase", "requested-increase", "verification-increase", "wall-clock-increase",
    "negative-cost", "fractional-cost", "boolean-cost", "wrong-remainder", "attempt-not-incremented",
    "attempt-skipped", "changed-run", "changed-digest", "changed-input", "changed-source",
    "changed-origin", "changed-profile", "changed-environment", "changed-backend", "frontier-claim",
    "checkpoint-retry", "unknown-field", "missing-provider",
    "unknown-cost-credit", "retained-outcome", "retained-termination",
])
def test_retry_cannot_reset_or_replace_its_durable_binding(case, attack):
    case.start()
    case.stop()
    result = case.snapshot(attempt=2, used=40)
    provider = result["provider"]
    budget = provider["budget"]
    if attack == "refund": budget.update(consumed_steps=0, remaining_steps=60)
    elif attack == "allowance-increase": budget.update(admitted_max_steps=100, remaining_steps=60, max_steps_reduced=False)
    elif attack == "requested-increase": budget["requested_max_steps"] = 200
    elif attack == "verification-increase": budget["verification_max_steps"] += 100
    elif attack == "wall-clock-increase": budget["admitted_wall_clock_ms"] += 1000
    elif attack == "negative-cost": budget.update(consumed_steps=-1, remaining_steps=61)
    elif attack == "fractional-cost": budget.update(consumed_steps=40.5, remaining_steps=19.5)
    elif attack == "boolean-cost": budget.update(consumed_steps=True, remaining_steps=59)
    elif attack == "wrong-remainder": budget["remaining_steps"] = 60
    elif attack == "attempt-not-incremented": provider["attempts"] = 1
    elif attack == "attempt-skipped": provider["attempts"] = 3
    elif attack == "changed-run": provider["run_id"] = "example/fr-99"
    elif attack == "changed-digest": provider["request_digest"] = "sha256:" + "ab"*32
    elif attack == "changed-input": provider["input_artifact_hash"] = "sha256:" + "ab"*32
    elif attack == "changed-source": provider["source_artifact_hash"] = "sha256:" + "ab"*32
    elif attack == "changed-origin": provider["source_origin"] = "caller-artifact"
    elif attack == "changed-profile": provider["resolved"]["profile_config_hash"] = "sha256:" + "ab"*32
    elif attack == "changed-environment": provider["resolved"]["environment_hash"] = "sha256:" + "ab"*32
    elif attack == "changed-backend": provider["resolved"]["backend"] = "accepted-gnn"
    elif attack == "frontier-claim": provider["frontier_resume"] = True
    elif attack == "checkpoint-retry": provider["retry_from"] = "saved-frontier"
    elif attack == "unknown-field": provider["replenish_budget"] = True
    elif attack == "unknown-cost-credit": budget["consumed_steps_exact"] = False
    elif attack == "retained-outcome": provider["disposition"] = "stagnated"
    elif attack == "retained-termination": provider["termination"] = "frontier-exhausted"
    ops = case.ops(result)
    if attack == "missing-provider":
        ops = [op for op in ops if not isinstance(op, SetField) or op.field != "provider"]
    case.assert_rejected(ops)


@pytest.mark.parametrize("attack", ["drop", "rewrite", "temporary-drop", "temporary-refund"])
def test_atomic_retry_cannot_hide_history_or_accounting_rewrites(case, attack):
    case.start()
    case.stop()
    ops = case.ops(case.snapshot(attempt=2, used=40))
    history = deepcopy(case.view.node(case.run_id).fields["attempt_history"])
    if attack in {"drop", "rewrite", "temporary-drop"}:
        replacement = [] if attack != "rewrite" else [{**history[0], "consumed_steps": 0}]
        ops.append(SetField("FormalRun", case.run_id, "attempt_history", replacement, prior=history))
        if attack == "temporary-drop":
            ops.append(SetField("FormalRun", case.run_id, "attempt_history", history, prior=[]))
    else:
        provider_op = next(op for op in ops if isinstance(op, SetField) and op.field == "provider")
        refund = deepcopy(provider_op.value)
        refund["budget"].update(consumed_steps=0, remaining_steps=60)
        index = ops.index(provider_op)
        ops[index:index+1] = [replace(provider_op, value=refund), replace(provider_op, prior=refund)]
    case.assert_rejected(ops)


@pytest.mark.parametrize("disposition,used,admitted", [
    ("proved-pending-replay", 40, True), ("counterexample", 40, True),
    ("budget-exhausted", 60, True), ("cancelled", 60, True),
    ("invalid-request", 0, False), ("environment-error", 0, False),
])
def test_ineligible_terminal_run_stays_closed(case, disposition, used, admitted):
    # Unadmitted inputs remain unadmitted throughout this run.
    case.accept(case.ops(case.snapshot(admitted=admitted)))
    case.stop(disposition, used=used, admitted=admitted)
    case.assert_rejected(case.ops(case.snapshot(attempt=2, used=used, admitted=admitted)))


@pytest.mark.parametrize("disposition", [
    "stagnated", "budget-exhausted", "internal-error", "cancelled", "invalid-request", "environment-error",
])
def test_retryable_outcomes_require_remaining_allowance_and_admitted_context(case, disposition):
    case.start()
    case.stop(disposition)
    case.accept(case.ops(case.snapshot(attempt=2, used=40)))
    assert case.view.node(case.run_id).fields["provider"]["budget"]["remaining_steps"] == 20


def test_retry_that_finishes_before_http_reply_retains_both_attempt_outcomes(case):
    case.start()
    case.stop()
    case.accept(case.ops(case.snapshot(attempt=2, used=60, disposition="budget-exhausted")))
    case.restart()
    assert [h["attempt"] for h in case.view.node(case.run_id).fields["attempt_history"]] == [1, 2]


def test_duplicate_retry_proposal_cannot_commit_twice(case):
    case.start()
    case.stop()
    proposal = case.proposal(case.ops(case.snapshot(attempt=2, used=40)))
    assert case.gate.commit(proposal).accepted
    case.project()
    case.assert_rejected(proposal=proposal, reason=Reason.STALE_BASE_REVISION)


def test_retry_cannot_launch_a_new_attempt_while_run_is_active(case):
    case.start()
    case.assert_rejected(case.ops(case.snapshot(attempt=2)))


def test_legacy_terminal_status_cannot_reopen_without_v2_accounting(case):
    case.accept([UpsertNode("FormalRun", case.run_id, {"status": "stagnated"})])
    case.assert_rejected([SetField("FormalRun", case.run_id, "status", "searching", prior="stagnated")])


def test_retry_still_requires_current_lease_after_journal_restart(case):
    case.start()
    case.stop()
    case.restart()
    proposal = case.proposal(case.ops(case.snapshot(attempt=2, used=40)))
    case.journal.release_lease("example", "lease", fencing_token=case.token)
    case.token = case.journal.acquire_lease("example", "lease", actor="worker", worker_class="formal-atp")
    case.assert_rejected(proposal=proposal, reason=Reason.FENCING_TOKEN_SUPERSEDED)


@pytest.mark.parametrize("cycles", [1, 2])
def test_status_cycles_cannot_fake_a_retry_without_new_attempt_receipt(case, cycles):
    case.start()
    case.stop()
    ops = []
    for _ in range(cycles):
        ops += [SetField("FormalRun", case.run_id, "status", "searching", prior="stagnated"),
                SetField("FormalRun", case.run_id, "status", "stagnated", prior="searching")]
    case.assert_rejected(ops)


def test_active_run_cannot_refund_cost_without_using_retry_endpoint(case):
    case.accept(case.ops(case.snapshot(used=20)))
    case.assert_rejected(case.ops(case.snapshot(used=0)))


def test_stale_base_revision_cannot_launch_retry(case):
    case.start()
    case.stop()
    proposal = case.proposal(case.ops(case.snapshot(attempt=2, used=40)))
    case.accept([UpsertNode("Obstruction", "example/o-concurrent", {"kind": "resource"})])
    case.assert_rejected(proposal=proposal, reason=Reason.STALE_BASE_REVISION)


def test_unprojected_journal_cannot_hide_previous_consumption(case):
    case.start()
    case.stop()
    result = case.snapshot(attempt=2, used=0)
    ops = case.ops(result)
    case.view = MemoryView()  # The gate must recover authoritative prior state itself.
    case.gate = CommitGate(case.view, case.journal)
    case.assert_rejected(ops)


def test_step_budget_exhaustion_cannot_leave_free_retry_credit(case):
    case.start()
    result = case.snapshot(disposition="budget-exhausted", used=40)
    result["provider"]["termination"] = "step-budget"
    case.assert_rejected(case.ops(result))


def test_existing_candidate_certificate_prevents_another_attempt(case):
    case.start()
    case.stop()
    # Model a historical inconsistent run that already produced a candidate.
    case.journal.append(case.proposal([
        UpsertNode("Certificate", "example/cert-1", {"status": "candidate"}),
        AddEdge("PRODUCED_CERTIFICATE", case.run_id, "example/cert-1", "example/produced"),
    ]).to_dict())
    case.project()
    case.assert_rejected(case.ops(case.snapshot(attempt=2, used=40)))


def test_adapter_retry_results_commit_without_bypassing_artifact_or_wire_validation(case):
    atp, request, admission, first, remote, _, transport = setup_adapter("goal")
    case.run_id = request["run_id"]
    # Match the real recorded admission: input resolution finishes later.
    admission.update(input_artifact_hash=None, source_artifact_hash=None, source_origin=None)
    first.update(disposition="stagnated", termination="frontier-exhausted")
    first["budget"].update(consumed_steps=40, remaining_steps=20)
    result = first["result"]
    result.update(disposition="stagnated", termination={"reason": "frontier-exhausted"}, certificate=None)
    root = next(s for s in result["states"] if s["state_id"] == result["root_state_id"])
    root["status"] = "open"
    result.update(states=[root], tactic_edges=[], obstructions=[])
    result["artifacts"] = [a for a in result["artifacts"] if a["role"] != "certificate"]
    result["budget"].update(remaining_steps=20)
    result["budget_used"]["tactic_steps"] = 40
    result["budget_used"]["attempt"].update(tactic_steps=40)
    second = deepcopy(first)
    second.update(status="admitting", disposition=None, termination=None, attempts=2, result=None)
    final = deepcopy(first)
    final.update(disposition="budget-exhausted", termination="step-budget", attempts=2)
    final["budget"].update(consumed_steps=60, remaining_steps=0)
    result = final["result"]
    result.update(disposition="budget-exhausted", termination={"reason": "step-budget"}, attempts=2)
    result["budget"].update(remaining_steps=0)
    result["budget_used"]["tactic_steps"] = 60
    result["budget_used"]["attempt"].update(attempt=2, tactic_steps=20, reserved_steps=20)
    result["root_state_id"] = result["root_state_id"].replace("/attempt-1/", "/attempt-2/")
    result["states"][0]["state_id"] = result["root_state_id"]
    phase = 1

    def call(method, path, payload=None, **kwargs):
        nonlocal phase
        if path == "/formal-capabilities":
            return example("01-capabilities")["response"]["body"]
        if path == "/formal-search/start":
            assert payload == request
            return deepcopy(admission)
        if method == "POST" and path.endswith("/retry"):
            assert payload == {}  # No replacement input or replenished budget.
            phase = 2
            return deepcopy(second)
        return deepcopy(first if phase == 1 else final)

    with patch.object(atp, "_call", side_effect=call), patch.object(atp, "_transport", side_effect=transport):
        case.accept(case.ops(atp.formal_search_start(request)))
        stopped = atp.formal_search_status(request["run_id"])
        case.accept(case.ops(stopped) + [
            UpsertNode("Obstruction", "example/o-1", {"kind": "search-policy"}),
            AddEdge("RAISED_OBSTRUCTION", case.run_id, "example/o-1", "example/obstruction"),
        ])
        case.accept(case.ops(atp.formal_search_resume(request["run_id"])))
        case.accept(case.ops(atp.formal_search_status(request["run_id"])))
    case.restart()
    fields = case.view.node(case.run_id).fields
    assert [h["consumed_steps"] for h in fields["attempt_history"]] == [40, 60]
    assert fields["provider"]["budget"]["remaining_steps"] == 0
    assert fields["provider"]["input_artifact_hash"] in atp.artifacts.blobs
