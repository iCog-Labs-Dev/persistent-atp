"""Validate v2 run accounting and guarded transitions to another attempt."""

from copy import deepcopy

from .ops import SetField, UpsertNode
from .reasons import Reason, Rejection
from .state import JournalOverlayView
from .transitions import RETRYABLE_RUN_DISPOSITIONS

ACTIVE_PROVIDER_STATUSES = frozenset({"admitting", "queued", "running", "cancelling"})


def attempt_outcome(status, provider):
    """The immutable summary retained when one provider attempt ends."""
    return {
        "attempt": provider["attempts"], "disposition": status,
        "termination": provider["termination"],
        **{key: provider["budget"][key] for key in (
            "consumed_steps", "consumed_steps_exact", "remaining_steps",
        )},
    }


def _check_provider(run_id, provider, environment_hash, status=None):
    from mathproof.formal_v2 import validate

    if not isinstance(provider, dict) or provider.get("protocol_version") != 2:
        raise ValueError("run needs its v2 provider metadata")
    snapshot = {k: v for k, v in provider.items() if k != "frontier_resume"}
    # Admission responses omit fields added by the first status response.
    for key, default in (("disposition", None), ("termination", None), ("cancel_requested", False)):
        snapshot.setdefault(key, default)
    snapshot.update(result=None, replays=[])
    errors = validate("formal-run", snapshot)
    if errors:
        raise ValueError(f"invalid provider metadata: {errors[:3]}")
    if provider.get("frontier_resume") is not False or provider.get("retry_from") != "bound-initial-input":
        raise ValueError("retry must use bound initial input without claiming frontier continuation")
    if provider["run_id"] != run_id or provider["resolved"]["environment_hash"] != environment_hash:
        raise ValueError("provider run or environment differs from the graph record")
    if type(provider["attempts"]) is not int or provider["attempts"] < 1:
        raise ValueError("attempt counter must be a positive integer")
    budget = provider["budget"]
    used, cap, requested = (budget[k] for k in ("consumed_steps", "admitted_max_steps", "requested_max_steps"))
    if not (0 <= used <= cap <= requested and cap > 0 and budget["remaining_steps"] == cap-used):
        raise ValueError("invalid cumulative tactic budget accounting")
    if budget["admitted_wall_clock_ms"] <= 0 or budget["verification_max_steps"] <= 0:
        raise ValueError("run allowances must be finite and positive")
    if budget["max_steps_reduced"] != (cap < requested):
        raise ValueError("budget reduction flag differs from the admitted allowance")
    if budget["consumed_steps_exact"] is False and budget["remaining_steps"] != 0:
        raise ValueError("an unknown tactic cost must consume the attempt's whole reservation")
    active = provider["status"] in ACTIVE_PROVIDER_STATUSES
    if active and (provider.get("disposition") not in {None, "searching"} or provider.get("termination") is not None):
        raise ValueError("active provider attempt cannot retain a terminal outcome")
    expected_status = "searching" if active else provider.get("disposition")
    if not active and expected_status in {None, "searching"}:
        raise ValueError("finished provider attempt must have a terminal disposition")
    if status is not None and status != expected_status:
        raise ValueError("graph status differs from the provider attempt disposition")
    if provider.get("termination") == "step-budget" and budget["remaining_steps"] != 0:
        raise ValueError("a step-exhausted attempt cannot retain tactic allowance")
    if provider.get("input_artifact_hash"):
        origin = {"source": "caller-artifact", "goal": "generated-from-text"}[provider["input_kind"]]
        if not provider.get("source_artifact_hash") or provider.get("source_origin") != origin:
            raise ValueError("admitted input must retain its source and source origin")


def _check_continuity(before, after):
    for key in ("run_id", "request_digest", "input_kind", "resolved", "retry_from"):
        if before.get(key) != after.get(key):
            raise ValueError(f"run {key} changed after admission")
    for key in ("input_artifact_hash", "source_artifact_hash", "source_origin"):
        if before.get(key) is not None and before[key] != after.get(key):
            raise ValueError(f"run {key} changed after input admission")
    old, new = before["budget"], after["budget"]
    for key in old.keys() | new.keys():
        if key not in {"consumed_steps", "remaining_steps", "consumed_steps_exact"} and old.get(key) != new.get(key):
            raise ValueError(f"run budget {key} changed after admission")
    if new["consumed_steps"] < old["consumed_steps"]:
        raise ValueError("cumulative tactic budget cannot be refunded")
    if old["consumed_steps_exact"] is False and new["consumed_steps_exact"] is not False:
        raise ValueError("an unknown tactic cost cannot become exact again")
    if not before["attempts"] <= after["attempts"] <= before["attempts"] + 1:
        raise ValueError("attempt counter moved backwards or skipped an attempt")


def check_formal_run_lifecycle(proposal, view):
    effective = JournalOverlayView(view, proposal.base_revision or 0, proposal.ops)
    run_ids = {op.node_id for op in proposal.ops
               if isinstance(op, (UpsertNode, SetField)) and op.label == "FormalRun"}
    for run_id in sorted(run_ids):
        before_node, after_node = view.node(run_id), effective.node(run_id)
        before = before_node.fields if before_node else {}
        after = after_node.fields if after_node else {}
        old, new = before.get("provider"), after.get("provider")
        reopening = before.get("status") not in {None, "searching"} and after.get("status") == "searching"
        if old is None and new is None and not reopening:
            continue  # Existing non-v2 run producers retain their lifecycle.
        try:
            _check_provider(run_id, new, after.get("environment_hash"), after.get("status"))
            if old is not None:
                _check_provider(run_id, old, before.get("environment_hash"), before.get("status"))

            # Check intermediate metadata writes too. A refund or history wipe
            # cannot be hidden by restoring the original value later in the op list.
            previous_provider = old
            previous_history = before.get("attempt_history", [])
            status_at_op = before.get("status")
            reopenings = 0
            for op in proposal.ops:
                if not isinstance(op, (UpsertNode, SetField)) or op.node_id != run_id:
                    continue
                writes = op.fields if isinstance(op, UpsertNode) else {op.field: op.value}
                if "status" in writes:
                    if status_at_op not in {None, "searching"} and writes["status"] == "searching":
                        reopenings += 1
                    status_at_op = writes["status"]
                if "provider" in writes:
                    candidate = writes["provider"]
                    _check_provider(run_id, candidate, after.get("environment_hash"))
                    if previous_provider is not None:
                        _check_continuity(previous_provider, candidate)
                    previous_provider = candidate
                if "attempt_history" in writes:
                    candidate = writes["attempt_history"]
                    if not isinstance(candidate, list) or candidate[:len(previous_history)] != previous_history:
                        raise ValueError("attempt history is append-only")
                    previous_history = candidate

            if reopenings and (old is None or new["attempts"] != old["attempts"] + 1 or reopenings != 1):
                raise ValueError("each reopening must record exactly one new provider attempt")

            if old is None:
                if new["attempts"] != 1 or reopening:
                    raise ValueError("retry needs the previous attempt's admitted accounting")
            else:
                _check_continuity(old, new)
                is_retry = new["attempts"] == old["attempts"] + 1
                if is_retry:
                    if reopenings != 1:
                        raise ValueError("a new attempt must record its guarded transition through searching")
                    if (before.get("status") not in RETRYABLE_RUN_DISPOSITIONS
                            or old["status"] not in {"finished", "interrupted"}
                            or old["budget"]["remaining_steps"] <= 0):
                        raise ValueError("prior attempt is active, final, or has exhausted its tactic allowance")
                    if before["status"] in {"invalid-request", "environment-error"} and old.get("input_artifact_hash") is None:
                        raise ValueError("rejected input cannot be retried")
                    if view.edges_from(run_id, "PRODUCED_CERTIFICATE"):
                        raise ValueError("a run with a candidate certificate cannot be retried")
                elif reopening or (before.get("status") != "searching" and new["status"] in ACTIVE_PROVIDER_STATUSES):
                    raise ValueError("retry must increment the attempt counter exactly once")

            history = deepcopy(before.get("attempt_history", []))

            def retain(status, provider):
                if status != "searching":
                    outcome = attempt_outcome(status, provider)
                    recorded = next((h for h in history if h["attempt"] == outcome["attempt"]), None)
                    if recorded is None:
                        history.append(outcome)
                    elif recorded != outcome:
                        raise ValueError("a recorded attempt outcome cannot be replaced")

            if old is not None:
                retain(before["status"], old)
            retain(after["status"], new)
            if after.get("attempt_history") != history:
                raise ValueError("run must preserve previous outcomes and record the finished attempt")
        except (KeyError, TypeError, ValueError) as exc:
            yield Rejection(Reason.FORMAL_RUN_LIFECYCLE_INVALID, f"FormalRun {run_id!r}: {exc}")
