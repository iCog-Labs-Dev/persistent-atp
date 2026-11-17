"""String-only Omega bridge. Workers never call Janus or mutate atomspaces."""
from concurrent.futures import ThreadPoolExecutor
import json
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


def reporting_policy():
    return Path(__file__).with_name("coordinator-policy.txt").read_text()


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
            with _lock:
                _errors.append({"transport_status": "error", "error_code": "BRIDGE_WORKER_FAILED",
                                "request_id": identity, "message": "Work requires reconciliation; no mathematical result is inferred."})
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
    adapter.stage_report(f"admission:{identity}:{result.get('phase', result.get('error_code', 'status'))}", result)
    if adapter.state.intent(identity).get("ref"):
        _observe(adapter, identity)


def startup(workspace):
    """Schedule capability negotiation and recovery without blocking the loop."""
    global _enabled
    adapter = _get_adapter()
    if workspace != adapter.workspace:
        return json.dumps({"transport_status": "error", "error_code": "UNREGISTERED_WORKSPACE"})
    _enabled = True
    def recover():
        adapter.startup()
        for row in adapter.state.snapshot()["intents"].values():
            if row["state"] in {"reserved", "prepared", "uncertain"} and row["arguments"] is not None:
                _submit(row["id"], lambda identity=row["id"]: _execute(adapter, identity))
            elif row["state"] == "accepted" and row.get("ref"):
                _submit(row["id"], lambda identity=row["id"]: _observe(adapter, identity))
    _submit("startup", recover)
    return json.dumps({"phase": "starting", "durable_admission": False})


def _invoke(kind, workspace, **arguments):
    try:
        if not _enabled:
            raise ValueError("PLUGIN_DISABLED")
        if not all(isinstance(value, str) for value in (workspace, *arguments.values())):
            raise ValueError("tool arguments must be strings")
        adapter = _get_adapter()
        identity = adapter.reserve(kind, workspace, arguments)
        scheduled = _submit(identity, lambda: _execute(adapter, identity))
        return json.dumps({"phase": "dispatching" if scheduled else "waiting-on-resource",
            "transport_status": "accepted" if scheduled else "busy", "durable_admission": False,
            "request_id": identity,
            "message": "Bridge intent checkpointed; receiver admission and results arrive separately."})
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc) in {"TOKEN_MISSING", "PLUGIN_DISABLED", "UNREGISTERED_WORKSPACE", "INVALID_BUDGET", "CYCLE_LIMIT_REACHED"} else "INVALID_REQUEST"
        return json.dumps({"transport_status": "error", "error_code": code})


def _render(report):
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if len(raw) > 16000:
        keep = {key: report[key] for key in ("notification_id", "request_id", "operation_ref", "operation_id",
                    "project_revision", "revision", "proof_id", "journal_hash", "event_hash") if key in report}
        raw = json.dumps({**keep, "rendering_incomplete": True,
            "message": "Detailed output omitted. Retrieve the complete operation/artifacts through the authorized SDK; this is not a complete goal/dependency list."}, ensure_ascii=False)
    return "PERSISTENT_ATP_RESULT " + raw


def take_completed():
    """Called by the loop thread; reports stay durable until history append ACK."""
    global _last_poll
    if not _enabled:
        return ""
    adapter = _get_adapter()
    now = time.monotonic()
    if now - _last_poll >= 0.25:
        _last_poll = now
        for row in adapter.active_intents():
            _submit(row["id"], lambda identity=row["id"]: _observe(adapter, identity))
        selected = adapter.state.snapshot().get("selected_proof")
        if selected:
            def refresh():
                status = adapter.status(selected)
                adapter.stage_report(f"status:{selected}:{status['revision']}", status)
            _submit(f"status:{selected}", refresh)
    with _lock:
        errors = _errors[:]
        _errors.clear()
    return "\n".join(_render(report) for report in adapter.pending_reports() + errors)


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
