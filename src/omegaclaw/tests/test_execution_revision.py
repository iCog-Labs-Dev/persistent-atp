"""Revision-pinned selected cycles with the real gate/SQLite and reference critic.

The critic is explicitly scripted, runs in a real bounded Python process, and
never establishes formal proof. No real ATP or independent Lean replay runs.
"""
import asyncio
import json
import os
import select
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.store import ConcurrencyError
from mathproof.scheduler import GlobalScheduler
from persistent_atp_client import AsyncClient, Preconditions, SelectedProofCycle
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.errors import StalePrecondition
import omegaclaw.service as service_module
from omegaclaw.contracts import ContractError
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from omegaclaw.api import serve


@pytest.fixture
def service(tmp_path):
    service = OperationService(tmp_path)
    result = service._create(json.dumps({"proof_id": "pinned", "title": "Pinned cycle",
                                        "literal_target": "∀ n : Nat, n = n"}), "bootstrap")
    assert result["revision"] == 1
    yield service
    service.store.close()


def bump(service):
    """An independent committed change, always through the deterministic gate."""
    revision = service.store.head("pinned")[0]
    lease_id = f"pinned/maintenance-{revision}"
    token = service.store.acquire_lease("pinned", lease_id, actor="seed", worker_class="maintenance")
    try:
        result = CommitGate(service.view, service.store).commit(Proposal(
            "pinned", "seed", "maintenance",
            ops=(UpsertNode("ResearchState", f"pinned/rs-{revision}", {"status": "open"}),),
            base_revision=revision, lease_id=lease_id, fencing_token=token))
        assert result.accepted, result.rejections
    finally:
        service.store.release_lease("pinned", lease_id, fencing_token=token)
    service.catch_up("pinned")
    return result.revision


def scheduler(service):
    return GlobalScheduler(service.view, service.store, worker_actors={"critic": "reference-critic"})


def test_scheduler_rejects_a_newer_snapshot_before_claiming_any_dispatch(service):
    bump(service)
    with pytest.raises(ConcurrencyError) as error:
        scheduler(service).lease_best_global_move("pinned", category="critic-task", expected_revision=1)
    assert error.value.reason == Reason.STALE_BASE_REVISION
    assert service.store.active_leases("pinned") == []


def test_scheduler_preserves_the_pin_at_the_atomic_lease_claim(service, monkeypatch):
    instance = scheduler(service)
    original = instance._snapshot_frontier
    def race(proof_id):
        snapshot = original(proof_id)
        bump(service)
        return snapshot
    monkeypatch.setattr(instance, "_snapshot_frontier", race)
    with pytest.raises(ConcurrencyError) as error:
        instance.lease_best_global_move("pinned", category="critic-task", expected_revision=1)
    assert error.value.reason == Reason.STALE_BASE_REVISION
    assert service.store.active_leases("pinned") == []


@pytest.mark.parametrize("revision", [-1, True, "1"])
def test_scheduler_rejects_invalid_revision_pins(service, revision):
    with pytest.raises(ValueError):
        scheduler(service).lease_best_global_move("pinned", category="critic-task", expected_revision=revision)


def test_admitted_cycle_rejects_a_changed_head_before_worker_execution(service, monkeypatch):
    async def scenario():
        receiver = ReferenceReceiver(service)
        try:
            queued = []
            monkeypatch.setattr(receiver, "_schedule", queued.append)
            client = AsyncClient(LocalReceiverAdapter(receiver))
            request = client.prepare(SelectedProofCycle("pinned", "critic-task"), workspace_id="reference",
                                     preconditions=Preconditions(expected_revision=1), request_id="pinned-intent")
            handle = await client.submit(request)
            assert bump(service) == 2
            worker = Mock(side_effect=AssertionError("stale cycle must never launch a worker"))
            monkeypatch.setattr(service_module.subprocess, "run", worker)
            receiver._execute(queued[0])
            result = await handle.snapshot()
            assert result.lifecycle == "failed"
            assert result.result["error_code"] == "StalePrecondition"
            assert service.store.verify_chain("pinned") == 2
            assert service.store.active_leases("pinned") == []
            assert (await client.submit(request)).ref == handle.ref
            worker.assert_not_called()
        finally:
            receiver.close()
    asyncio.run(scenario())


def test_service_does_not_rebase_if_head_changes_before_scheduling(service, monkeypatch):
    original = GlobalScheduler._snapshot_frontier
    def race(instance, proof_id):
        bump(service)
        return original(instance, proof_id)
    monkeypatch.setattr(GlobalScheduler, "_snapshot_frontier", race)
    worker = Mock(side_effect=AssertionError("worker must not execute after rebasing"))
    monkeypatch.setattr(service_module.subprocess, "run", worker)
    with pytest.raises(StalePrecondition):
        service._cycle("pinned", "racing-snapshot", category="critic-task", record_progress=False, expected_revision=1)
    worker.assert_not_called()
    assert service.store.verify_chain("pinned") == 2


def test_changed_head_after_lease_is_rejected_and_the_lease_is_not_left_active(service, monkeypatch):
    original = GlobalScheduler.lease_best_global_move
    def race(instance, *args, **kwargs):
        lease = original(instance, *args, **kwargs)
        bump(service)
        return lease
    monkeypatch.setattr(GlobalScheduler, "lease_best_global_move", race)
    worker = Mock(side_effect=AssertionError("worker must not run on a changed head"))
    monkeypatch.setattr(service_module.subprocess, "run", worker)
    with pytest.raises(StalePrecondition):
        service._cycle("pinned", "racing-lease", category="critic-task", record_progress=False, expected_revision=1)
    worker.assert_not_called()
    assert service.store.active_leases("pinned") == []


def test_gate_still_rejects_a_late_commit_when_head_changes_after_worker_execution(service, monkeypatch):
    original_commit = service.gate.commit
    def race(proposal):
        if proposal.worker_class == "critic":
            bump(service)
        return original_commit(proposal)
    monkeypatch.setattr(service.gate, "commit", race)
    with pytest.raises(ContractError, match="gate rejected scripted attempt"):
        service._cycle("pinned", "late-commit", category="critic-task", record_progress=False, expected_revision=1)
    assert service.view.node("pinned/at-latecommit") is None
    assert service.store.verify_chain("pinned") == 2
    assert service.view.node("pinned/c-1").fields["status"] == "provisional"


def test_a_committed_attempt_recovers_its_original_pin_without_running_again(service, monkeypatch):
    result = service._cycle("pinned", "recovered-cycle", category="critic-task", record_progress=False, expected_revision=1)
    assert result["cycle_disposition"] == "pending-review"
    assert service.store.verify_chain("pinned") == 2
    worker = Mock(side_effect=AssertionError("recovery must not rerun an already committed worker"))
    monkeypatch.setattr(service_module.subprocess, "run", worker)
    recovered = service._cycle("pinned", "recovered-cycle", category="critic-task", record_progress=False, expected_revision=1)
    assert recovered["attempt_id"] == result["attempt_id"]
    assert recovered["packet_ref"] == result["packet_ref"]
    assert service.store.verify_chain("pinned") == 2
    with pytest.raises(StalePrecondition):
        service._cycle("pinned", "recovered-cycle", category="critic-task", record_progress=False, expected_revision=99)
    worker.assert_not_called()


def test_restarted_receiver_rechecks_a_queued_request_original_revision(service, monkeypatch):
    async def scenario():
        first = ReferenceReceiver(service)
        try:
            monkeypatch.setattr(first, "_schedule", lambda _: None)
            client = AsyncClient(LocalReceiverAdapter(first))
            request = client.prepare(SelectedProofCycle("pinned", "critic-task"), workspace_id="reference",
                                     preconditions=Preconditions(expected_revision=1), request_id="restart-pin")
            handle = await client.submit(request)
            assert bump(service) == 2
        finally:
            first.close()
        worker = Mock(side_effect=AssertionError("recovery must reject the queued stale request"))
        monkeypatch.setattr(service_module.subprocess, "run", worker)
        restored_service = OperationService(service.root)
        restored = ReferenceReceiver(restored_service)
        try:
            client = AsyncClient(LocalReceiverAdapter(restored))
            result = await client.attach(handle.ref).wait(timeout=5)
            assert result.lifecycle == "failed"
            assert result.result["error_code"] == "StalePrecondition"
            assert (await client.submit(request)).ref == handle.ref
            assert restored_service.store.verify_chain("pinned") == 2
            worker.assert_not_called()
        finally:
            restored.close()
            restored_service.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["before-execution", "after-commit"])
def test_process_death_preserves_the_original_cycle_pin_and_committed_attempt(service, monkeypatch, stage):
    """Kill a real receiver process with its original request already durable."""
    source = r'''
import asyncio
import json
from pathlib import Path
import sys
import threading
from persistent_atp_client import AsyncClient, Preconditions, SelectedProofCycle
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
root, stage = Path(sys.argv[1]), sys.argv[2]
service = OperationService(root)
receiver = ReferenceReceiver(service)
marker = root / 'pinned-kill-marker'
if stage == 'before-execution':
    receiver._schedule = lambda _: None
else:
    original = service.gate.commit
    def after_append(proposal):
        result = original(proposal)
        if proposal.worker_class == 'critic' and result.accepted:
            marker.write_text('after-commit')
            threading.Event().wait(30)
        return result
    service.gate.commit = after_append
async def main():
    client = AsyncClient(LocalReceiverAdapter(receiver))
    request = client.prepare(SelectedProofCycle('pinned', 'critic-task'), workspace_id='reference',
        preconditions=Preconditions(expected_revision=1), request_id='kill-pin-' + stage)
    handle = await client.submit(request)
    if stage == 'before-execution':
        marker.write_text(stage)
    print(json.dumps({'ref': handle.ref.to_dict(), 'request': request.to_dict()}), flush=True)
    await asyncio.sleep(30)
asyncio.run(main())
'''
    child = subprocess.Popen([sys.executable, "-c", source, str(service.root), stage],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, "receiver did not acknowledge its durable admission"
        line = child.stdout.readline()
        assert line, child.stderr.read()
        admitted = json.loads(line)
        marker = service.root / "pinned-kill-marker"
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.read_text() == stage
    finally:
        child.kill()
        child.communicate(timeout=5)
    if stage == "before-execution":
        assert bump(service) == 2
    worker = Mock(side_effect=AssertionError("recovery must not launch replacement execution"))
    monkeypatch.setattr(service_module.subprocess, "run", worker)
    async def recover():
        from persistent_atp_client import ActionRequest, OperationRef
        restored_service = OperationService(service.root)
        restored = ReferenceReceiver(restored_service)
        try:
            client = AsyncClient(LocalReceiverAdapter(restored))
            ref = OperationRef.from_dict(admitted["ref"])
            result = await client.attach(ref).wait(timeout=5)
            if stage == "before-execution":
                assert result.lifecycle == "failed"
                assert result.result["error_code"] == "StalePrecondition"
            else:
                assert result.lifecycle == "succeeded"
                assert result.result["cycle_disposition"] == "pending-review"
                attempt = restored_service.view.node(result.result["attempt_id"])
                packet = json.loads(restored_service.artifacts.get(attempt.fields["packet_artifact"]))
                assert packet["snapshot_revision"] == 1
            assert (await client.submit(ActionRequest.from_dict(admitted["request"]))).ref == ref
            assert restored_service.store.verify_chain("pinned") == 2
            assert restored_service.view.node("pinned/c-1").fields["status"] == "provisional"
            worker.assert_not_called()
        finally:
            restored.close()
            restored_service.store.close()
    asyncio.run(recover())


@pytest.mark.parametrize("change_before_execution", [False, True])
def test_revision_pin_survives_http_admission_and_service_execution(service, monkeypatch, change_before_execution):
    receiver = ReferenceReceiver(service)
    queued = []
    monkeypatch.setattr(receiver, "_schedule", queued.append)
    server = serve(service, "reference-revision-test-token", receiver=receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        async with AsyncClient(HTTPReceiverAdapter(f"http://127.0.0.1:{server.server_port}",
                                                  "reference-revision-test-token")) as client:
            request = client.prepare(SelectedProofCycle("pinned", "critic-task"), workspace_id="reference",
                preconditions=Preconditions(expected_revision=1), request_id="http-pinned")
            handle = await client.submit(request)
            if change_before_execution:
                bump(service)
                worker = Mock(side_effect=AssertionError("HTTP admission must retain the revision pin"))
                monkeypatch.setattr(service_module.subprocess, "run", worker)
            receiver._execute(queued[0])
            result = await handle.wait(timeout=5)
            assert result.request_id == request.request_id
            assert service.store.verify_chain("pinned") == 2
            assert service.view.node("pinned/c-1").fields["status"] == "provisional"
            if change_before_execution:
                assert result.lifecycle == "failed"
                assert result.result["error_code"] == "StalePrecondition"
                worker.assert_not_called()
            else:
                assert result.lifecycle == "succeeded"
                assert result.result["cycle_disposition"] == "pending-review"
                assert result.result["formal_verified"] is False
                packet = json.loads(service.artifacts.get(result.result["packet_ref"]))
                assert packet["snapshot_revision"] == 1
            assert (await client.submit(request)).ref == handle.ref
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        receiver.close()
