"""Production composition with real PostgreSQL/MORK and a synthetic HTTP ATP.

The HTTP fixture tests orchestration and recovery, never actual Lean proof.
Run scripts/test-omega-production.sh for an isolated database and native setup.
"""
import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from artifacts.schema import apply_schema
from commit_gate.ops import AddEdge, UpsertNode
from commit_gate.proposal import Proposal
from mathproof.formal_atp import build_result
from omegaclaw.execution import CanonicalExecutor
from omegaclaw.factory import build_service
from omegaclaw.production import PostgresArtifacts, ProductionService
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, ArtifactRef, Budget, FormalSearch, Preconditions, ProofCreate, OperationRef
from persistent_atp_client.adapters.local import LocalReceiverAdapter
from persistent_atp_client.errors import UnsupportedCapability


@pytest.fixture(scope="module")
def deployment(tmp_path_factory):
    db_url = os.environ.get("OMEGACLAW_PRODUCTION_TEST_DB_URL")
    if not db_url:
        pytest.skip("requires OMEGACLAW_PRODUCTION_TEST_DB_URL and native MORK")
    if "test" not in db_url.rsplit("/", 1)[-1].lower():
        pytest.fail("production integration requires a dedicated test database")
    from mork.backend import MorkSpace, MorkUnavailable
    try: MorkSpace()
    except MorkUnavailable: pytest.skip("native MORK is unavailable")
    apply_schema(db_url)
    state = {"requests": {}, "starts": {}, "searching": False, "bad_root": False, "mode": "real"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/capabilities":
                return self.send({"backend_mode": state["mode"], "provider_id": "synthetic-production-test",
                    **{key: True for key in CanonicalExecutor.REQUIRED}})
            run_id = unquote(self.path.removeprefix("/formal-search/"))
            if run_id not in state["requests"]:
                self.send_error(404)
                return
            request = state["requests"][run_id]
            result = build_result("budget-exhausted", request)
            if state["searching"]: result["disposition"] = "searching"
            if state["bad_root"]:
                from mathproof.formal_atp import build_state
                result["states"][0] = build_state(request["proof_id"], 1, "False", request["environment_hash"])
            self.send(result)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            run_id = request["run_id"]
            old = state["requests"].get(run_id)
            if old and old != request:
                self.send_error(409)
                return
            if old is None:
                state["starts"][run_id] = state["starts"].get(run_id, 0) + 1
                state["requests"][run_id] = request
            result = build_result("budget-exhausted", request)
            if state["searching"]: result["disposition"] = "searching"
            if state["bad_root"]:
                from mathproof.formal_atp import build_state
                result["states"][0] = build_state(request["proof_id"], 1, "False", request["environment_hash"])
            self.send(result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    root = tmp_path_factory.mktemp("production-receiver")
    yield root, db_url, endpoint, state
    server.shutdown()
    server.server_close()


def service_for(deployment):
    root, db_url, endpoint, _ = deployment
    return ProductionService(root, workspace="production", artifacts=PostgresArtifacts(db_url),
                             executor=CanonicalExecutor(endpoint, timeout=2))


def seed_reviewed(service, environment_hash="sha256:" + "11" * 32):
    """Trusted test-only review fixture; it is not an automated alignment claim."""
    proof = "prod" + uuid.uuid4().hex[:16]
    service._create(json.dumps({"proof_id": proof, "literal_target": "True", "title": "Integration fixture"}),
                    "create-" + proof)
    source = service.artifacts.put(b"theorem receiver_target : True := by trivial")
    ids = {name: f"{proof}/{local}" for name, local in [("claim", "c-1"), ("decl", "fd-1"),
            ("alignment", "al-1"), ("env", "env-1")]}
    ops = [UpsertNode("FormalDeclaration", ids["decl"], {"status": "aligned", "lean_type": "True", "lean_source_artifact": source}),
           UpsertNode("Alignment", ids["alignment"], {"lifecycle": "reviewed", "verdict": "aligned", "source_hash": source}),
           UpsertNode("Environment", ids["env"], {"environment_hash": environment_hash})]
    for rel, src, dst in [("ALIGNS_CLAIM", "alignment", "claim"), ("ALIGNS_DECLARATION", "alignment", "decl"),
                          ("PINNED_ENVIRONMENT", "decl", "env")]:
        ops.append(AddEdge(rel, ids[src], ids[dst], f"{proof}/e-{rel}"))
    token = service.store.acquire_lease(proof, "fixture", actor="fixture", worker_class="coordinator")
    result = service.gate.commit(Proposal(proof, "fixture", "coordinator", ops=tuple(ops),
        base_revision=1, lease_id="fixture", fencing_token=token))
    service.store.release_lease(proof, "fixture", fencing_token=token)
    assert result.accepted, result.rejections
    return proof


def test_real_storage_transport_gate_and_scoped_capabilities(deployment):
    service = service_for(deployment)
    receiver = ReferenceReceiver(service)
    with pytest.raises(ValueError, match="active receiver"):
        ReferenceReceiver(service)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        caps = await client.capabilities(contract_version="2.1")
        assert caps.receiver_mode == "production" and caps.backend_mode == "real"
        assert caps.independent_replay is False
        assert "statement-align" not in caps.actions and "formal-resume" not in caps.actions
        proof = seed_reviewed(service)
        request = client.prepare(FormalSearch(proof, f"{proof}/c-1", Budget(10, 1000)), workspace_id="production",
            request_id="formal-" + proof, preconditions=Preconditions(expected_revision=2))
        handle = await client.submit(request)
        assert (await client.submit(request)).ref == handle.ref
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded", result.result
        assert result.result["commit_revision"] == 3
        assert service.store.verify_chain(proof) == 3
        assert service.view.projected_revision(proof) == 3
        assert service.view.node(result.result["run_id"]).fields["operation_id"] == handle.ref.operation_id
        assert service.artifacts.exists(result.result["result_ref"])
        for field in ("request_ref", "result_ref"):
            assert await client.get_artifact(ArtifactRef(result.result[field]), workspace_id="production")
        status = await client.proof_status(proof, workspace_id="production")
        assert status.formal_verified is False and status.component_mode == "production"
        events, _ = await receiver.read_events(handle.ref)
        assert events[-1].event_type == "succeeded"
        page = await client.coordination_page(proof, workspace_id="production")
        assert page.component_mode == "production" and page.revision == 3
        assert deployment[3]["starts"][result.result["run_id"]] == 1
        with pytest.raises(UnsupportedCapability):
            await client.proof_cycle(proof, workspace_id="production", category="research-move")
    try: asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


@pytest.mark.parametrize("stage", ["after-admission", "after-lease", "after-provider-start", "after-gate-commit"])
def test_process_death_reconciles_original_operation_and_run(deployment, tmp_path, stage):
    service = service_for(deployment)
    proof = seed_reviewed(service)
    service.close()
    root, db_url, endpoint, state = deployment
    marker = tmp_path / "boundary"
    source = r'''
import asyncio,json,pathlib,sys,time
from omegaclaw.production import PostgresArtifacts,ProductionService
from omegaclaw.execution import CanonicalExecutor
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient,Budget,FormalSearch,Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter
root,db,endpoint,proof,stage,marker=sys.argv[1:]
service=ProductionService(root,workspace="production",artifacts=PostgresArtifacts(db),executor=CanonicalExecutor(endpoint,timeout=2))
def stop():
    pathlib.Path(marker).write_text(stage)
    time.sleep(60)
if stage=="after-lease":
    original=service.executions.prepare
    def prepare(*args):
        stop()
        return original(*args)
    service.executions.prepare=prepare
if stage=="after-provider-start":
    original=service.executor.adapter.formal_search_start
    def start(request):
        result=original(request)
        stop()
        return result
    service.executor.adapter.formal_search_start=start
if stage=="after-gate-commit":
    original=service.gate.commit
    def commit(proposal):
        result=original(proposal)
        if proposal.worker_class=="formal-atp" and result.accepted:stop()
        return result
    service.gate.commit=commit
receiver=ReferenceReceiver(service)
if stage=="after-admission":receiver._schedule=lambda _operation:None
async def main():
    client=AsyncClient(LocalReceiverAdapter(receiver))
    request=client.prepare(FormalSearch(proof,proof+"/c-1",Budget(10,1000)),workspace_id="production",request_id="crash-"+proof,preconditions=Preconditions(expected_revision=2))
    handle=await client.submit(request)
    print(json.dumps({"ref":handle.ref.to_dict(),"request_id":request.request_id}),flush=True)
    if stage=="after-admission":stop()
    time.sleep(60)
asyncio.run(main())
'''
    child = subprocess.Popen([sys.executable, "-c", source, str(root), db_url, endpoint, proof, stage, str(marker)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, child.poll()
        line = child.stdout.readline()
        assert line, child.stderr.read()
        admitted = json.loads(line)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert marker.read_text() == stage
    finally:
        child.kill()
        child.communicate(timeout=5)
    recovered = service_for(deployment)
    receiver = ReferenceReceiver(recovered)
    async def observe():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = client.attach(OperationRef.from_dict(admitted["ref"]))
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded", result.result
        assert recovered.store.verify_chain(proof) == 3
        assert recovered.view.projected_revision(proof) == 3
        lookup = await client.lookup_request(workspace_id="production", request_id=admitted["request_id"])
        assert lookup.operation_ref == handle.ref
        assert state["starts"][result.result["run_id"]] == 1
        assert len([event for event in recovered.store.read_lease_events(proof)
                    if event["actor"] == "receiver-formal-atp" and event["event"] == "issued"]) == 1
    try: asyncio.run(observe())
    finally:
        receiver.close()
        recovered.close()


def test_scripted_backend_cannot_satisfy_production_configuration(deployment):
    _, _, endpoint, state = deployment
    state["mode"] = "scripted-test"
    try:
        with pytest.raises(ValueError, match="production"):
            CanonicalExecutor(endpoint)
    finally: state["mode"] = "real"


def test_projection_failure_catches_up_without_reexecuting_backend(deployment, monkeypatch):
    service = service_for(deployment)
    receiver = ReferenceReceiver(service)
    proof = seed_reviewed(service)
    failed = threading.Event()
    original = service.view.apply_journal_event
    def fail_once(store, proof_id, revision):
        if proof_id == proof and revision == 3 and not failed.is_set():
            failed.set()
            raise RuntimeError("injected projection interruption")
        return original(store, proof_id, revision)
    monkeypatch.setattr(service.view, "apply_journal_event", fail_once)
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.formal_search(proof, f"{proof}/c-1", Budget(10, 1000), workspace_id="production")
        # The gate may append before projection and operation completion.
        deadline = time.monotonic() + 5
        while not failed.is_set() and time.monotonic() < deadline: await asyncio.sleep(.01)
        assert failed.is_set()
        assert service.health()["ready"] is False
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "succeeded", result.result
        assert service.store.verify_chain(proof) == 3
        assert service.health()["ready"] is True
        assert deployment[3]["starts"][result.result["run_id"]] == 1
    try: asyncio.run(scenario())
    finally:
        receiver.close()
        service.close()


def test_wrong_exact_root_is_retained_but_never_committed(deployment):
    service = service_for(deployment)
    receiver = ReferenceReceiver(service)
    proof = seed_reviewed(service)
    deployment[3]["bad_root"] = True
    async def scenario():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.formal_search(proof, f"{proof}/c-1", Budget(10, 1000), workspace_id="production")
        result = await handle.wait(timeout=5)
        assert result.lifecycle == "failed"
        assert "exact dispatched goal" in result.result["summary"]
        assert service.store.verify_chain(proof) == 2
        record = service.executions.get(handle.ref.operation_id)
        assert service.artifacts.exists(record["result_artifact"])
        assert service.store.active_leases(proof) == []
    try: asyncio.run(scenario())
    finally:
        deployment[3]["bad_root"] = False
        receiver.close()
        service.close()


def test_unknown_mode_and_missing_configuration_fail_before_startup(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        build_service({"mode": "typo"}, tmp_path)
    with pytest.raises(ValueError, match="mork"):
        build_service({"mode": "production"}, tmp_path)
    with pytest.raises(ValueError, match="explicit mode"):
        build_service({"workspace": "reference", "graph_backend": "mork"}, tmp_path)


def test_receiver_root_cannot_change_workspace_on_restart(deployment):
    service = service_for(deployment)
    service.close()
    root, db_url, endpoint, _ = deployment
    with pytest.raises(ValueError, match="mode/workspace"):
        ProductionService(root, workspace="other", artifacts=PostgresArtifacts(db_url),
                          executor=CanonicalExecutor(endpoint))
    restored = service_for(deployment)
    restored.close()
