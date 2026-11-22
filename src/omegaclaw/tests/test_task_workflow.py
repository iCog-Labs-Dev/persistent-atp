"""Task liveness and recovery against explicitly scripted receiver results."""
from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import logging
import threading
import time
import types

import pytest

from omegaclaw.frameworks.omega import OmegaAdapter
from omegaclaw.frameworks.omega_state import OmegaState
from persistent_atp_client.errors import TransientUnavailable, UnsupportedCapability
from persistent_atp_client.models import ArtifactRef, CoordinationPage, CoordinationItem, OperationRef, ProjectDigest
from persistent_atp_client.testing import ScriptedReceiverPort


SPEC = {"theorem": "True", "definitions": {}, "assumptions": [], "quantifier_domains": {},
        "allowed_background": [], "targets": ["True"], "verification_policy": {"independent_lean_replay": True}}
HASH = "sha256:" + "a" * 64


class TaskPort(ScriptedReceiverPort):
    """No Lean or production verification claim is made by this fixture."""
    def __init__(self):
        super().__init__()
        self.proofs = {}
        self.calls = []
        self.hold = None
        self.stagnate = False
        self.verified = False
        self.fail_audit = False
        self.lose_reply = False
        self.lookup_offline = False
        self.invalidate_after_export = False
        self.requests = []

    async def capabilities(self, **kwargs):
        cap = await super().capabilities(**kwargs)
        return replace(cap, controls=cap.controls | {"coordination-read"})

    async def submit(self, request):
        self.requests.append(request)
        if self.fail_audit and request.action.kind == "proof-audit":
            raise UnsupportedCapability("fixture audit unavailable")
        receipt = await super().submit(request)
        if request.request_id not in self.calls:
            self.calls.append(request.request_id)
            if request.action.kind == "proof-create":
                proof = "p" + request.request_id[:20]
                spec = json.loads(self._artifacts[(request.workspace_id, request.action.spec_artifact.hash)])
                self.proofs[proof] = {"target": request.action.spec_artifact, "literal": spec["theorem"],
                                      "verified": False, "revision": 1}
            else:
                proof = request.action.proof_id
            previous = self._operations[receipt.operation_ref]
            self._operations[receipt.operation_ref] = replace(previous, proof_id=proof)
            if request.action.kind != self.hold:
                self.complete(receipt.operation_ref, request.action.kind)
        if self.lose_reply:
            raise TransientUnavailable()
        return receipt

    async def lookup_request(self, *args):
        if self.lookup_offline:
            raise TransientUnavailable()
        return await super().lookup_request(*args)

    def complete(self, ref, kind):
        snapshot = self._operations[ref]
        proof = snapshot.proof_id
        record = self.proofs[proof]
        result = {"proof_id": proof, "revision": record["revision"], "event_hash": HASH, 'execution_settled': True}
        if kind == "proof-cycle":
            record["revision"] += 1
            record["verified"] = self.verified
            result.update(mathematical_disposition="stagnated" if self.stagnate else "budget-exhausted")
            if self.stagnate:
                result["obstruction_contexts"] = [{"obstruction_id": proof + "/obs-1"}]
        elif kind == "lemma-bridge":
            result.update(hypothesis_ids=[proof + "/hyp-1"], hypothesis_status="speculative")
        elif kind == "proof-audit":
            result["audit_status"] = "ok"
        elif kind == "proof-export":
            result["export_artifact"] = HASH
            if self.invalidate_after_export:
                record["verified"] = False
                record["revision"] += 1
        self._operations[ref] = replace(snapshot, lifecycle="succeeded", observation_version=1, result=result)
        self._event(ref, "succeeded", 1)

    async def read_status(self, *, workspace_id, proof_id, **kwargs):
        record = self.proofs[proof_id]
        revision = record["revision"]
        return ProjectDigest(workspace_id, proof_id, revision, HASH, revision, record["target"], record["literal"],
            "lean-verified" if record["verified"] else "provisional", record["verified"],
            "scripted fixture", "scripted-test")

    async def read_coordination(self, *, workspace_id, proof_id, **kwargs):
        return CoordinationPage(workspace_id, proof_id, self.proofs[proof_id]["revision"], HASH,
            self.proofs[proof_id]["revision"], (CoordinationItem("node", proof_id + "/obs-1", "Obstruction", {}),),
            1, None, "scripted-test")


@pytest.fixture
def task_host(tmp_path):
    port = TaskPort()
    clock = [time.time()]
    adapter = OmegaAdapter(port, workspace="reference", cache_path=tmp_path / "state.json", clock=lambda: clock[0])
    yield adapter, port, clock
    adapter.close()


def accept(adapter, message="1", **options):
    return adapter.tasks.accept("channel-session", message, {"spec": SPEC, "reply_prefix": "TASK_FINAL", **options})


def finish(adapter, identity):
    for _ in range(40):
        adapter.tasks.tick(identity)
        task = adapter.state.snapshot()["tasks"][identity]
        if task["phase"] == "delivery":
            return task
    pytest.fail("task did not reach delivery")


def creation_response(adapter, port, *, settlement=None, lifecycle='succeeded', **options):
    """One admitted creation with a scripted receiver result, before observation."""
    port.hold = 'proof-create'
    identity = accept(adapter, **options)
    adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    row = adapter.state.intent(task['steps']['create'])
    ref = OperationRef(**row['ref'])
    snapshot = port._operations[ref]
    result = {'proof_id': snapshot.proof_id, 'revision': 1, 'event_hash': HASH}
    if settlement != 'missing':
        result['execution_settled'] = settlement
    if lifecycle != 'succeeded':
        result.update(error_code='CREATION_BACKEND_UNCERTAIN', summary='Original creation completion is uncertain')
    port._operations[ref] = replace(snapshot, lifecycle=lifecycle, observation_version=1, result=result)
    port._event(ref, lifecycle, 1)
    return identity, row, ref


@pytest.mark.parametrize('settlement', ['missing', None, False])
@pytest.mark.parametrize('lifecycle', ['succeeded', 'failed', 'cancelled'])
def test_uncertain_creation_preserves_proof_and_finalizes_read_only(task_host, settlement, lifecycle):
    adapter, port, clock = task_host
    identity, original, ref = creation_response(adapter, port, settlement=settlement, lifecycle=lifecycle)
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert final['proof_id'] == report['proof_id'] == port._operations[ref].proof_id
    assert report['reason'] == 'CREATION_REQUIRES_RECONCILIATION'
    assert report['execution_settled'] is (False if settlement is False else None)
    assert report['execution_may_continue'] and report['formal_verified'] is False
    assert report['operation_states']['create'] == lifecycle
    assert report['errors'][0]['operation_ref'] == ref.to_dict()
    assert report['errors'][0]['error_code'] == ('EXECUTION_RECONCILIATION_REQUIRED'
        if lifecycle == 'succeeded' else 'CREATION_BACKEND_UNCERTAIN')
    if lifecycle != 'succeeded':
        assert report['errors'][0]['summary'] == 'Original creation completion is uncertain'
    assert report['audit']['audit_status'] == 'ok' and report['export']['export_artifact']
    assert report['evidence_matches_status']
    assert [request.action.kind for request in port.requests] == ['proof-create', 'proof-audit', 'proof-export']
    assert final['cycles'] == final['research'] == 0 and len(port.proofs) == 1
    current = adapter.state.intent(original['id'])
    assert current['request'] == original['request'] and current['fingerprint'] == original['fingerprint']
    assert final['finalization_deadline'] == clock[0] + 30
    for step in ('audit', 'export'):
        request = adapter.state.intent(final['steps'][step])['request']
        assert datetime.fromisoformat(request['deadline']) == datetime.fromtimestamp(final['finalization_deadline'], timezone.utc)
    with pytest.raises(ValueError, match='TASK_OWNED'):
        adapter.reserve('proof-cycle', 'reference', {'proof': final['proof_id']})


@pytest.mark.parametrize('after', [0, 1, 3, 6])
def test_uncertain_creation_finalization_survives_restart(task_host, after):
    adapter, port, clock = task_host
    identity, original, ref = creation_response(adapter, port)
    for _ in range(after):
        adapter.tasks.tick(identity)
    before = adapter.state.snapshot()['tasks'][identity]
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        final = finish(restored, identity)
        assert final['proof_id'] == port._operations[ref].proof_id
        assert before['steps'].items() <= final['steps'].items()
        assert final['reason'] == 'CREATION_REQUIRES_RECONCILIATION'
        assert restored.state.intent(original['id'])['request'] == original['request']
        if before.get('finalization_deadline'):
            assert final['finalization_deadline'] == before['finalization_deadline']
        assert len(port.proofs) == 1 and len(port.calls) == 3
    finally:
        restored.close()


@pytest.mark.parametrize('expired', [False, True])
def test_pending_legacy_creation_report_recovers_id_without_extending_deadline(task_host, expired):
    adapter, port, clock = task_host
    identity, original, ref = creation_response(adapter, port)
    adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    adapter.tasks._patch(identity, phase='report', proof_id=None)  # old coordinator draft, not a persisted reply
    if expired:
        clock[0] = task['finalization_deadline'] + 1
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        final = finish(restored, identity)
        report = json.loads(final['delivery']['text'].split('\n', 1)[1])
        assert report['proof_id'] == port._operations[ref].proof_id
        assert report['execution_settled'] is None and report['execution_may_continue']
        assert final['finalization_deadline'] == task['finalization_deadline']
        assert restored.state.intent(original['id'])['request'] == original['request']
        assert ('audit' in final['steps']) is (not expired)
        assert ('export' in final['steps']) is (not expired)
        assert len(port.calls) == (1 if expired else 3) and len(port.proofs) == 1
    finally:
        restored.close()


@pytest.mark.parametrize('location', ['snapshot', 'result', 'neither'])
def test_creation_uses_only_consistent_available_proof_ids(task_host, location):
    adapter, port, _ = task_host
    identity, _, ref = creation_response(adapter, port)
    snapshot = port._operations[ref]
    result = dict(snapshot.result)
    if location != 'result':
        result.pop('proof_id')
    port._operations[ref] = replace(snapshot, proof_id=snapshot.proof_id if location == 'snapshot' else None, result=result)
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] == (None if location == 'neither' else snapshot.proof_id)
    assert report['execution_settled'] is None and report['execution_may_continue']
    assert ('audit' in final['steps']) is (location != 'neither')
    assert len(port.proofs) == 1 and not {'cycle-1', 'research-1'} & set(final['steps'])


@pytest.mark.parametrize('elapsed', [2, 40])
def test_creation_observation_crossing_deadline_keeps_known_id(task_host, monkeypatch, elapsed):
    adapter, port, clock = task_host
    identity, original, ref = creation_response(adapter, port, timeout_seconds=1)
    deadline = adapter.state.snapshot()['tasks'][identity]['deadline']
    get_operation = port.get_operation
    async def late(ref_to_read):
        if ref_to_read == ref:
            clock[0] += elapsed
        return await get_operation(ref_to_read)
    monkeypatch.setattr(port, 'get_operation', late)
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] == port._operations[ref].proof_id
    assert report['reason'] == 'TASK_DEADLINE_REACHED' and report['execution_settled'] is None
    assert final['finalization_deadline'] == deadline + 30
    assert ('audit' in final['steps']) is (elapsed == 2)
    assert len(port.calls) == (3 if elapsed == 2 else 1) and len(port.proofs) == 1
    assert adapter.state.intent(original['id'])['request'] == original['request']


@pytest.mark.parametrize('proof_id', ['', 42, ['not-an-id']])
def test_invalid_creation_proof_id_is_a_protocol_error(task_host, proof_id):
    adapter, port, _ = task_host
    identity, _, ref = creation_response(adapter, port)
    port._operations[ref] = replace(port._operations[ref], result={**port._operations[ref].result, 'proof_id': proof_id})
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] is None and final['creation_binding_invalid']
    assert report['errors'][-1]['error_code'] == 'RECEIVER_PROTOCOL_ERROR'
    assert report['execution_settled'] is None and len(port.calls) == 1


@pytest.mark.parametrize('settlement', [None, True])
def test_conflicting_creation_proof_ids_require_review(task_host, settlement):
    adapter, port, _ = task_host
    identity, original, ref = creation_response(adapter, port, settlement=settlement)
    port._operations[ref] = replace(port._operations[ref],
        result={**port._operations[ref].result, 'proof_id': 'different-proof'})
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] is None and report['formal_verified'] is None
    assert final['creation_binding_invalid'] and report['execution_settled'] is None
    assert report['errors'][-1]['error_code'] == 'RECEIVER_PROTOCOL_ERROR'
    if settlement is None:
        assert report['errors'][0]['error_code'] == 'EXECUTION_RECONCILIATION_REQUIRED'
    assert final['reason'] == 'CREATION_REQUIRES_RECONCILIATION' and len(port.calls) == 1
    assert adapter.state.intent(original['id'])['request'] == original['request']


@pytest.mark.parametrize('binding', ['request_id', 'ref', 'fingerprint'])
def test_creation_draft_rejects_a_different_original_operation(task_host, binding):
    adapter, port, _ = task_host
    identity, original, _ = creation_response(adapter, port)
    adapter.tasks.tick(identity)
    row = adapter.state.intent(original['id'])
    adapter.tasks._patch(identity, phase='report', proof_id=None)
    if binding == 'fingerprint':
        adapter.state.patch(row['id'], fingerprint=HASH)
    else:
        snapshot = dict(row['snapshot'])
        snapshot[binding] = 'different-request' if binding == 'request_id' else {
            'workspace_id': 'reference', 'operation_id': 'different-operation'}
        adapter.state.patch(row['id'], snapshot=snapshot)
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] is None and final['creation_binding_invalid']
    assert report['errors'][-1]['error_code'] == 'RECEIVER_PROTOCOL_ERROR'
    assert report['execution_settled'] is None and len(port.calls) == 1


@pytest.mark.parametrize('unavailable', ['status', 'audit', 'export'])
def test_uncertain_creation_keeps_id_when_final_evidence_is_unavailable(task_host, monkeypatch, unavailable):
    adapter, port, clock = task_host
    identity, _, ref = creation_response(adapter, port)
    if unavailable == 'status':
        async def fail_status(**kwargs):
            raise TransientUnavailable('fixture status unavailable')
        monkeypatch.setattr(port, 'read_status', fail_status)
    else:
        submit = port.submit
        async def fail_evidence(request):
            if request.action.kind == 'proof-' + unavailable:
                raise UnsupportedCapability('fixture evidence unavailable')
            return await submit(request)
        monkeypatch.setattr(port, 'submit', fail_evidence)
    for _ in range(40):
        adapter.tasks.tick(identity)
        final = adapter.state.snapshot()['tasks'][identity]
        if final['phase'] == 'delivery':
            break
        clock[0] += 1  # Exercise retry backoff and the fixed evidence deadline.
    else:
        pytest.fail('unavailable finalization did not reach its bounded report')
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['proof_id'] == port._operations[ref].proof_id
    assert report['reason'] == 'CREATION_REQUIRES_RECONCILIATION'
    assert report['errors'][0]['error_code'] == 'EXECUTION_RECONCILIATION_REQUIRED'
    assert any(error['error_code'] == ('TRANSIENT_UNAVAILABLE' if unavailable == 'status' else 'UNSUPPORTED_CAPABILITY')
        for error in report['errors'])
    assert report['execution_settled'] is None and report['execution_may_continue']
    assert len(port.proofs) == 1 and not {'cycle-1', 'research-1'} & set(final['steps'])


@pytest.mark.parametrize('acknowledged', [False, True])
def test_persisted_creation_final_reply_is_immutable_after_restart(task_host, acknowledged):
    adapter, port, clock = task_host
    identity, _, ref = creation_response(adapter, port)
    final = finish(adapter, identity)
    if acknowledged:
        claimed = adapter.tasks.claim_delivery(final['source'])
        adapter.tasks.acknowledge_delivery(final['source'], claimed['delivery']['message_id'])
    before = adapter.state.snapshot()['tasks'][identity]
    port._operations[ref] = replace(port._operations[ref], observation_version=2,
        result={**port._operations[ref].result, 'execution_settled': True})
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        restored.tasks.tick(identity)
        after = restored.state.snapshot()['tasks'][identity]
        assert after['phase'] == before['phase'] and after['delivery'] == before['delivery']
        assert len(port.calls) == 3 and len(port.proofs) == 1
    finally:
        restored.close()


def test_exhausted_search_finishes_without_any_model_turns(task_host):
    adapter, port, _ = task_host
    task = finish(adapter, accept(adapter))
    assert len(port.calls) == 4  # create/cycle/audit/export, no duplicate search
    assert task["cycles"] == 1 and task["research"] == 0
    assert task["delivery"]["text"].startswith("TASK_FINAL — Target remains unverified")
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["evidence_matches_status"]
    assert report["operations"]["export"]


@pytest.mark.parametrize("after", [1, 2, 4, 7, 9])
def test_restart_preserves_task_steps_even_after_completed_creation(task_host, after):
    adapter, port, clock = task_host
    identity = accept(adapter)
    for _ in range(after):
        adapter.tasks.tick(identity)
    before = adapter.state.snapshot()["tasks"][identity]["steps"]
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        assert accept(restored) == identity
        task = finish(restored, identity)
        assert before.items() <= task["steps"].items()
        assert len(port.calls) == 4 and len(port.proofs) == 1
    finally:
        restored.close()


def test_distinct_identical_tasks_and_replayed_message_identity(task_host):
    adapter, port, _ = task_host
    one = accept(adapter, "1")
    assert accept(adapter, "1") == one
    two = accept(adapter, "2")
    assert one != two
    first, second = finish(adapter, one), finish(adapter, two)
    assert first["proof_id"] != second["proof_id"]
    assert len(port.proofs) == 2
    with pytest.raises(ValueError, match="different content"):
        accept(adapter, "1", max_cycles=0)


def test_long_wait_is_not_another_cycle_and_deadline_reports_active_work(task_host):
    adapter, port, clock = task_host
    port.hold = "proof-cycle"
    identity = accept(adapter, timeout_seconds=10)
    for _ in range(40):
        adapter.tasks.tick(identity)
    task = adapter.state.snapshot()["tasks"][identity]
    assert task["phase"] == "cycle" and len(port.calls) == 2
    clock[0] += 11
    task = finish(adapter, identity)
    assert task["reason"] == "TASK_DEADLINE_REACHED"
    cycle = adapter.state.intent(task["steps"]["cycle-1"])
    assert cycle["snapshot"]["lifecycle"] == "queued"
    assert len(port.calls) == 4


def test_preparation_crossing_deadline_cannot_dispatch_cycle(task_host, monkeypatch):
    adapter, port, clock = task_host
    identity = accept(adapter, timeout_seconds=1)
    adapter.tasks.tick(identity)
    adapter.tasks.tick(identity)
    task = adapter.state.snapshot()["tasks"][identity]
    original = port.read_status
    async def slow_status(**kwargs):
        clock[0] = task["deadline"] + 1
        return await original(**kwargs)
    monkeypatch.setattr(port, "read_status", slow_status)
    adapter.tasks.tick(identity)
    assert [request.action.kind for request in port.requests] == ["proof-create"]
    finished = finish(adapter, identity)
    assert finished["reason"] == "TASK_DEADLINE_REACHED"


def test_request_deadlines_are_bound_and_preserved_after_restart(task_host):
    adapter, port, clock = task_host
    port.lose_reply = port.lookup_offline = True
    identity = accept(adapter)
    adapter.tasks.tick(identity)
    task = adapter.state.snapshot()["tasks"][identity]
    request = adapter.state.intent(task["steps"]["create"])["request"]
    assert request["deadline"] is not None
    assert abs(datetime.fromisoformat(request["deadline"]).timestamp() - task["deadline"]) < .00001
    port.lose_reply = port.lookup_offline = False
    clock[0] += 300  # Admission succeeded before expiry; reconcile it afterwards.
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        restored.execute(task["steps"]["create"])
        assert restored.state.intent(task["steps"]["create"])["request"] == request
        assert len(port.calls) == 1
    finally:
        restored.close()


def test_blocked_task_does_not_stop_independent_task(task_host, monkeypatch):
    adapter, port, _ = task_host
    port.hold = "proof-cycle"
    first = accept(adapter, "first")
    for _ in range(3):
        adapter.tasks.tick(first)
    first_task = adapter.state.snapshot()["tasks"][first]
    blocked = adapter.state.intent(first_task["steps"]["cycle-1"])["ref"]
    entered, release = threading.Event(), threading.Event()
    original = port.get_operation
    async def get_operation(ref):
        if ref.operation_id == blocked["operation_id"]:
            entered.set()
            assert release.wait(5)
        return await original(ref)
    monkeypatch.setattr(port, "get_operation", get_operation)
    port.hold = None
    second = accept(adapter, "second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(adapter.tasks.tick, first)
        try:
            assert entered.wait(2)
            sibling = pool.submit(finish, adapter, second).result(timeout=3)
            assert sibling["phase"] == "delivery"
            assert sibling["proof_id"] != first_task["proof_id"]
        finally:
            release.set()
            pending.result(timeout=2)


def continuation(adapter, task, message="continuation", **changes):
    return adapter.tasks.accept("channel-session", message, {
        "spec": SPEC, "proof_id": task["proof_id"],
        "expected_revision": task["status"]["revision"],
        "expected_theorem_hash": task["status"]["target_artifact"]["hash"],
        "max_cycles": 1, "max_research": 0, **changes})


def test_explicit_continuation_reuses_proof_with_new_task_and_limits(task_host):
    adapter, port, _ = task_host
    previous_id = accept(adapter)
    previous = finish(adapter, previous_id)
    original_history = adapter.state.snapshot()["tasks"][previous_id]
    identity = continuation(adapter, previous)
    task = finish(adapter, identity)
    assert identity != previous_id and task["proof_id"] == previous["proof_id"]
    assert "create" not in task["steps"]
    assert len(port.proofs) == 1 and len(port.calls) == 7
    assert task["attachment"]["expected_revision"] == previous["status"]["revision"]
    assert adapter.state.snapshot()["tasks"][previous_id] == original_history


def test_completed_proof_allows_read_only_audit_and_export(task_host):
    adapter, _, _ = task_host
    task = finish(adapter, accept(adapter))
    for kind, args in (("proof-audit", {"proof": task["proof_id"]}),
                       ("proof-export", {"proof": task["proof_id"], "destination": "default"})):
        identity = adapter.reserve(kind, "reference", args)
        assert adapter.execute(identity)["durable_admission"]
    with pytest.raises(ValueError, match="TASK_OWNED"):
        adapter.reserve("proof-cycle", "reference", {"proof": task["proof_id"]})


@pytest.mark.parametrize("change", [
    {"expected_revision": 99}, {"expected_theorem_hash": "sha256:" + "b" * 64},
    {"spec": {**SPEC, "assumptions": ["an extra assumption"]}},
])
def test_continuation_rejects_changed_revision_target_or_full_spec(task_host, change):
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    identity = continuation(adapter, previous, **change)
    task = finish(adapter, identity)
    assert "cycle-1" not in task["steps"] and "create" not in task["steps"]
    assert len(port.proofs) == 1 and len(port.calls) == 6  # only audit/export follow
    assert task["errors"][0]["error_code"] == "STALE_PRECONDITION"
    if "expected_theorem_hash" in change:
        report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
        assert report["formal_verified"] is None


def test_change_after_attachment_cannot_be_silently_rebased(task_host):
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    identity = continuation(adapter, previous)
    adapter.tasks.tick(identity)
    assert adapter.state.snapshot()["tasks"][identity]["phase"] == "cycle"
    port.proofs[previous["proof_id"]]["revision"] += 1
    task = finish(adapter, identity)
    assert len(port.calls) == 6 and len(port.proofs) == 1
    assert task["errors"][0]["error_code"] == "STALE_PRECONDITION"


def test_unaccepted_spec_cannot_inherit_verification_from_existing_proof(task_host):
    adapter, port, _ = task_host
    port.verified = True
    previous = finish(adapter, accept(adapter))
    identity = continuation(adapter, previous, spec={**SPEC, "theorem": "False", "targets": ["False"]})
    task = finish(adapter, identity)
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["proof_formal_verified"] is True
    assert report["formal_verified"] is None and report["attachment"] is None
    assert "Target verified by receiver" not in task["delivery"]["text"]
    assert len(port.calls) == 6


def test_already_verified_exact_target_needs_no_new_cycle(task_host):
    adapter, port, _ = task_host
    port.verified = True
    previous = finish(adapter, accept(adapter))
    identity = continuation(adapter, previous)
    task = finish(adapter, identity)
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["formal_verified"] is True and task["reason"] == "TARGET_ALREADY_VERIFIED"
    assert task["cycles"] == 0 and len(port.calls) == 6


def test_handoff_waits_for_running_work_after_old_task_reported(task_host):
    adapter, port, clock = task_host
    port.hold = "proof-cycle"
    previous_id = accept(adapter, timeout_seconds=1)
    for _ in range(3):
        adapter.tasks.tick(previous_id)
    clock[0] += 2
    previous = finish(adapter, previous_id)
    identity = continuation(adapter, previous)
    adapter.tasks.tick(identity)
    task = adapter.state.snapshot()["tasks"][identity]
    assert task["phase"] == "attach" and task["reason"] == "EXISTING_WORK_RUNNING"
    assert len(port.calls) == 4  # the old operation remains the only cycle
    clock[0] += 241
    task = finish(adapter, identity)
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["execution_may_continue"]
    assert report["blocking_operations"][0]["lifecycle"] == "queued"
    assert len(port.proofs) == 1


def test_handoff_cannot_use_stale_reconciliation_across_dispatch_and_report(task_host, monkeypatch):
    adapter, port, clock = task_host
    previous = finish(adapter, accept(adapter))
    first = continuation(adapter, previous, "first", timeout_seconds=1)
    second = continuation(adapter, previous, "second")
    entered, release = threading.Event(), threading.Event()
    original = adapter._request
    def paused(row):
        if threading.current_thread().name == "paused-handoff" and row["id"] == previous["steps"]["cycle-1"]:
            entered.set()
            assert release.wait(5)
        return original(row)
    monkeypatch.setattr(adapter, "_request", paused)
    thread = threading.Thread(target=adapter.tasks.tick, args=(second,), name="paused-handoff")
    thread.start()
    try:
        assert entered.wait(2)
        # A rival cannot attach or dispatch while B holds proof coordination.
        adapter.tasks.tick(first)
        assert adapter.state.snapshot()["tasks"][first]["phase"] == "attach"
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    port.hold = "proof-cycle"
    adapter.tasks.tick(second)
    clock[0] += 241
    reported = finish(adapter, second)
    assert reported["phase"] == "delivery"
    assert adapter.state.snapshot()["proof_owners"][previous["proof_id"]]["task_id"] == second
    # A fresh continuation still observes the outstanding cycle after restart.
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        waiting = continuation(restored, reported, "after-report")
        restored.tasks.tick(waiting)
        assert restored.state.snapshot()["tasks"][waiting]["reason"] == "EXISTING_WORK_RUNNING"
        active = restored.client.list_operations(workspace_id="reference", proof_id=previous["proof_id"])
        assert len(active) == 1 and active[0].lifecycle == "queued"
        ref = active[0].ref
        port.complete(ref, "proof-cycle")
        # Completion changed revision: renewed consent must use the new pins.
        status = restored.status(previous["proof_id"])
        renewed = continuation(restored, {**reported, "status": status}, "settled")
        restored.tasks.tick(renewed)
        assert restored.state.snapshot()["proof_owners"][previous["proof_id"]]["task_id"] == renewed
    finally:
        restored.close()


def test_proof_coordination_survives_cache_reopen(task_host):
    adapter, port, clock = task_host
    previous = finish(adapter, accept(adapter))
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        identity = continuation(restored, previous)
        with adapter.state.coordinate_proof(previous["proof_id"]) as acquired:
            assert acquired
            restored.tasks.tick(identity)
            assert restored.state.snapshot()["tasks"][identity]["phase"] == "attach"
        restored.tasks.tick(identity)
        assert restored.state.snapshot()["tasks"][identity]["phase"] == "cycle"
    finally:
        restored.close()


def test_reserved_ordinary_intent_cannot_dispatch_across_managed_handoff(task_host):
    adapter, port, _ = task_host
    create = adapter.reserve('proof-create', 'reference', {'spec': json.dumps(SPEC)})
    adapter.execute(create)
    adapter.observe(create)
    proof = adapter.state.intent(create)['snapshot']['proof_id']
    pending = adapter.reserve('proof-cycle', 'reference', {'proof': proof})
    status = adapter.status(proof)
    identity = continuation(adapter, {'proof_id': proof, 'status': status})
    adapter.tasks.tick(identity)
    calls = len(port.calls)
    result = adapter.execute(pending)
    assert result['transport_status'] == 'error'
    assert len(port.calls) == calls and not adapter.state.intent(pending).get('request')
    with adapter.state.coordinate_proof(proof) as acquired:
        assert acquired
        # Adapter dispatch participates in the lock even without task tick.
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(adapter.execute, pending).result(timeout=2)
        assert blocked['error_code'] == 'PROOF_COORDINATION_BUSY'


def test_failed_operation_requires_reconciliation_before_handoff(task_host):
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    row = adapter.state.intent(previous["steps"]["cycle-1"])
    from persistent_atp_client.models import OperationRef
    ref = OperationRef(**row["ref"])
    port._operations[ref] = replace(port._operations[ref], lifecycle="failed",
        result={"error_code": "FORMAL_REPLAY_RECONCILIATION_REQUIRED"})
    identity = continuation(adapter, previous)
    task = finish(adapter, identity)
    assert task["reason"] == "PROOF_RECONCILIATION_REQUIRED"
    assert len(port.calls) == 6


@pytest.mark.parametrize('code', ['FORMAL_REPLAY_RECONCILIATION_REQUIRED', 'FORMAL_TRANSPORT_TIMEOUT', 'ValueError'])
def test_failed_task_preserves_diagnostics_and_unknown_execution(task_host, code):
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    port.hold = 'proof-cycle'
    identity = accept(adapter)
    for _ in range(3):
        adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    row = adapter.state.intent(task['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    port._operations[ref] = replace(port._operations[ref], lifecycle='failed', observation_version=2,
        result={'error_code': code, 'summary': 'Provider completion requires reconciliation',
            'run_id': task['proof_id'] + '/fr-1', 'execution_settled': None,
            'obstruction_contexts': [{'obstruction_id': task['proof_id'] + '/obs-1'}]})
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['reason'] == code and report['errors'][0]['error_code'] == code
    assert report['errors'][0]['operation_ref'] == row['ref']
    assert report['errors'][0]['summary'] == 'Provider completion requires reconciliation'
    assert report['cycle']['run_id'] == task['proof_id'] + '/fr-1'
    assert report['execution_settled'] is None and report['execution_may_continue']
    assert report['operation_execution']['cycle-1']['execution_settled'] is None
    assert 'settlement is unknown' in final['delivery']['text']
    assert final['research'] == 0 and len(port.calls) == 4
    assert report['formal_verified'] is False


def test_terminal_legacy_result_without_settlement_remains_unknown(task_host, monkeypatch):
    adapter, port, _ = task_host
    complete = port.complete
    def legacy(ref, kind):
        complete(ref, kind)
        if kind == 'proof-cycle':
            result = {key: value for key, value in port._operations[ref].result.items() if key != 'execution_settled'}
            port._operations[ref] = replace(port._operations[ref], result=result)
    monkeypatch.setattr(port, 'complete', legacy)
    final = finish(adapter, accept(adapter))
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['operation_states']['cycle-1'] == 'succeeded'
    assert report['execution_settled'] is None and report['execution_may_continue']


@pytest.mark.parametrize('lifecycle', ['succeeded', 'cancelled'])
@pytest.mark.parametrize('settlement', ['missing', None, False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_terminal_settlement_controls_reporting_and_handoff(task_host, lifecycle, settlement, restart):
    from persistent_atp_client.models import OperationRef
    adapter, port, clock = task_host
    port.hold = 'proof-cycle'
    identity = accept(adapter, max_research=0)
    for _ in range(3):
        adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    row = adapter.state.intent(task['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    result = {'proof_id': task['proof_id']}
    if settlement != 'missing':
        result['execution_settled'] = settlement
    port._operations[ref] = replace(port._operations[ref], lifecycle=lifecycle,
        observation_version=1, result=result)
    previous = finish(adapter, identity)
    report = json.loads(previous['delivery']['text'].split('\n', 1)[1])
    expected = settlement if type(settlement) is bool else None
    assert report['execution_settled'] is expected
    assert report['execution_may_continue'] is (expected is not True)
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path,
        clock=lambda: clock[0]) if restart else adapter
    try:
        successor = continuation(restored, previous)
        restored.tasks.tick(successor)
        if expected is True:
            restored.tasks.tick(successor)
            following = restored.state.snapshot()['tasks'][successor]
            assert restored.state.intent(following['steps']['cycle-1'])['ref']
        else:
            following = finish(restored, successor)
            assert following['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
            assert 'cycle-1' not in following['steps']
            assert restored.state.snapshot()['proof_owners'][previous['proof_id']]['task_id'] == identity
            blocked_report = json.loads(following['delivery']['text'].split('\n', 1)[1])
            assert blocked_report['blocking_operations'][0]['execution_settled'] is expected
            assert blocked_report['execution_settled'] is expected
        assert len(port.proofs) == 1
    finally:
        if restart:
            restored.close()


@pytest.mark.parametrize('restart', [False, True])
def test_dispatch_rechecks_settlement_after_attachment(task_host, restart):
    from persistent_atp_client.models import OperationRef
    adapter, port, clock = task_host
    previous = finish(adapter, accept(adapter))
    successor = continuation(adapter, previous)
    adapter.tasks.tick(successor)
    assert adapter.state.snapshot()['tasks'][successor]['phase'] == 'cycle'
    row = adapter.state.intent(previous['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    port._operations[ref] = replace(port._operations[ref], observation_version=2,
        result={**port._operations[ref].result, 'execution_settled': None})
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path,
        clock=lambda: clock[0]) if restart else adapter
    try:
        final = finish(restored, successor)
        assert final['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
        assert 'cycle-1' not in final['steps'] and final['cycles'] == 0
        assert len(port.calls) == 6  # read-only finalization still works
    finally:
        if restart:
            restored.close()


@pytest.mark.parametrize('after_attachment', [False, True])
def test_unavailable_settlement_observation_keeps_execution_unknown(task_host, monkeypatch, after_attachment):
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    identity = continuation(adapter, previous)
    if after_attachment:
        adapter.tasks.tick(identity)
        assert adapter.state.snapshot()['tasks'][identity]['phase'] == 'cycle'
    async def unavailable(**kwargs):
        raise TransientUnavailable('settlement observations are unavailable')
    monkeypatch.setattr(port, 'list_operations', unavailable)
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert final['reason'] == 'WORKFLOW_REQUIRES_RECONCILIATION'
    assert report['errors'][0]['error_code'] == 'TRANSIENT_UNAVAILABLE'
    assert report['execution_settled'] is None and report['execution_may_continue']
    assert 'cycle-1' not in final['steps'] and len(port.calls) == 6
    owner = identity if after_attachment else previous['id']
    assert adapter.state.snapshot()['proof_owners'][previous['proof_id']]['task_id'] == owner


def test_cached_operation_still_blocks_when_absent_from_listing(task_host, monkeypatch):
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    row = adapter.state.intent(previous['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    port._operations[ref] = replace(port._operations[ref], observation_version=2,
        result={**port._operations[ref].result, 'execution_settled': None})
    listing = port.list_operations
    async def without_original(**kwargs):
        return tuple(item for item in await listing(**kwargs) if item.ref != ref)
    monkeypatch.setattr(port, 'list_operations', without_original)
    final = finish(adapter, continuation(adapter, previous))
    assert final['reason'] == 'PROOF_RECONCILIATION_REQUIRED' and 'cycle-1' not in final['steps']


@pytest.mark.parametrize('settlement', [None, False])
def test_unsettled_success_cannot_start_research(task_host, monkeypatch, settlement):
    adapter, port, _ = task_host
    port.stagnate = True
    complete = port.complete
    def unsettled(ref, kind):
        complete(ref, kind)
        if kind == 'proof-cycle':
            port._operations[ref] = replace(port._operations[ref],
                result={**port._operations[ref].result, 'execution_settled': settlement})
    monkeypatch.setattr(port, 'complete', unsettled)
    final = finish(adapter, accept(adapter))
    assert final['reason'] == 'EXECUTION_RECONCILIATION_REQUIRED'
    assert final['cycle_result']['obstruction_contexts']
    assert 'research-1' not in final['steps'] and final['research'] == 0
    assert len(port.calls) == 4


def test_research_rechecks_settlement_without_spending_allowance(task_host):
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    port.stagnate = True
    identity = accept(adapter)
    for _ in range(4):
        adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    assert task['phase'] == 'research' and 'research-1' not in task['steps']
    row = adapter.state.intent(task['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    port._operations[ref] = replace(port._operations[ref], observation_version=2,
        result={**port._operations[ref].result, 'execution_settled': False})
    final = finish(adapter, identity)
    assert final['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
    assert 'research-1' not in final['steps'] and final['research'] == 0
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['execution_settled'] is False and len(port.calls) == 4


@pytest.mark.parametrize('lifecycle', ['succeeded', 'cancelled'])
def test_blocked_terminal_handoff_recovers_with_bound_receipt_after_restart(task_host, lifecycle):
    import asyncio
    from persistent_atp_client.models import OperationRef
    adapter, port, clock = task_host
    previous = finish(adapter, accept(adapter))
    row = adapter.state.intent(previous['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    original_result = {**port._operations[ref].result, 'execution_settled': None}
    port._operations[ref] = replace(port._operations[ref], lifecycle=lifecycle, observation_version=2, result=original_result)
    blocked = finish(adapter, continuation(adapter, previous, 'blocked'))
    assert blocked['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
    summary = {'format': 'persistent-atp-search-reconciliation-v1', 'operation_ref': ref.to_dict(),
        'request_id': row['id'], 'request_fingerprint': row['fingerprint'], 'proof_id': previous['proof_id'],
        'run_id': previous['proof_id'] + '/fr-1', 'execution_settled': True, 'replay_performed': False}
    artifact = asyncio.run(port.put_artifact(json.dumps({**summary, 'provider_status': 'finished'}).encode(),
        workspace_id='reference', media_type='application/json'))
    summary['receipt_artifact'] = artifact.hash
    port._operations[ref] = replace(port._operations[ref], observation_version=3,
        result={**original_result, 'execution_reconciliation': summary})
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        final = finish(restored, continuation(restored, previous, 'reconciled'))
        assert 'cycle-1' in final['steps'] and len(port.proofs) == 1
        assert restored.state.snapshot()['proof_owners'][previous['proof_id']]['task_id'] == final['id']
        assert restored.state.intent(row['id'])['request'] == row['request']
        assert port._operations[ref].lifecycle == lifecycle
        assert restored.state.snapshot()['tasks'][blocked['id']]['delivery'] == blocked['delivery']
    finally:
        restored.close()


def test_concurrent_handoffs_preserve_owner_for_unknown_success(task_host):
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    row = adapter.state.intent(previous['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    port._operations[ref] = replace(port._operations[ref], observation_version=2,
        result={**port._operations[ref].result, 'execution_settled': None})
    successors = [continuation(adapter, previous, message) for message in ('first', 'second')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(adapter.tasks.tick, successors))
    for identity in successors:
        final = finish(adapter, identity)
        assert final['reason'] == 'PROOF_RECONCILIATION_REQUIRED' and 'cycle-1' not in final['steps']
    assert adapter.state.snapshot()['proof_owners'][previous['proof_id']]['task_id'] == previous['id']
    assert len(port.proofs) == 1 and len(port.calls) == 8


@pytest.mark.parametrize('valid_receipt', [True, False])
def test_report_accepts_only_bound_receipt_as_settlement_of_failed_search(task_host, valid_receipt):
    import asyncio
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    port.hold = 'proof-cycle'
    identity = accept(adapter)
    for _ in range(3):
        adapter.tasks.tick(identity)
    task = adapter.state.snapshot()['tasks'][identity]
    row = adapter.state.intent(task['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    failure = {'error_code': 'FORMAL_TRANSPORT_TIMEOUT', 'summary': 'Original response was lost', 'execution_settled': None}
    port._operations[ref] = replace(port._operations[ref], lifecycle='failed', observation_version=2, result=failure)
    adapter.tasks.tick(identity)
    summary = {'format': 'persistent-atp-search-reconciliation-v1', 'operation_ref': row['ref'],
        'request_id': row['id'], 'request_fingerprint': row['fingerprint'], 'proof_id': task['proof_id'],
        'run_id': task['proof_id'] + '/fr-1', 'execution_settled': True, 'replay_performed': False}
    receipt = {**summary, 'provider_status': 'finished'}
    if not valid_receipt:
        receipt['request_id'] = 'different-original-request'
    artifact = asyncio.run(port.put_artifact(json.dumps(receipt).encode(), workspace_id='reference', media_type='application/json'))
    summary['receipt_artifact'] = artifact.hash
    port._operations[ref] = replace(port._operations[ref], observation_version=3,
        result={**failure, 'execution_reconciliation': summary})
    adapter.observe(row['id'])
    final = finish(adapter, identity)
    report = json.loads(final['delivery']['text'].split('\n', 1)[1])
    assert report['errors'][0]['error_code'] == 'FORMAL_TRANSPORT_TIMEOUT'
    assert report['operation_states']['cycle-1'] == 'failed'
    assert report['execution_settled'] is (True if valid_receipt else None)
    assert report['execution_may_continue'] is (not valid_receipt)
    assert report['operation_execution']['cycle-1']['reconciliation_receipt'] == artifact.hash
    assert report['formal_verified'] is False


@pytest.mark.parametrize('lifecycle', ['failed', 'succeeded', 'cancelled'])
@pytest.mark.parametrize('mismatch', [None, 'request_id', 'operation_ref', 'request_fingerprint', 'receipt', 'replay'])
def test_handoff_requires_original_receiver_bound_reconciliation_receipt(task_host, mismatch, lifecycle):
    import asyncio
    from persistent_atp_client.models import OperationRef
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    row = adapter.state.intent(previous['steps']['cycle-1'])
    ref = OperationRef(**row['ref'])
    summary = {'format': 'persistent-atp-search-reconciliation-v1', 'operation_ref': ref.to_dict(),
        'request_id': row['id'], 'request_fingerprint': row['fingerprint'], 'proof_id': previous['proof_id'],
        'run_id': previous['proof_id'] + '/fr-1', 'execution_settled': True, 'replay_performed': False}
    if mismatch in {'request_id', 'request_fingerprint'}:
        summary[mismatch] = 'different-original-request'
    if mismatch == 'operation_ref':
        summary['operation_ref'] = {'workspace_id': 'reference', 'operation_id': 'different-operation'}
    receipt = {**summary, 'provider_status': 'finished'}
    if mismatch == 'receipt':
        receipt['request_id'] = 'different-receipt'
    artifact = asyncio.run(port.put_artifact(json.dumps(receipt).encode(), workspace_id='reference', media_type='application/json'))
    summary['receipt_artifact'] = artifact.hash
    port._operations[ref] = replace(port._operations[ref], lifecycle=lifecycle, result={
        'error_code': 'FORMAL_REPLAY_RECONCILIATION_REQUIRED' if mismatch == 'replay' else 'SEARCH_OBSERVATION_FAILED',
        'execution_reconciliation': summary})
    identity = continuation(adapter, previous)
    task = finish(adapter, identity)
    if mismatch is None:
        assert 'cycle-1' in task['steps'] and len(port.calls) == 7
        assert port._operations[ref].lifecycle == lifecycle
        assert task['status']['formal_verified'] is False
    else:
        assert task['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
        assert 'cycle-1' not in task['steps'] and len(port.calls) == 6


def test_unknown_failed_operation_cannot_be_ignored_during_handoff(task_host):
    import asyncio
    from persistent_atp_client.models import ProofAudit
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    request = adapter.client.prepare(ProofAudit(previous["proof_id"]), workspace_id="reference", request_id="external-operation")
    receipt = asyncio.run(port.submit(request))
    port._operations[receipt.operation_ref] = replace(port._operations[receipt.operation_ref], lifecycle="failed")
    identity = continuation(adapter, previous)
    task = finish(adapter, identity)
    assert task["reason"] == "PROOF_RECONCILIATION_REQUIRED"
    assert "cycle-1" not in task["steps"] and len(port.proofs) == 1


@pytest.mark.parametrize('lifecycle', ['succeeded', 'cancelled'])
def test_unknown_terminal_operation_without_settlement_blocks_handoff(task_host, lifecycle):
    import asyncio
    from persistent_atp_client.models import ProofCycle
    adapter, port, _ = task_host
    previous = finish(adapter, accept(adapter))
    request = adapter.client.prepare(ProofCycle(previous['proof_id']), workspace_id='reference',
        request_id='external-terminal-operation')
    admission = asyncio.run(port.submit(request))
    ref = admission.operation_ref
    port._operations[ref] = replace(port._operations[ref], lifecycle=lifecycle,
        result={key: value for key, value in port._operations[ref].result.items() if key != 'execution_settled'})
    previous['status'] = adapter.status(previous['proof_id'])
    final = finish(adapter, continuation(adapter, previous))
    assert final['reason'] == 'PROOF_RECONCILIATION_REQUIRED'
    assert final['blocking_operations'][0]['operation_ref'] == ref.to_dict()
    assert 'cycle-1' not in final['steps'] and len(port.proofs) == 1


def test_concurrent_handoffs_claim_one_owner_and_preserve_new_task_on_restart(task_host):
    adapter, port, clock = task_host
    previous = finish(adapter, accept(adapter))
    first = continuation(adapter, previous, "first-continuation")
    second = continuation(adapter, previous, "second-continuation")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(adapter.tasks.tick, (first, second)))
    tasks = adapter.state.snapshot()["tasks"]
    owners = [identity for identity in (first, second) if tasks[identity]["phase"] == "cycle"]
    assert len(owners) == 1
    waiting = second if owners[0] == first else first
    assert tasks[waiting]["reason"] == "WAITING_FOR_TASK_OWNER"
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        task = finish(restored, owners[0])
        assert task["proof_id"] == previous["proof_id"] and len(port.proofs) == 1
        task = finish(restored, waiting)
        assert task["errors"][0]["error_code"] == "STALE_PRECONDITION"
        assert len(port.calls) == 9  # first continuation cycles, second only audits/exports
    finally:
        restored.close()


def test_handoff_through_reference_receiver_preserves_gate_history(tmp_path):
    """Real SDK/receiver/journal path with explicitly reference critic work."""
    from omegaclaw.receiver import ReferenceReceiver
    from omegaclaw.service import OperationService
    from persistent_atp_client.adapters.local import LocalReceiverAdapter
    service = OperationService(tmp_path / "service")
    receiver = ReferenceReceiver(service)
    adapter = OmegaAdapter(LocalReceiverAdapter(receiver), workspace="reference", cache_path=tmp_path / "tasks.json")
    def settle(identity):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            adapter.tasks.tick(identity)
            task = adapter.state.snapshot()["tasks"][identity]
            if task["phase"] == "delivery":
                return task
            time.sleep(.01)
        pytest.fail("reference receiver task did not settle")
    try:
        first = accept(adapter)
        previous = settle(first)
        proof = previous["proof_id"]
        assert previous["status"]["formal_verified"] is False
        old_request = adapter.state.intent(previous["steps"]["cycle-1"])["request"]
        old_revision = service.store.head(proof)[0]
        next_task = continuation(adapter, previous)
        task = settle(next_task)
        assert task["proof_id"] == proof and "create" not in task["steps"]
        assert task["attachment"]["expected_revision"] == old_revision
        assert service.store.proof_ids() == [proof]
        assert adapter.state.intent(previous["steps"]["cycle-1"])["request"] == old_request
        assert service.store.verify_chain(proof) == service.store.head(proof)[0]
        assert not task["status"]["formal_verified"]
    finally:
        receiver.close()
        adapter.close()


def test_research_uses_exact_obstruction_once_and_keeps_candidates_speculative(task_host):
    adapter, port, _ = task_host
    port.stagnate = True
    task = finish(adapter, accept(adapter))
    assert task["research"] == 1 and len(port.calls) == 5
    assert task["research_result"]["hypothesis_status"] == "speculative"
    assert task["reason"] == "CANDIDATE_REVIEW_REQUIRED"
    assert task["status"]["formal_verified"] is False


def test_zero_task_allowances_and_invalid_limits(task_host):
    adapter, port, _ = task_host
    task = finish(adapter, accept(adapter, max_cycles=0, max_research=0))
    assert task["cycles"] == 0 and len(port.calls) == 3
    for kwargs in ({"max_cycles": 11}, {"max_cycles": True}, {"max_research": 2}, {"timeout_seconds": float("inf")}):
        with pytest.raises(ValueError):
            accept(adapter, "other", **kwargs)


def test_model_reentry_cannot_duplicate_managed_creation_or_exceed_task_limits(task_host):
    adapter, port, _ = task_host
    identity = accept(adapter)
    with pytest.raises(ValueError, match="TASK_OWNED"):
        adapter.reserve("proof-create", "reference", {"spec": json.dumps(SPEC)})
    task = finish(adapter, identity)
    with pytest.raises(ValueError, match="TASK_OWNED"):
        adapter.reserve("proof-cycle", "reference", {"proof": task["proof_id"]})
    assert len(port.calls) == 4


def test_lost_creation_reply_recovers_same_request_after_restart(task_host):
    adapter, port, clock = task_host
    port.lose_reply = port.lookup_offline = True
    identity = accept(adapter)
    adapter.tasks.tick(identity)
    assert len(port.proofs) == 1
    port.lose_reply = port.lookup_offline = False
    clock[0] += 2
    task = finish(adapter, identity)
    assert len(port.calls) == 4 and len(port.proofs) == 1
    assert task["delivery"]["state"] == "pending"


def test_final_status_detects_invalidation_after_audit(task_host):
    adapter, port, _ = task_host
    port.verified = port.invalidate_after_export = True
    task = finish(adapter, accept(adapter))
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["formal_verified"] is False
    assert not report["evidence_matches_status"]


def test_unavailable_audit_is_reported_and_export_still_finishes(task_host):
    adapter, port, _ = task_host
    port.fail_audit = True
    task = finish(adapter, accept(adapter))
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert report["audit"] == {} and report["export"]["export_artifact"]
    assert report["errors"][0]["error_code"] == "UNSUPPORTED_CAPABILITY"
    assert not report["evidence_matches_status"]
    assert len(port.calls) == 3


def test_large_target_response_is_bounded_and_retains_exact_artifact(task_host):
    adapter, port, _ = task_host
    literal = "λ" * 10000
    spec = {**SPEC, "theorem": literal, "targets": [literal]}
    identity = adapter.tasks.accept("channel-session", "large", {"spec": spec})
    task = finish(adapter, identity)
    report = json.loads(task["delivery"]["text"].split("\n", 1)[1])
    assert len(task["delivery"]["text"].encode()) < 64000
    assert report["literal_target_incomplete"]
    pointer = report["target_artifact"]["hash"]
    assert json.loads(port._artifacts[("reference", pointer)])["theorem"] == literal


def test_acknowledgment_is_scoped_and_uncertain_delivery_keeps_same_id(task_host):
    adapter, port, clock = task_host
    identity = accept(adapter)
    task = finish(adapter, identity)
    message = task["delivery"]["message_id"]
    assert not adapter.tasks.acknowledge_delivery("other-channel", message)
    assert not adapter.tasks.acknowledge_delivery("channel-session", message)
    for _ in range(4):
        assert adapter.tasks.pending_delivery("channel-session")[0]["delivery"]["message_id"] == message
        assert adapter.tasks.claim_delivery("channel-session")["id"] == identity
        assert adapter.tasks.claim_delivery("channel-session") is None
        clock[0] += 31
    assert not adapter.tasks.pending_delivery("channel-session")
    assert adapter.state.snapshot()["tasks"][identity]["delivery"]["state"] == "delivery-uncertain"
    assert adapter.tasks.acknowledge_delivery("channel-session", message)
    assert adapter.state.snapshot()["tasks"][identity]["phase"] == "done"


def test_final_delivery_survives_restart_without_new_mathematical_work(task_host):
    adapter, port, clock = task_host
    identity = accept(adapter)
    task = finish(adapter, identity)
    original = adapter.tasks.claim_delivery("channel-session")["delivery"]
    restored = OmegaAdapter(port, workspace="reference", cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        assert restored.tasks.claim_delivery("channel-session") is None
        clock[0] += 31
        retried = restored.tasks.claim_delivery("channel-session")["delivery"]
        assert retried["message_id"] == original["message_id"] and retried["text"] == original["text"]
        assert len(port.calls) == 4
        assert restored.tasks.acknowledge_delivery("channel-session", retried["message_id"])
    finally:
        restored.close()


def test_v1_migration_preserves_uncertain_requests_and_references(tmp_path):
    path = tmp_path / "state.json"
    original = {"version": 1, "scope": ["local", "reference"], "intents": {"old": {"request": {"unchanged": True},
        "state": "uncertain", "ref": {"operation_id": "old"}, "cursor": "cursor"}}, "proofs": {}, "displayed": ["event"]}
    path.write_text(json.dumps(original))
    state = OmegaState(path, scope=original["scope"])
    state.update(lambda data: data.update(selected_proof=None))
    migrated = json.loads(path.read_text())
    assert migrated["version"] == 2 and migrated["tasks"] == {}
    assert migrated["intents"] == original["intents"] and migrated["displayed"] == ["event"]


@pytest.fixture
def channel(task_host, monkeypatch):
    adapter, *_ = task_host
    root = Path(__file__).resolve().parents[1] / "omega_plugin"
    modules = []
    for name, file in (("omega_persistent_atp_bridge", "omega_persistent_atp_bridge.py"),
                       ("persistent_atp_channel", "persistent_atp_channel.py")):
        spec = importlib.util.spec_from_file_location(name, root / file)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    bridge, channel = modules
    bridge.configure_adapter(adapter)
    yield channel, bridge
    bridge.disable()
    bridge._pool.shutdown(wait=True)


def test_channel_intake_replay_validation_and_durable_delivery(channel, task_host):
    module, _ = channel
    adapter, _, clock = task_host
    frame = {"seq": 7, "text": "please prove", "persistent_atp_task": {"spec": SPEC}}
    assert module.accept_frame(frame, "ws://server/session")
    assert module.model_suspended()
    assert module.accept_frame(frame, "ws://server/session")
    identity = next(iter(adapter.state.snapshot()["tasks"]))
    task = finish(adapter, identity)
    messages = []
    module.flush(messages.append, "ws://server/session")
    assert len(messages) == 1
    clock[0] += 31
    module.flush(messages.append, "ws://server/session")
    assert messages[0] == messages[1]
    assert not module.acknowledged({"client_seq": messages[0]["client_seq"]}, "ws://other")
    assert module.acknowledged({"client_seq": messages[0]["client_seq"]}, "ws://server/session")
    assert adapter.state.snapshot()["tasks"][identity]["phase"] == "done"
    assert module.accept_frame({"seq": 8, "text": "PERSISTENT_ATP_TASK {broken"}, "ws://server/session")
    assert len(adapter.state.snapshot()["intents"]) == 4
    assert not module.accept_frame({"seq": 9, "text": "ordinary follow-up"}, "ws://server/session")
    assert not module.model_suspended()
    # A correction is a new input, never a changed replay of rejected content.
    assert module.accept_frame({"seq": 8, "text": "corrected", "persistent_atp_task": {"spec": SPEC}}, "ws://server/session")
    assert len(adapter.state.snapshot()["intents"]) == 4
    assert not module.model_suspended()


def ready_channel_replies(module, adapter, url, count):
    tasks = []
    for sequence in range(1, count + 1):
        assert module.accept_frame({'seq': sequence, 'text': 'prove', 'persistent_atp_task': {'spec': SPEC}}, url)
        identity = next(reversed(adapter.state.snapshot()['tasks']))
        tasks.append(finish(adapter, identity))
    return tasks


@pytest.mark.parametrize('failure_index', [0, 1])
@pytest.mark.parametrize('send_error', [OSError, RuntimeError])
def test_failed_send_preserves_unsent_replies_retry_allowance(channel, task_host, failure_index, send_error):
    module, _ = channel
    adapter, port, clock = task_host
    url = 'ws://server/session'
    tasks = ready_channel_replies(module, adapter, url, 4)
    calls = len(port.calls)
    messages = [task['delivery']['message_id'] for task in tasks]
    failed_message = messages[failure_index]
    attempts = []
    def failing_send(frame):
        attempts.append(frame['client_seq'])
        if frame['client_seq'] == failed_message:
            raise send_error('injected disconnected socket')
        assert module.acknowledged({'client_seq': frame['client_seq']}, url)
    for attempt in range(4):
        module.flush(failing_send, url)
        state = adapter.state.snapshot()['tasks']
        assert state[tasks[failure_index]['id']]['delivery']['attempts'] == attempt + 1
        for task in tasks[failure_index + 1:]:
            assert state[task['id']]['delivery']['attempts'] == 0
            assert state[task['id']]['delivery']['state'] == 'pending'
            assert state[task['id']]['delivery']['next_attempt_at'] == 0
            assert not module.acknowledged({'client_seq': task['delivery']['message_id']}, url)
        clock[0] += 31
    assert attempts.count(failed_message) == 4
    recovered = []
    def healthy_send(frame):
        recovered.append(frame)
        assert module.acknowledged({'client_seq': frame['client_seq']}, url)
    module.flush(healthy_send, url)
    assert [frame['client_seq'] for frame in recovered] == messages[failure_index + 1:]
    assert [frame['text'] for frame in recovered] == [task['delivery']['text'] for task in tasks[failure_index + 1:]]
    state = adapter.state.snapshot()['tasks']
    assert state[tasks[failure_index]['id']]['delivery']['state'] == 'delivery-uncertain'
    assert all(state[task['id']]['delivery']['attempts'] == 1 for task in tasks[failure_index + 1:])
    assert all(state[task['id']]['phase'] == 'done' for task in tasks[failure_index + 1:])
    assert len(port.calls) == calls


def test_single_delivery_claim_preserves_unsent_replies_across_restart(channel, task_host):
    module, bridge = channel
    adapter, port, clock = task_host
    url = 'ws://server/session'
    tasks = ready_channel_replies(module, adapter, url, 4)
    source = module._source(url)
    claimed = adapter.tasks.claim_delivery(source)
    assert claimed['id'] == tasks[0]['id']
    # Simulate process loss after the claim, before any socket outcome exists.
    restored = OmegaAdapter(port, workspace='reference', cache_path=adapter.state.path, clock=lambda: clock[0])
    try:
        bridge.configure_adapter(restored)
        state = restored.state.snapshot()['tasks']
        assert [state[task['id']]['delivery']['attempts'] for task in tasks] == [1, 0, 0, 0]
        messages = []
        def acknowledged_send(frame):
            messages.append(frame)
            assert module.acknowledged({'client_seq': frame['client_seq']}, url)
        module.flush(acknowledged_send, url)
        assert [frame['client_seq'] for frame in messages] == [task['delivery']['message_id'] for task in tasks[1:]]
        clock[0] += 31
        module.flush(acknowledged_send, url)
        assert messages[-1]['client_seq'] == claimed['delivery']['message_id']
        assert messages[-1]['text'] == claimed['delivery']['text']
        state = restored.state.snapshot()['tasks']
        assert [state[task['id']]['delivery']['attempts'] for task in tasks] == [2, 1, 1, 1]
        assert all(state[task['id']]['phase'] == 'done' for task in tasks)
        assert len(port.calls) == 16
    finally:
        bridge.configure_adapter(adapter)
        restored.close()


def test_concurrent_flushes_claim_distinct_replies(channel, task_host):
    module, _ = channel
    adapter, _, _ = task_host
    url = 'ws://server/session'
    tasks = ready_channel_replies(module, adapter, url, 4)
    barrier = threading.Barrier(2)
    first_send = threading.local()
    messages = []
    def send(frame):
        messages.append(frame)
        if not getattr(first_send, 'reached', False):
            first_send.reached = True
            barrier.wait(timeout=3)
        assert module.acknowledged({'client_seq': frame['client_seq']}, url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(module.flush, send, url) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert len(messages) == len({frame['client_seq'] for frame in messages}) == 4
    assert {frame['client_seq'] for frame in messages} == {task['delivery']['message_id'] for task in tasks}
    state = adapter.state.snapshot()['tasks']
    assert all(state[task['id']]['delivery']['attempts'] == 1 for task in tasks)
    assert all(state[task['id']]['phase'] == 'done' for task in tasks)


def test_flush_keeps_four_message_limit_without_claiming_later_replies(channel, task_host):
    module, _ = channel
    adapter, _, _ = task_host
    url = 'ws://server/session'
    tasks = ready_channel_replies(module, adapter, url, 6)
    messages = []
    module.flush(messages.append, url)
    state = adapter.state.snapshot()['tasks']
    assert [frame['client_seq'] for frame in messages] == [task['delivery']['message_id'] for task in tasks[:4]]
    assert [state[task['id']]['delivery']['attempts'] for task in tasks] == [1, 1, 1, 1, 0, 0]
    module.flush(messages.append, url)
    assert [frame['client_seq'] for frame in messages] == [task['delivery']['message_id'] for task in tasks]


def test_event_continuation_is_bounded_and_not_triggered_by_duplicate_status(channel, task_host):
    module, _ = channel
    adapter, _, _ = task_host
    identity = adapter.reserve("proof-create", "reference", {"spec": json.dumps(SPEC)})
    adapter.execute(identity)
    adapter.observe(identity)
    assert module.continuation_turns() == 2
    adapter.observe(identity)
    assert module.continuation_turns() == 0
    task = finish(adapter, accept(adapter))
    assert module.continuation_turns() == 0


def test_oversized_notifications_retain_terminal_routing(channel):
    _, bridge = channel
    raw = bridge._render({"notification_id": "id", "lifecycle": "succeeded",
        "mathematical_disposition": "budget-exhausted", "result": {"large": "x" * 20000}})
    record = json.loads(raw.removeprefix("PERSISTENT_ATP_RESULT "))
    assert record["lifecycle"] == "succeeded" and record["mathematical_disposition"] == "budget-exhausted"
    assert record["rendering_incomplete"]


def test_real_websocket_reconnect_retries_final_with_same_id(channel, task_host, monkeypatch):
    """Actual local sockets, patched pinned channel, scripted proof execution."""
    from websockets.sync.server import serve
    from omegaclaw.runtime import DEFAULT_OMEGA_ROOT
    from omegaclaw.runtime_overlay import patched_bytes
    hook, bridge = channel
    adapter, port, clock = task_host
    config = types.ModuleType("config")
    config.config_get_by_key = lambda key, default=None: default
    logger = types.ModuleType("src.logger")
    logger.get_logger = logging.getLogger
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "src.logger", logger)
    monkeypatch.setitem(sys.modules, "channels", types.SimpleNamespace(CommChannel=object))
    raw = (DEFAULT_OMEGA_ROOT / "channels/wschat.py").read_bytes()
    ws = types.ModuleType("wschat")
    ws.__file__ = str(DEFAULT_OMEGA_ROOT / "channels/wschat.py")
    monkeypatch.setitem(sys.modules, "wschat", ws)
    exec(compile(patched_bytes("channels/wschat.py", raw), ws.__file__, "exec"), ws.__dict__)
    messages = []
    errors = []
    def handler(socket):
        try:
            socket.recv(timeout=5)  # resume
            socket.send(json.dumps({"type": "user_message", "seq": 1, "text": "prove",
                "persistent_atp_task": {"spec": SPEC}}))
            frame = json.loads(socket.recv(timeout=5))
            messages.append(frame)
            if len(messages) == 1:
                # Lose the acknowledgment after the server accepted the reply.
                clock[0] += 31
                socket.close()
            else:
                socket.send(json.dumps({"type": "ack", "client_seq": frame["client_seq"]}))
                socket.recv(timeout=2)
        except TimeoutError:
            pass
        except Exception as error:
            # Normal client shutdown can close the last blocking receive.
            from websockets.exceptions import ConnectionClosed
            if not isinstance(error, ConnectionClosed):
                errors.append(error)
    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ws.start_websocket("ws://127.0.0.1:" + str(server.socket.getsockname()[1]))
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            clock[0] += .1
            bridge._last_poll = 0
            bridge.take_completed()
            tasks = adapter.state.snapshot()["tasks"]
            if tasks and all(task["phase"] == "done" for task in tasks.values()):
                break
            time.sleep(.01)
        else:
            pytest.fail("WebSocket task did not recover its lost final ACK: " + repr({
                "messages": len(messages), "errors": errors,
                "delivery": [(row["phase"], row.get("delivery")) for row in tasks.values()]}))
        assert not errors
        assert len(messages) == 2 and messages[0] == messages[1]
        assert len(port.calls) == 4 and len(port.proofs) == 1
    finally:
        ws.stop_websocket()
        ws._thread.join(timeout=3)
        server.shutdown()
        thread.join(timeout=3)
