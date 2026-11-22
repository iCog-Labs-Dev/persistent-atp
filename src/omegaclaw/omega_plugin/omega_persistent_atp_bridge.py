"""String-only Omega bridge. Workers never call Janus or mutate atomspaces."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
from pathlib import Path

from omegaclaw.frameworks.omega import OmegaAdapter

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="omega-atp")
_slots = threading.BoundedSemaphore(32)
_inflight = set()
_adapter = None
_enabled = False
_last_poll = 0.0
_errors = []
_dispatch_offset = 0
_observe_offset = 0
_task_offset = 0


def reporting_policy():
    workspace = json.dumps(os.environ.get('PERSISTENT_ATP_OMEGA_WORKSPACE', 'reference'))
    return (f'The configured persistent-ATP workspace argument is exactly {workspace}. '
        'Use this exact value for every proof tool. The Omega agent name is not a workspace. '
        'An immediate transport_status=error is a rejected bridge call: report its error_code; '
        'no asynchronous result will arrive for that call. Do not search memory for an admission.\n\n'
        + Path(__file__).with_name("coordinator-policy.txt").read_text())


def disable():
    global _enabled
    _enabled = False
    return True


def configure_adapter(adapter, *, enabled=True):
    """Trusted host/test injection. This function is not a model skill."""
    global _adapter, _enabled
    with _lock:
        if _inflight:
            raise RuntimeError("cannot replace adapter during active work")
        _adapter, _enabled = adapter, enabled


def _get_adapter():
    global _adapter
    with _lock:
        if _adapter is None:
            _adapter = OmegaAdapter.from_environment()
        return _adapter


def _submit(identity, function):
    with _lock:
        if identity in _inflight:
            return True
        if not _slots.acquire(blocking=False):
            return False
        _inflight.add(identity)
    def run():
        try:
            function()
        except Exception:
            error = {"transport_status": "error", "error_code": "BRIDGE_WORKER_FAILED",
                     "request_id": identity, "message": "Work requires reconciliation; no mathematical result is inferred."}
            adapter = _adapter
            if adapter is not None:
                rows = adapter.state.snapshot()["intents"]
                if identity in rows:
                    error["operation_ref"] = rows[identity].get("ref")
                    values = {"observation_suspended": True} if error["operation_ref"] else {"state": "needs-input"}
                    adapter.state.patch(identity, last_error=error, **values)
                adapter.stage_report(f"worker:{identity}", error)
            else:
                with _lock:
                    _errors.append(error)
        finally:
            with _lock:
                _inflight.discard(identity)
                _slots.release()
    try:
        _pool.submit(run)
    except BaseException:
        with _lock:
            _inflight.discard(identity)
            _slots.release()
        raise
    return True


def _observe(adapter, identity):
    for report in adapter.observe(identity):
        if "notification_id" not in report:
            adapter.stage_report(f"observation:{identity}:{report.get('error_code', 'error')}", report)


def _execute(adapter, identity):
    result = adapter.execute(identity)
    suffix = ":exhausted" if result.get("retry_exhausted") else ""
    adapter.stage_report(f"admission:{identity}:{result.get('phase', result.get('error_code', 'status'))}{suffix}", result)
    if adapter.state.intent(identity).get("ref"):
        _observe(adapter, identity)


def startup(workspace):
    """Schedule capability negotiation and recovery without blocking the loop."""
    global _enabled
    adapter = _get_adapter()
    if workspace != adapter.workspace:
        return json.dumps({"transport_status": "error", "error_code": "UNREGISTERED_WORKSPACE"})
    _enabled = True
    _submit("startup", lambda: adapter.bootstrap())
    return json.dumps({"phase": "starting", "durable_admission": False})


def _invoke(kind, workspace, **arguments):
    try:
        if not _enabled:
            raise ValueError("PLUGIN_DISABLED")
        if not all(isinstance(value, str) for value in (workspace, *arguments.values())):
            raise ValueError("tool arguments must be strings")
        adapter = _get_adapter()
        identity = adapter.reserve(kind, workspace, arguments)
        row = adapter.state.intent(identity)
        if row["state"] in {"rejected", "needs-input"} or row.get("retry_exhausted"):
            return json.dumps(row["last_error"])
        ready = row.get("next_attempt_at", 0) <= adapter._clock()
        scheduled = ready and _submit(identity, lambda: _execute(adapter, identity))
        phase = "dispatching" if scheduled else "waiting-on-resource" if ready else "waiting-on-retry"
        return json.dumps({"phase": phase,
            "transport_status": "accepted" if scheduled else "busy", "durable_admission": False,
            "request_id": identity,
            "message": "Bridge intent checkpointed; receiver admission and results arrive separately."})
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc) in {"TOKEN_MISSING", "PLUGIN_DISABLED", "UNREGISTERED_WORKSPACE", "INVALID_BUDGET", "CYCLE_LIMIT_REACHED", "TASK_OWNED_BY_COORDINATOR"} else "INVALID_REQUEST"
        detail = {"expected_budget_fields": ["max_steps", "wall_clock_ms"]} if code == "INVALID_BUDGET" else {}
        if code == "TASK_OWNED_BY_COORDINATOR":
            detail["message"] = ("Use a new bounded managed task to continue this proof, with proof_id, "
                "expected_revision, expected_theorem_hash, max_cycles and max_research. "
                "Status, audit and export remain available.")
        return json.dumps({"transport_status": "error", "error_code": code, **detail})


def _render(report):
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if len(raw) > 16000:
        keep = {key: report[key] for key in ("notification_id", "request_id", "operation_ref", "operation_id",
                    "project_revision", "revision", "proof_id", "journal_hash", "event_hash", "lifecycle",
                    "mathematical_disposition", "execution_phase", "error_code", "event_history_complete",
                    "events_pending", "effective_budget") if key in report}
        raw = json.dumps({**keep, "rendering_incomplete": True,
            "message": "Detailed output omitted. Retrieve the complete operation/artifacts through the authorized SDK; this is not a complete goal/dependency list."}, ensure_ascii=False)
    return "PERSISTENT_ATP_RESULT " + raw


def take_completed():
    """Called by the loop thread; reports stay durable until history append ACK."""
    global _last_poll, _dispatch_offset, _observe_offset, _task_offset
    if not _enabled:
        return ""
    adapter = _get_adapter()
    now = time.monotonic()
    if now - _last_poll >= 0.25:
        _last_poll = now
        if adapter.bootstrap_due():
            _submit("startup", lambda: adapter.bootstrap())
        tasks = adapter.tasks.actionable()
        batch = _rotate(tasks, _task_offset)
        _task_offset += len(batch)
        for identity in batch:
            _submit("task:" + identity, lambda identity=identity: adapter.tasks.tick(identity))
        # Bounded round-robin batches leave capacity for both dispatch and
        # observation, even when either list exceeds the executor's queue.
        pending = [row for row in adapter.dispatchable_intents() if not row.get("task_id")]
        batch = _rotate(pending, _dispatch_offset)
        _dispatch_offset += len(batch)
        for row in batch:
            _submit(row["id"], lambda identity=row["id"]: _execute(adapter, identity))
        # Task reducers observe their own operation until settled; afterwards
        # drain any remaining event pages without launching another operation.
        task_records = adapter.state.snapshot()["tasks"]
        active = []
        for row in adapter.active_intents():
            owner = task_records.get(row.get("task_id"))
            if row.get("task_id") and owner is None:
                adapter.stage_report("missing-task:" + row["id"], {"error_code": "TASK_METADATA_UNAVAILABLE",
                    "request_id": row["id"], "operation_ref": row["ref"],
                    "message": "Task metadata is missing; observing existing work without recreating user intent."})
            if owner is None or owner["phase"] in {"delivery", "done"}:
                active.append(row)
        batch = _rotate(active, _observe_offset)
        _observe_offset += len(batch)
        for row in batch:
            _submit(row["id"], lambda identity=row["id"]: _observe(adapter, identity))
        selected = adapter.state.snapshot().get("selected_proof")
        if selected:
            _submit(f"status:{selected}", lambda: adapter.refresh_status(selected))
        # Channel I/O is bounded and runs off the MeTTa loop thread.
        import sys
        channel = sys.modules.get("persistent_atp_channel")
        if channel is not None:
            _submit("task-delivery", channel.flush_connected)
    with _lock:
        errors = _errors[:]
        _errors.clear()
    return "\n".join(_render(report) for report in adapter.pending_reports() + errors)


def _rotate(rows, offset):
    if not rows:
        return []
    offset %= len(rows)
    return (rows[offset:] + rows[:offset])[:4]


def ack_completed(raw, write_receipt):
    if not isinstance(write_receipt, str) or not write_receipt.startswith("APPEND-VERIFIED"):
        return False
    identities = []
    for line in raw.splitlines():
        if line.startswith("PERSISTENT_ATP_RESULT "):
            record = json.loads(line[len("PERSISTENT_ATP_RESULT "):])
            if record.get("notification_id"):
                identities.append(record["notification_id"])
    _get_adapter().acknowledge(identities)
    return True


def proof_create(spec, workspace): return _invoke("proof-create", workspace, spec=spec)
def proof_cycle(proof, workspace): return _invoke("proof-cycle", workspace, proof=proof)
def proof_status(proof, workspace): return _invoke("proof-status", workspace, proof=proof)
def proof_audit(proof, workspace): return _invoke("proof-audit", workspace, proof=proof)
def proof_export(proof, destination, workspace): return _invoke("proof-export", workspace, proof=proof, destination=destination)
def formal_search(proof, claim, budget, workspace): return _invoke("formal-search", workspace, proof=proof, claim=claim, budget=budget)
def formal_resume(proof, run, budget, workspace): return _invoke("formal-resume", workspace, proof=proof, run=run, budget=budget)
def statement_align(proof, claim, lean_source, workspace): return _invoke("statement-align", workspace, proof=proof, claim=claim, lean_source=lean_source)
def lemma_bridge(proof, obstruction, workspace): return _invoke("lemma-bridge", workspace, proof=proof, obstruction=obstruction)
def hypothesis_formalize(proof, hypothesis, expected_revision, workspace):
    return _invoke("hypothesis-formalize", workspace, proof=proof, hypothesis=hypothesis, expected_revision=expected_revision)

def hypothesis_revise(proof, hypothesis, spec, previous_claim, expected_revision, workspace):
    return _invoke("hypothesis-revise", workspace, proof=proof, hypothesis=hypothesis, spec=spec,
        previous_claim=previous_claim, expected_revision=expected_revision)
