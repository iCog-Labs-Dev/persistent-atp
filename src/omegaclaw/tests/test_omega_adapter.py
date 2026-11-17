"""Framework contract: real reference facade and explicitly scripted failures."""
import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import CursorExpired, ReceiverProtocolError, TransientUnavailable, UnsupportedCapability
from persistent_atp_client.models import CancelRequest, OperationRef, ProjectDigest, ArtifactRef
from persistent_atp_client import Budget, FormalSearch
from persistent_atp_client.codecs import to_dict
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.testing import ScriptedReceiverPort
from omegaclaw.api import serve
from omegaclaw.frameworks.omega import OmegaAdapter, SKILLS
from omegaclaw.frameworks.omega_state import OmegaState
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService


def theorem():
    return {"theorem": '∀ n : Nat, n = n — "quotes" \\ and\nUnicode λ', "definitions": {},
            "assumptions": [], "quantifier_domains": {"n": "Nat"}, "allowed_background": [],
            "targets": ["literal theorem"], "verification_policy": {"independent_lean_replay": True}}


@pytest.fixture
def host(tmp_path):
    service = OperationService(tmp_path / "service")
    receiver = ReferenceReceiver(service)
    adapter = OmegaAdapter(LocalReceiverAdapter(receiver), workspace="reference", cache_path=tmp_path / "cache.json")
    yield adapter, receiver, service
    receiver.close()
    adapter.close()


def settle(adapter, identity):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        adapter.observe(identity)
        if adapter.state.intent(identity)["state"] == "completed":
            return adapter.state.intent(identity)
        time.sleep(0.01)
    pytest.fail("reference operation did not settle")


def create(adapter):
    identity = adapter.reserve("proof-create", "reference", {"spec": json.dumps(theorem(), ensure_ascii=False)})
    admission = adapter.execute(identity)
    assert admission["durable_admission"] is True, admission
    settle(adapter, identity)
    return identity, adapter.state.snapshot()["selected_proof"]


def test_complete_input_is_preserved_in_artifact_journal_packet_and_export(host):
    adapter, receiver, service = host
    identity, proof = create(adapter)
    status = adapter.status(proof)
    complete = service.artifacts.get(status["target_artifact"]["hash"])
    assert json.loads(complete) == theorem()
    assert status["literal_target"] == theorem()["theorem"]
    assert service.store.verify_chain(proof) == 1
    cycle = adapter.reserve("proof-cycle", "reference", {"proof": proof})
    assert adapter.execute(cycle)["durable_admission"]
    settle(adapter, cycle)
    result = next(report["result"] for report in adapter.pending_reports()
                  if report.get("request_id") == cycle and report.get("result"))
    packet = json.loads(service.artifacts.get(result["packet_ref"]))
    assert packet["theorem_specification"] == theorem()
    assert packet["assumptions"] == theorem()["assumptions"]
    request = adapter.state.intent(identity)["request"]
    assert request["request_id"] == identity
    assert adapter.state.intent(identity)["fingerprint"].startswith("sha256:")
    assert all("client" not in key for key in adapter.state.snapshot())


def test_distinct_later_intent_gets_new_sdk_request(host):
    adapter, _, service = host
    first, proof = create(adapter)
    second, second_proof = create(adapter)
    assert first != second and proof != second_proof
    assert len(service.store.proof_ids()) == 2


def test_reentry_reuses_one_unresolved_workflow_identity(host):
    adapter, _, _ = host
    args = {"spec": json.dumps(theorem())}
    first = adapter.reserve("proof-create", "reference", args)
    assert adapter.reserve("proof-create", "reference", args) == first
    assert adapter.reserve("proof-create", "reference", args, new_intent=True) != first
    with pytest.raises(ValueError, match="different arguments"):
        adapter.reserve("proof-audit", "reference", {"proof": "p1"}, intent_id=first)


def test_uncertain_submission_recovers_original_prepared_request_after_restart(host, tmp_path):
    adapter, receiver, service = host
    class LostReply(LocalReceiverAdapter):
        unavailable = True
        async def submit(self, request):
            receipt = await self.receiver.submit(request)
            if self.unavailable:
                raise TransientUnavailable()
            return receipt
        async def lookup_request(self, *args):
            if self.unavailable:
                raise TransientUnavailable()
            return await self.receiver.lookup_request(*args)
    port = LostReply(receiver)
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "recovery.json")
    identity = adapter.reserve("proof-create", "reference", {"spec": json.dumps(theorem())})
    result = adapter.execute(identity)
    assert result["error_code"] == "SUBMISSION_UNCERTAIN"
    prepared = adapter.state.intent(identity)["request"]
    assert adapter.state.intent(identity)["state"] == "uncertain"
    port.unavailable = False
    restored = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "recovery.json")
    restored.startup()
    assert restored.execute(identity)["request_id"] == identity
    settle(restored, identity)
    assert restored.state.intent(identity)["request"] == prepared
    assert receiver._db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert len(service.store.proof_ids()) == 1


def test_empty_cache_recovers_active_creation_without_submitting_again(host, tmp_path):
    adapter, receiver, service = host
    entered, release = threading.Event(), threading.Event()
    original = receiver._execute
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    receiver._execute = paused
    identity = adapter.reserve("proof-create", "reference", {"spec": json.dumps(theorem())})
    assert adapter.execute(identity)["durable_admission"]
    assert entered.wait(2)
    try:
        fresh = OmegaAdapter(LocalReceiverAdapter(receiver), workspace="reference", cache_path=tmp_path / "empty.json")
        fresh.startup()
        assert fresh.state.intent(identity)["ref"] == adapter.state.intent(identity)["ref"]
        assert fresh.reserve("proof-create", "reference", {"spec": json.dumps(theorem())}) == identity
    finally:
        release.set()
    settle(fresh, identity)
    assert len(service.store.proof_ids()) == 1


def test_duplicate_event_pages_and_expired_cursor_do_not_repeat_milestones(host):
    adapter, receiver, _ = host
    identity, proof = create(adapter)
    pending = adapter.pending_reports()
    assert pending
    adapter.acknowledge([item["notification_id"] for item in pending])
    adapter.observe(identity)
    assert adapter.pending_reports() == []
    class ExpiredOnce(LocalReceiverAdapter):
        expired = False
        async def read_events(self, ref, cursor=None):
            if cursor and not self.expired:
                self.expired = True
                raise CursorExpired()
            return await self.receiver.read_events(ref, cursor)
    adapter.client._client._port = ExpiredOnce(receiver)
    reports = adapter.observe(identity)
    assert any(report.get("history_incomplete") for report in reports)
    assert not any(report.get("event") for report in reports)


def test_receiver_scope_mismatch_does_not_reuse_checkpoint(tmp_path):
    state = OmegaState(tmp_path / "cache.json", scope=["one", "reference"])
    state.update(lambda data: data.update(selected_proof="p1"))
    with pytest.raises(ValueError, match="different receiver/workspace"):
        OmegaState(tmp_path / "cache.json", scope=["two", "reference"]).snapshot()


def test_authoritative_status_invalidation_and_regression_are_distinguished(tmp_path):
    digest = ProjectDigest("reference", "p1", 2, "sha256:" + "a" * 64, 2,
                           ArtifactRef("sha256:" + "b" * 64), "P", "lean-verified", True, "fixture", "scripted-test")
    class Port:
        async def read_status(self, **kwargs): return digest
        async def aclose(self): pass
    adapter = OmegaAdapter(Port(), workspace="reference", cache_path=tmp_path / "status.json")
    assert adapter.status("p1")["formal_verified"] is True
    digest = replace(digest, revision=3, formal_verified=False, claim_status="tainted")
    assert adapter.status("p1")["milestone"] == "verification-invalidated"
    adapter.status("p1")
    restored = OmegaState(tmp_path / "status.json", scope=["local", "reference"])
    assert any(row["milestone"] == "verification-invalidated" for row in restored.snapshot()["outbox"].values())
    digest = replace(digest, revision=1, projection_watermark=1)
    with pytest.raises(ReceiverProtocolError, match="regressed"):
        adapter.status("p1")


def test_cycle_allowance_survives_reentry_and_completion(host):
    adapter, _, _ = host
    _, proof = create(adapter)
    adapter.max_cycles_per_proof = 1
    args = {"proof": proof}
    identity = adapter.reserve("proof-cycle", "reference", args)
    assert adapter.reserve("proof-cycle", "reference", args) == identity
    assert adapter.execute(identity)["durable_admission"]
    settle(adapter, identity)
    with pytest.raises(ValueError, match="CYCLE_LIMIT_REACHED"):
        adapter.reserve("proof-cycle", "reference", args)


def test_artifact_rendering_marks_omissions_and_retains_complete_retrieval(host):
    adapter, _, _ = host
    _, proof = create(adapter)
    ref = ArtifactRef(**adapter.status(proof)["target_artifact"])
    excerpt = adapter.artifact_excerpt(ref, max_chars=12)
    assert excerpt["rendering_incomplete"] and len(excerpt["text"]) == 12
    complete = adapter.artifact_excerpt(ref)
    assert not complete["rendering_incomplete"]
    assert json.loads(complete["text"]) == theorem()


def test_adapter_full_spec_and_observation_match_over_http(host, tmp_path):
    local, receiver, service = host
    server = serve(service, "adapter-transport-test-token", receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    remote = OmegaAdapter(HTTPReceiverAdapter(f"http://127.0.0.1:{server.server_port}", "adapter-transport-test-token"),
                          workspace="reference", cache_path=tmp_path / "remote.json")
    try:
        identity, proof = create(remote)
        assert remote.status(proof) == local.status(proof)
        assert json.loads(remote.artifact_excerpt(ArtifactRef(**remote.status(proof)["target_artifact"]))["text"]) == theorem()
        assert remote.state.intent(identity)["event_sequence"] == 3
    finally:
        remote.close()
        server.shutdown()
        server.server_close()


def test_observation_reports_requested_and_effective_budgets_separately(tmp_path):
    class Port(ScriptedReceiverPort):
        async def read_status(self, **kwargs):
            return ProjectDigest("reference", "p1", 1, "sha256:" + "a" * 64, 1,
                ArtifactRef("sha256:" + "b" * 64), "P", "provisional", False, "scripted", "scripted-test")
    port = Port()
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "budgets.json")
    identity = adapter.reserve("formal-search", "reference", {"proof": "p1", "claim": "p1/c-1",
        "budget": '{"max_steps":1000,"wall_clock_ms":100000}'})
    request = adapter.client.prepare(FormalSearch("p1", "p1/c-1", Budget(1000, 100000)),
                                     workspace_id="reference", request_id=identity)
    handle = adapter.client.submit(request)
    finished = port.finish(handle.ref, mathematical_disposition="stagnated")
    port._operations[handle.ref] = replace(finished, result={"budget": {"max_steps":100, "wall_clock_ms":30000}})
    adapter.state.patch(identity, request=to_dict(request), ref=to_dict(handle.ref), state="accepted")
    report = next(row for row in adapter.observe(identity) if row.get("lifecycle"))
    assert report["requested_budget"] == {"max_steps":1000, "wall_clock_ms":100000}
    assert report["effective_budget"] == {"max_steps":100, "wall_clock_ms":30000}
    assert "target remains open" in report["message"]


def test_changed_duplicate_event_is_rejected_without_losing_operation(host):
    adapter, receiver, _ = host
    identity, _ = create(adapter)
    original = receiver.read_events
    async def changed(ref, cursor=None):
        events, last = await original(ref, None)
        return (replace(events[-1], event_type="forged"),), last
    receiver.read_events = changed
    report = adapter.observe(identity)[0]
    assert report["error_code"] == "RECEIVER_PROTOCOL_ERROR"
    assert report["request_id"] == identity and report["operation_ref"]


def test_ui_controls_forward_only_advertised_sdk_controls(tmp_path):
    port = ScriptedReceiverPort()
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "controls.json")
    identity = adapter.reserve("proof-create", "reference", {"spec": json.dumps(theorem())})
    ref = OperationRef(**adapter.execute(identity)["operation_ref"])
    result = adapter.control("cancel", CancelRequest("cancel-stable", ref))
    assert result["lifecycle"] == "cancelled"
    assert adapter.control("cancel", CancelRequest("cancel-stable", ref)) == result


@pytest.mark.parametrize("change", [{"actor": "maintenance"}, {"lease_id": "forged"}, {"approved": True}])
def test_complete_spec_rejects_effective_authority_overrides(host, change):
    adapter, _, _ = host
    with pytest.raises(ValueError):
        adapter.reserve("proof-create", "reference", {"spec": json.dumps({**theorem(), **change})})


def test_budget_and_artifact_arguments_never_become_paths_or_code(host):
    adapter, _, _ = host
    with pytest.raises(ValueError):
        adapter.reserve("statement-align", "reference", {"proof": "p1", "claim": "p1/c-1", "lean_source": "/etc/passwd"})
    with pytest.raises(ValueError):
        adapter.reserve("formal-search", "reference", {"proof": "p1", "claim": "p1/c-1", "budget": '{"max_steps":1,"max_steps":2,"wall_clock_ms":1}'})
    with pytest.raises(ValueError):
        adapter.reserve("proof-create", "other", {"spec": json.dumps(theorem())})


def test_ui_controls_explicitly_report_unavailable_capability(host):
    adapter, _, _ = host
    adapter.startup()
    with pytest.raises(UnsupportedCapability, match="cancel"):
        adapter.control("cancel", CancelRequest("control", OperationRef("reference", "unknown")))


def test_sync_adapter_never_nests_a_framework_event_loop(host):
    adapter, _, _ = host
    async def host_loop():
        with pytest.raises(RuntimeError, match="active event loop"):
            adapter.startup()
    asyncio.run(host_loop())


def test_bridge_returns_before_receiver_finishes_and_preserves_outbox_until_ack(host):
    adapter, receiver, service = host
    path = Path(__file__).resolve().parents[1] / "omega_plugin/omega_persistent_atp_bridge.py"
    spec = importlib.util.spec_from_file_location("nonblocking_omega_test_bridge", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bridge.configure_adapter(adapter)
    entered, release = threading.Event(), threading.Event()
    original = service._create
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    service._create = paused
    started = time.monotonic()
    result = json.loads(bridge.proof_create(json.dumps(theorem()), "reference"))
    assert result["durable_admission"] is False and result["phase"] == "dispatching"
    assert time.monotonic() - started < 0.5
    assert entered.wait(2)
    release.set()
    try:
        deadline = time.monotonic() + 5
        delivered = ""
        while time.monotonic() < deadline:
            delivered = bridge.take_completed()
            if '"lifecycle": "succeeded"' in delivered:
                break
            time.sleep(0.02)
        assert '"lifecycle": "succeeded"' in delivered
        assert not bridge.ack_completed(delivered, "APPEND-FAILED")
        assert adapter.pending_reports()
        assert bridge.ack_completed(delivered, "APPEND-VERIFIED test")
        assert all(item["notification_id"] not in delivered for item in adapter.pending_reports())
        bridge.disable()
        assert json.loads(bridge.proof_cycle("p1", "reference"))["error_code"] == "PLUGIN_DISABLED"
        assert len(SKILLS) == 9
    finally:
        bridge.disable()
        bridge._pool.shutdown(wait=True)
