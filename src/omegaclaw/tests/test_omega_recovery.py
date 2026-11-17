"""Workflow scheduling and observation regressions, without external services."""
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

from omegaclaw.frameworks.omega import OmegaAdapter
from persistent_atp_client.errors import Forbidden, HashMismatch, StalePrecondition, TransientUnavailable
from persistent_atp_client.models import ArtifactRef, OperationRef, ProjectDigest
from persistent_atp_client.testing import ScriptedReceiverPort


SPEC = json.dumps({"theorem": "True", "definitions": {}, "assumptions": [],
    "quantifier_domains": {}, "allowed_background": [], "targets": ["literal theorem"],
    "verification_policy": "independent Lean replay"})


@pytest.fixture
def bridge():
    path = Path(__file__).resolve().parents[1] / "omega_plugin/omega_persistent_atp_bridge.py"
    spec = importlib.util.spec_from_file_location("omega_recovery_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.disable()
    module._pool.shutdown(wait=True)


def tick_until(bridge, condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        bridge._last_poll = 0
        bridge.take_completed()
        if condition(): return
        time.sleep(0.01)
    pytest.fail("heartbeat did not make expected progress")


class PagedPort(ScriptedReceiverPort):
    async def read_events(self, ref, cursor=None):
        page, last = await super().read_events(ref, cursor)
        return (page[:1], page[0].cursor) if page else (page, last)


def test_terminal_events_are_drained_across_pages_and_restart(tmp_path):
    port = PagedPort()
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "state.json")
    identity = adapter.reserve("proof-create", "reference", {"spec": SPEC})
    ref = OperationRef(**adapter.execute(identity)["operation_ref"])
    port._event(ref, "central-lemma", 0)
    port.finish(ref)
    adapter.observe(identity)
    assert adapter.state.intent(identity)["state"] == "completed"
    assert adapter.active_intents() and adapter.state.intent(identity)["event_sequence"] == 1
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "state.json")
    for _ in range(3): adapter.observe(identity)
    assert not adapter.active_intents()
    assert adapter.state.intent(identity)["event_sequence"] == 3
    reports = adapter.pending_reports()
    assert [row["event"]["event_type"] for row in reports if "event" in row] == ["queued", "central-lemma", "succeeded"]
    assert any(row.get("event_history_complete") for row in reports)
    adapter.acknowledge([row["notification_id"] for row in reports])
    adapter.observe(identity)
    assert not adapter.pending_reports()


def test_duplicate_only_page_does_not_end_terminal_observation_or_spin():
    port = ScriptedReceiverPort()
    adapter = OmegaAdapter(port, workspace="reference")
    identity = adapter.reserve("proof-create", "reference", {"spec": SPEC})
    ref = OperationRef(**adapter.execute(identity)["operation_ref"])
    port.finish(ref)
    adapter.observe(identity)
    original = port.read_events
    async def duplicate(*args): return await original(ref, None)
    port.read_events = duplicate
    adapter.observe(identity)
    assert adapter.active_intents()
    assert len([row for row in adapter.pending_reports() if "event" in row]) == 2


def test_saturated_work_resumes_on_heartbeat_without_tool_reentry(bridge):
    port = ScriptedReceiverPort()
    adapter = OmegaAdapter(port, workspace="reference")
    bridge.configure_adapter(adapter)
    bridge._slots = threading.BoundedSemaphore(1)
    bridge._slots.acquire()
    receipt = json.loads(bridge.proof_create(SPEC, "reference"))
    assert receipt["phase"] == "waiting-on-resource" and not port._operations
    bridge._slots.release()
    tick_until(bridge, lambda: bool(adapter.state.intent(receipt["request_id"]).get("ref")))
    assert len(port._operations) == 1


def test_uncertain_delivery_recovers_in_same_process_without_resubmission(bridge):
    clock = [100.0]
    class LostReply(ScriptedReceiverPort):
        unavailable = True
        submits = 0
        async def submit(self, request):
            self.submits += 1
            await super().submit(request)
            raise TransientUnavailable()
        async def lookup_request(self, *args):
            if self.unavailable: raise TransientUnavailable()
            return await super().lookup_request(*args)
    port = LostReply()
    adapter = OmegaAdapter(port, workspace="reference", clock=lambda: clock[0])
    identity = adapter.reserve("proof-create", "reference", {"spec": SPEC})
    assert adapter.execute(identity)["error_code"] == "SUBMISSION_UNCERTAIN"
    prepared = adapter.state.intent(identity)["request"]
    assert not adapter.dispatchable_intents()
    port.unavailable = False
    clock[0] += 1
    bridge.configure_adapter(adapter)
    tick_until(bridge, lambda: bool(adapter.state.intent(identity).get("ref")))
    assert port.submits == 1 and len(port._operations) == 1
    assert adapter.state.intent(identity)["request"] == prepared


def test_retry_limit_is_persistent_and_tool_reentry_cannot_bypass_it(bridge, tmp_path):
    clock = [100.0]
    class Offline(ScriptedReceiverPort):
        calls = 0
        async def capabilities(self, **kwargs):
            self.calls += 1
            raise TransientUnavailable()
    port = Offline()
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "state.json",
                           retry_limit=2, clock=lambda: clock[0])
    identity = adapter.reserve("proof-create", "reference", {"spec": SPEC})
    assert adapter.execute(identity)["automatic_retry"]
    adapter.execute(identity)
    assert port.calls == 1  # No early retry before backoff.
    clock[0] += 10
    assert adapter.execute(identity)["retry_exhausted"]
    restored = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "state.json", clock=lambda: clock[0])
    bridge.configure_adapter(restored)
    assert json.loads(bridge.proof_create(SPEC, "reference"))["retry_exhausted"]
    assert not restored.dispatchable_intents() and port.calls == 2
    assert restored.resume_reconciliation(identity) == identity
    assert restored.dispatchable_intents()[0]["id"] == identity


@pytest.mark.parametrize("failure,action", [(Forbidden, "stop"), (StalePrecondition, "refresh-and-review")])
def test_permanent_or_stale_error_preserves_request_and_stops_dispatch(failure, action):
    class Port(ScriptedReceiverPort):
        async def read_status(self, **kwargs):
            return ProjectDigest("reference", "p1", 3, "sha256:" + "a" * 64, 3,
                ArtifactRef("sha256:" + "b" * 64), "True", "provisional", False, "fixture", "scripted-test")
        async def submit(self, request): raise failure()
    adapter = OmegaAdapter(Port(), workspace="reference")
    identity = adapter.reserve("proof-cycle", "reference", {"proof": "p1"})
    result = adapter.execute(identity)
    prepared = adapter.state.intent(identity)["request"]
    assert result["recovery_action"] == action and not result["automatic_retry"]
    if failure is StalePrecondition: assert result["current_status"]["revision"] == 3
    assert not adapter.dispatchable_intents()
    adapter.execute(identity)
    assert adapter.state.intent(identity)["request"] == prepared
    with pytest.raises(ValueError): adapter.resume_reconciliation(identity)


def test_hash_mismatch_reuploads_only_original_spec():
    clock = [100.0]
    class Port(ScriptedReceiverPort):
        uploaded = []
        async def put_artifact(self, data, **kwargs):
            self.uploaded.append(data)
            if len(self.uploaded) == 1: raise HashMismatch()
            return await super().put_artifact(data, **kwargs)
    port = Port()
    adapter = OmegaAdapter(port, workspace="reference", clock=lambda: clock[0])
    identity = adapter.reserve("proof-create", "reference", {"spec": SPEC})
    assert adapter.execute(identity)["recovery_action"] == "repair-artifact"
    clock[0] += 1
    assert adapter.execute(identity)["durable_admission"]
    assert all(data == port.uploaded[0] for data in port.uploaded)


def test_empty_cache_bootstrap_retries_active_operation_recovery(bridge):
    clock = [100.0]
    class Port(ScriptedReceiverPort):
        unavailable = False
        async def list_operations(self, **kwargs):
            if self.unavailable: raise TransientUnavailable()
            return await super().list_operations(**kwargs)
    port = Port()
    original = OmegaAdapter(port, workspace="reference")
    identity = original.reserve("proof-create", "reference", {"spec": SPEC})
    original.execute(identity)
    restored = OmegaAdapter(port, workspace="reference", clock=lambda: clock[0])
    port.unavailable = True
    restored.bootstrap()
    assert not restored.state.snapshot()["intents"] and not restored.bootstrap_due()
    port.unavailable = False
    clock[0] += 1
    bridge.configure_adapter(restored)
    tick_until(bridge, lambda: identity in restored.state.snapshot()["intents"])
    assert len(port._operations) == 1


def test_background_status_backoff_and_permanent_error_stop():
    clock = [100.0]
    class Port(ScriptedReceiverPort):
        failure = TransientUnavailable
        calls = 0
        async def read_status(self, **kwargs):
            self.calls += 1
            raise self.failure()
    port = Port()
    adapter = OmegaAdapter(port, workspace="reference", clock=lambda: clock[0])
    adapter.refresh_status("p1")
    adapter.refresh_status("p1")
    assert port.calls == 1
    clock[0] += 1
    port.failure = Forbidden
    adapter.refresh_status("p1")
    clock[0] += 100
    adapter.refresh_status("p1")
    assert port.calls == 2
