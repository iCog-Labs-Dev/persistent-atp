"""Slow provider calls must leave journal-backed reads available."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import threading
import urllib.request

import pytest

from omegaclaw.api import serve
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.tests.test_formal_v2_receiver import provider
from omegaclaw.tests.test_hypothesis_recovery import hypothesis, HypothesisCase
from omegaclaw.tests.test_research_recovery import research, ResearchCase, commit
from commit_gate.ops import UpsertNode
from persistent_atp_client import AsyncClient, HypothesisFormalize, LemmaBridge, Preconditions
from persistent_atp_client.adapters.http import HTTPReceiverAdapter
from persistent_atp_client.adapters.local import LocalReceiverAdapter


class PausedLLM:
    def __init__(self, inner, *, call):
        self.inner, self.call = inner, call
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def history(self):
        return self.inner.history

    def _invoke(self, method, *args, **kwargs):
        self.calls += 1
        if self.calls == self.call:
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("controlled provider was not released")
        return getattr(self.inner, method)(*args, **kwargs)

    def generate(self, *args, **kwargs):
        return self._invoke("generate", *args, **kwargs)

    def generate_json(self, *args, **kwargs):
        return self._invoke("generate_json", *args, **kwargs)


@pytest.mark.parametrize("transport", ["local", "http"])
@pytest.mark.parametrize("stage", ["research", "draft", "review"])
def test_reads_remain_available_during_provider_io(request, stage, transport):
    case = request.getfixturevalue("research" if stage == "research" else "hypothesis")
    assert_reads_available(case, stage, transport)


def assert_reads_available(case, stage, transport):
    proof = "p1" if stage == "research" else case.proof
    llm = PausedLLM(case.llm, call=2 if stage == "review" else 1)
    case.llm = llm
    receiver = ReferenceReceiver(case.service)
    server = thread = None
    if transport == "http":
        server = serve(case.service, "provider-io-test-token", receiver=receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        port = HTTPReceiverAdapter(url, "provider-io-test-token", timeout=2)
    else:
        port = LocalReceiverAdapter(receiver)
    client = AsyncClient(port)
    action = LemmaBridge(proof, "p1/obs-1") if stage == "research" else HypothesisFormalize(proof, case.hypothesis)
    prepared = client.prepare(action, workspace_id="reference", request_id="paused-provider",
        preconditions=Preconditions(expected_revision=case.revision))

    async def read_while_paused(handle):
        snapshot = await handle.snapshot()
        assert snapshot.lifecycle == "running"
        digest = await client.status(workspace_id="reference", proof_id=proof)
        assert not digest.formal_verified
        assert digest.revision == digest.projection_watermark
        receipt = await client.lookup_request(workspace_id="reference", request_id=prepared.request_id)
        assert receipt.operation_ref == handle.ref
        assert (await client.submit(prepared)).ref == handle.ref
        active = await client.list_operations(workspace_id="reference", proof_id=proof)
        assert handle.ref in [item.ref for item in active]
        events, _ = await receiver.read_events(handle.ref)
        assert events[-1].event_type == "running"
        if transport == "http":
            with urllib.request.urlopen(url + "/health", timeout=2) as response:
                assert response.status == 200
        return digest.revision

    try:
        handle = asyncio.run(client.submit(prepared))
        assert llm.entered.wait(4), "provider call did not start"
        with ThreadPoolExecutor(max_workers=1) as reads:
            future = reads.submit(lambda: asyncio.run(read_while_paused(handle)))
            try:
                revision = future.result(timeout=2)
                assert not llm.release.is_set(), "reads only completed after generation ended"
                with case.service.lock:
                    assert case.service.store.head(proof)[0] == revision
                    assert len(case.service.store.active_leases(proof)) == 1
                    assert not case.service.view.list_nodes(proof, "Certificate")
            finally:
                llm.release.set()
        result = asyncio.run(handle.wait(timeout=10, poll_interval=.01))
        assert result.lifecycle == "succeeded", result.result
        if stage == "research":
            assert result.result["hypothesis_status"] == "speculative"
            assert len(llm.history) == 1
            assert case.service.store.verify_chain(proof) == case.revision + 1
        else:
            case.assert_complete(result.result)
        assert not case.service._executing
    finally:
        llm.release.set()
        if server:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        receiver.close()
        asyncio.run(client.aclose())


def test_duplicate_service_calls_wait_only_for_the_same_operation(research, monkeypatch):
    llm = PausedLLM(research.llm, call=1)
    research.llm = llm
    service = research.service
    raw = {"schema_version": 1, "request_id": "concurrent-research", "workspace": "reference",
        "operation": "lemma-bridge", "arguments": {"proof": "p1", "obstruction": "p1/obs-1"}}
    submit = service.jobs.submit
    admitted = threading.Event()
    count = 0
    def track(request):
        nonlocal count
        result = submit(request)
        if request["request_id"] == raw["request_id"]:
            count += 1
            if count == 2:
                admitted.set()
        return result
    monkeypatch.setattr(service.jobs, "submit", track)
    with ThreadPoolExecutor(max_workers=3) as workers:
        try:
            first = workers.submit(service.submit, raw)
            assert llm.entered.wait(4)
            duplicate = workers.submit(service.submit, raw)
            assert admitted.wait(2), "duplicate admission waited on the shared lock"
            status = workers.submit(service.submit, {"schema_version": 1, "request_id": "concurrent-status",
                "workspace": "reference", "operation": "proof-status", "arguments": {"proof": "p1"}})
            assert status.result(timeout=2)["phase"] == "finished"
            assert not duplicate.done()
        finally:
            llm.release.set()
        result = first.result(timeout=5)
        assert duplicate.result(timeout=5) == result
    assert result["phase"] == "finished", result
    assert len(llm.history) == 1
    assert service.store.verify_chain("p1") == research.revision + 1
    assert not service._executing


@pytest.mark.parametrize("failure", ["provider-error", "stale-revision"])
def test_slow_research_failure_releases_locks_without_committing_candidates(research, monkeypatch, failure):
    llm = PausedLLM(research.llm, call=1)
    research.llm = llm
    service = research.service
    if failure == "provider-error":
        def fail(*args, **kwargs):
            raise RuntimeError("controlled provider error")
        monkeypatch.setattr(llm.inner, "generate_json", fail)
    raw = {"schema_version": 1, "request_id": "failing-research", "workspace": "reference",
        "operation": "lemma-bridge", "arguments": {"proof": "p1", "obstruction": "p1/obs-1"}}
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(service.submit, raw)
        try:
            assert llm.entered.wait(4)
            # A concurrent writer must be able to acquire the service lock,
            # while the saved research proposal must retain its old revision.
            assert service.lock.acquire(timeout=2)
            try:
                if failure == "stale-revision":
                    commit(service, [UpsertNode("Claim", "p1/c-99",
                        {"status": "provisional", "claim_text": "Concurrent claim"})], selected="p1/c-99")
                revision = service.store.head("p1")[0]
            finally:
                service.lock.release()
        finally:
            llm.release.set()
        result = future.result(timeout=5)
    assert result["phase"] == "failed", result
    expected = "research provider failed" if failure == "provider-error" else "proof changed during research"
    assert expected in result["summary"]
    assert service.store.head("p1")[0] == revision
    assert not service.view.list_nodes("p1", "SpeculativeHypothesis")
    assert not service.store.active_leases("p1")
    assert not service._executing
    status = service.submit({"schema_version": 1, "request_id": "after-failure", "workspace": "reference",
        "operation": "proof-status", "arguments": {"proof": "p1"}})
    assert status["phase"] == "finished"


@pytest.mark.parametrize("stage", ["research", "draft", "review"])
def test_native_provider_io_keeps_reads_available(provider, tmp_path, monkeypatch, stage):
    if not os.environ.get("MORK_LIBRARY") or not os.environ.get("ARTIFACT_TEST_DB_URL"):
        if os.environ.get("OMEGA_PERSISTENCE_REQUIRED") == "1":
            pytest.fail("native provider I/O checks require MORK and a dedicated PostgreSQL database")
        pytest.skip("native MORK and dedicated PostgreSQL required")
    if os.environ.get("OMEGA_PROVIDER_IO_CASE") != stage:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q",
            f"{__file__}::test_native_provider_io_keeps_reads_available[{stage}]",
            "--basetemp", str(tmp_path / "native-process")], capture_output=True, text=True, timeout=60,
            env={**os.environ, "OMEGA_PROVIDER_IO_CASE": stage})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ["ARTIFACT_TEST_DB_URL"])
    case = (ResearchCase(tmp_path / "receiver", profile="production") if stage == "research"
        else HypothesisCase(tmp_path / "receiver", monkeypatch, profile="production"))
    try:
        assert_reads_available(case, stage, "http")
    finally:
        case.service.close()
