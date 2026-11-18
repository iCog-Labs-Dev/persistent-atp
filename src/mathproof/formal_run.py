"""Translate v2 worker results into durable run/attempt graph updates."""

from copy import deepcopy

from commit_gate.formal_runs import attempt_outcome
from commit_gate.ops import SetField, UpsertNode
from commit_gate.state import NodeRecord


def formal_run_ops(result, current: NodeRecord | None = None, *, actor: str):
    """Build a proposal's run ops; the commit gate validates their authority.

    Persist the adapter's provider metadata rather than reconstructing an
    allowance from the original request. Past outcomes are append-only.
    Obstructions, search graph records, certificates and leases remain the
    caller's responsibility.
    """
    run_id = result["run_id"]
    status = result["disposition"]
    provider = deepcopy(result["provider"])
    if current is not None and (current.label != "FormalRun" or current.node_id != run_id):
        raise ValueError("current record must be the same FormalRun")
    before = dict(current.fields) if current else {}
    history = deepcopy(before.get("attempt_history", []))

    def retain(outcome_status, snapshot):
        if outcome_status != "searching":
            outcome = attempt_outcome(outcome_status, snapshot)
            previous = next((h for h in history if h["attempt"] == outcome["attempt"]), None)
            if previous is None:
                history.append(outcome)
            elif previous != outcome:
                raise ValueError("cannot replace a recorded attempt outcome")

    if current and before.get("provider"):
        retain(before["status"], before["provider"])
    retain(status, provider)
    if current is None:
        return (UpsertNode("FormalRun", run_id, {
            "actor": actor, "status": status,
            "environment_hash": result["environment_hash"],
            "provider": provider, "attempt_history": history,
        }),)

    ops = []
    old_status = before["status"]
    previous_provider = before.get("provider") or {}
    retry = provider["attempts"] > previous_provider.get("attempts", provider["attempts"])
    # A retry can finish before its HTTP response arrives. Record both status
    # steps in that one proposal rather than overwriting attempt 1's outcome.
    if retry:
        ops.append(SetField("FormalRun", run_id, "status", "searching", prior=old_status))
        old_status = "searching"
    if status != old_status:
        ops.append(SetField("FormalRun", run_id, "status", status, prior=old_status))
    for field, value in (("provider", provider), ("attempt_history", history)):
        if value != before.get(field):
            ops.append(SetField("FormalRun", run_id, field, value, prior=before.get(field)))
    return tuple(ops)
