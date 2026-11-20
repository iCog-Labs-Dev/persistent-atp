"""Candidate admission and intake recovery with controlled provider/LLM fixtures."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from alignment.llm_client import MockLLMClient
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import LeaseCandidate
from omegaclaw.formal_runtime import FormalProviderConfig
from omegaclaw.intake_runtime import IntakeConfig
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.service import OperationService
from omegaclaw.tests.test_formal_v2_receiver import provider
from omegaclaw.tests.test_production_intake import CANDIDATE, REVIEW, SPEC
from omegaclaw.tests.test_research_runtime import candidate
from persistent_atp_client import AsyncClient, HypothesisFormalize, Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter


class HypothesisCase:
    def __init__(self, root, monkeypatch, *, profile="reference", suggestion=None):
        self.root, self.profile = root, profile
        self.llm = MockLLMClient(responses=[CANDIDATE, REVIEW])
        monkeypatch.setattr(IntakeConfig, "llm", lambda _config: self.llm)
        self.open()
        created = self.service.submit({"schema_version": 1, "request_id": "create", "workspace": "reference",
            "operation": "proof-create", "arguments": {"spec": json.dumps(SPEC)}})
        self.proof = created["proof_id"]
        self.hypothesis = self.proof + "/hyp-" + "a" * 32
        self.claim = self.proof + "/c-1"
        artifact = self.service.artifacts.put(b"controlled research evidence")
        proposal = suggestion if suggestion is not None else candidate("True")
        self.commit([
            UpsertNode("FormalRun", self.proof + "/fr-1", {"status": "stagnated"}),
            UpsertNode("Obstruction", self.proof + "/obs-1", {"formal_run_id": self.proof + "/fr-1"}),
            AddEdge("RAISED_OBSTRUCTION", self.proof + "/fr-1", self.proof + "/obs-1", self.proof + "/raised"),
            UpsertNode("SpeculativeHypothesis", self.hypothesis, {**proposal, "schema_version": 1,
                "hypothesis_id": self.hypothesis, "source_ids": [self.claim, self.proof + "/obs-1"],
                "prerequisites": [], "support": "Controlled speculative fixture.",
                "packet_artifact": artifact, "result_artifact": artifact}),
        ])
        self.revision = self.service.store.head(self.proof)[0]
        self.request = {"schema_version": 1, "request_id": "formalize", "workspace": "reference",
            "operation": "hypothesis-formalize", "arguments": {"proof": self.proof,
                "hypothesis": self.hypothesis, "expected_revision": str(self.revision)}}

    def open(self):
        self.service = OperationService(self.root, profile=self.profile,
            formal_config=FormalProviderConfig("http://fixture", deployment_id="fixture", profile="bounded",
                mode="real" if self.profile == "production" else "scripted-test", poll_seconds=.05),
            intake_config=IntakeConfig("DeepSeek", "fixture-lean"),
            artifact_db_url=os.environ.get("ARTIFACT_TEST_DB_URL") if self.profile == "production" else None)

    def restart(self):
        self.service.close()
        self.open()

    def commit(self, ops):
        service = self.service
        lease = service.store.claim_next(self.proof, [LeaseCandidate(self.claim, {})],
            actor="fixture", worker_class="coordinator", base_revision=service.store.head(self.proof)[0])
        try:
            result = service.gate.commit(Proposal(self.proof, lease.actor, lease.worker_class, ops=tuple(ops),
                base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
                selected_move_id=lease.selected_move_id))
            assert result.accepted, result.to_dict()
            service.catch_up(self.proof)
        finally:
            service.store.release_lease(self.proof, lease.lease_id, fencing_token=lease.fencing_token)

    def run(self):
        return self.service.submit(self.request)

    def operation(self):
        return self.service.jobs.conn.execute("SELECT operation_id FROM operations WHERE request_id='formalize'").fetchone()[0]

    def assert_complete(self, result):
        assert result["phase"] == "finished", result
        assert result["formalization_ready"], result
        assert len(self.llm.history) == 2
        assert len(self.service.view.list_nodes(self.proof, "Claim")) == 2
        assert len(self.service.view.list_nodes(self.proof, "FormalDeclaration")) == 1
        assert len(self.service.view.list_nodes(self.proof, "Alignment")) == 1
        assert len(self.service.view.edges_from(self.hypothesis, "PROMOTED_TO")) == 1
        assert self.service.view.node(result["claim_id"]).fields["status"] == "provisional"
        assert self.service.view.node(self.claim).fields["status"] == "provisional"
        assert not self.service.store.active_leases(self.proof)
        assert not self.service.view.list_nodes(self.proof, "Certificate")


@pytest.fixture
def hypothesis(provider, tmp_path, monkeypatch):
    case = HypothesisCase(tmp_path, monkeypatch)
    try:
        yield case
    finally:
        case.service.close()


def interrupt(case, monkeypatch, boundary):
    service = case.service
    commit, save = service.gate.commit, service.intake._save
    stopped = []
    def stop():
        if not stopped:
            stopped.append(True)
            raise SystemExit("controlled hypothesis interruption: " + boundary)
    def crash_commit(proposal):
        result = commit(proposal)
        labels = {getattr(op, "label", None) for op in proposal.ops}
        if result.accepted and (
                boundary == "admission" and "Claim" in labels
                or boundary == "draft" and proposal.worker_class == "formalizer"
                or boundary == "source" and "Environment" in labels
                or boundary == "alignment" and "Alignment" in labels
                or boundary == "aligned" and any(getattr(op, "value", None) == "aligned" for op in proposal.ops)):
            stop()
        return result
    def crash_save(operation, state):
        save(operation, state)
        if (boundary == "candidate" and "candidate" in state
                or boundary == "review" and "review" in state
                or boundary == "source-lease" and "source_lease" in state
                or boundary == "promotion-lease" and "promotion_lease" in state
                or boundary == "finished" and state.get("phase") == "finished"):
            stop()
    with monkeypatch.context() as patch:
        patch.setattr(service.gate, "commit", crash_commit)
        patch.setattr(service.intake, "_save", crash_save)
        if boundary == "before-intake":
            patch.setattr(service.intake, "run", lambda *args, **kwargs: stop())
        with pytest.raises(SystemExit):
            case.run()
    assert stopped


@pytest.mark.parametrize("boundary", ["admission", "before-intake", "candidate", "draft", "source-lease",
    "source", "review", "alignment", "promotion-lease", "aligned", "finished"])
def test_original_operation_recovers_admission_and_saved_intake(hypothesis, monkeypatch, boundary):
    interrupt(hypothesis, monkeypatch, boundary)
    operation = hypothesis.operation()
    saved = hypothesis.service.jobs.intake_state(operation)
    hypothesis.restart()
    result = hypothesis.run()
    hypothesis.assert_complete(result)
    assert result["operation_id"] == operation
    recovered = hypothesis.service.jobs.intake_state(operation)
    for field in ("candidate", "review", "declaration_id", "alignment_id", "source_hash", "packet_artifact"):
        if field in saved:
            assert recovered[field] == saved[field]
    revision = hypothesis.service.store.head(hypothesis.proof)[0]
    assert hypothesis.run() == result
    assert hypothesis.service.store.verify_chain(hypothesis.proof) == revision == hypothesis.revision + 5


def test_committed_admission_releases_lease_left_by_process_death(hypothesis, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(hypothesis.service.store, "release_lease", lambda *args, **kwargs: False)
        interrupt(hypothesis, patch, "admission")
    assert len(hypothesis.service.store.active_leases(hypothesis.proof)) == 1
    hypothesis.restart()
    hypothesis.assert_complete(hypothesis.run())


@pytest.mark.parametrize("boundary,key", [("candidate", "draft_lease"), ("source-lease", "source_lease"),
    ("review", "review_lease"), ("promotion-lease", "promotion_lease")])
def test_expired_stage_lease_recovers_without_regenerating_outputs(hypothesis, monkeypatch, boundary, key):
    interrupt(hypothesis, monkeypatch, boundary)
    operation = hypothesis.operation()
    old = hypothesis.service.jobs.intake_state(operation)[key]
    hypothesis.restart()
    monkeypatch.setattr(hypothesis.service.store, "_clock_ns", lambda: int(old["expires_at"] * 1e9) + 1_000_000_000)
    hypothesis.assert_complete(hypothesis.run())
    assert hypothesis.service.jobs.intake_state(operation)[key]["fencing_token"] > old["fencing_token"]


def test_expired_stage_does_not_supersede_another_owner(hypothesis, monkeypatch):
    interrupt(hypothesis, monkeypatch, "candidate")
    old = hypothesis.service.jobs.intake_state(hypothesis.operation())["draft_lease"]
    hypothesis.restart()
    monkeypatch.setattr(hypothesis.service.store, "_clock_ns", lambda: int(old["expires_at"] * 1e9) + 1_000_000_000)
    other = hypothesis.service.store.claim_next(hypothesis.proof, [LeaseCandidate(old["selected_move_id"], {})],
        actor="other-formalizer", worker_class="formalizer", base_revision=old["base_revision"])
    result = hypothesis.run()
    assert result["phase"] == "failed" and "already leased" in result["summary"]
    assert hypothesis.service.store.active_leases(hypothesis.proof) == [other]
    assert len(hypothesis.llm.history) == 1


def test_stale_initial_revision_cannot_admit_a_claim(hypothesis):
    hypothesis.commit([UpsertNode("Claim", hypothesis.proof + "/c-99",
        {"status": "provisional", "claim_text": "Unrelated claim"})])
    result = hypothesis.run()
    assert result["phase"] == "failed" and "proof changed before hypothesis admission" in result["summary"]
    assert not hypothesis.service.view.edges_from(hypothesis.hypothesis, "PROMOTED_TO")
    assert not hypothesis.llm.history


@pytest.mark.parametrize("boundary", ["before-intake", "candidate", "review"])
def test_unrelated_revision_changes_are_not_rebased(hypothesis, monkeypatch, boundary):
    interrupt(hypothesis, monkeypatch, boundary)
    hypothesis.commit([UpsertNode("Claim", hypothesis.proof + "/c-99",
        {"status": "provisional", "claim_text": "Unrelated claim"})])
    revision, calls = hypothesis.service.store.head(hypothesis.proof)[0], len(hypothesis.llm.history)
    hypothesis.restart()
    result = hypothesis.run()
    assert result["phase"] == "failed", result
    assert "outside the hypothesis formalization operation" in result["summary"]
    assert hypothesis.service.store.head(hypothesis.proof)[0] == revision
    assert len(hypothesis.llm.history) == calls


def test_another_request_cannot_take_over_admitted_hypothesis(hypothesis, monkeypatch):
    interrupt(hypothesis, monkeypatch, "before-intake")
    request = {**hypothesis.request, "request_id": "another-operation", "arguments": {
        **hypothesis.request["arguments"], "expected_revision": str(hypothesis.service.store.head(hypothesis.proof)[0])}}
    result = hypothesis.service.submit(request)
    assert result["phase"] == "failed" and "belongs to another operation" in result["summary"]
    assert not hypothesis.llm.history
    hypothesis.assert_complete(hypothesis.run())


def test_changed_hypothesis_inputs_are_rejected(hypothesis, monkeypatch):
    interrupt(hypothesis, monkeypatch, "before-intake")
    hypothesis.commit([SetField("SpeculativeHypothesis", hypothesis.hypothesis, "statement", "False", prior="True")])
    result = hypothesis.run()
    assert result["phase"] == "failed" and "inputs changed" in result["summary"]
    assert not hypothesis.llm.history


def test_receiver_restart_reattaches_original_formalization_request(hypothesis, monkeypatch):
    receiver = ReferenceReceiver(hypothesis.service)
    async def start():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        request = client.prepare(HypothesisFormalize(hypothesis.proof, hypothesis.hypothesis), workspace_id="reference",
            request_id="sdk-hypothesis", preconditions=Preconditions(expected_revision=hypothesis.revision))
        handle = await client.submit(request)
        async with asyncio.timeout(5):
            while receiver._pending:
                await asyncio.sleep(.01)
        assert (await receiver.get_operation(handle.ref)).lifecycle == "running"
        return request, handle.ref
    try:
        with monkeypatch.context() as patch:
            patch.setattr(hypothesis.service.intake, "run",
                lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("crash before intake")))
            request, original = asyncio.run(start())
    finally:
        receiver.close()
    hypothesis.restart()
    receiver = ReferenceReceiver(hypothesis.service)
    async def recover():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.submit(request)
        assert handle.ref == original
        result = await handle.wait(timeout=10, poll_interval=.01)
        assert result.lifecycle == "succeeded", result.result
        hypothesis.assert_complete(result.result)
    try:
        asyncio.run(recover())
    finally:
        receiver.close()


def test_native_hypothesis_formalization_recovery(provider, tmp_path, monkeypatch):
    if not os.environ.get("MORK_LIBRARY") or not os.environ.get("ARTIFACT_TEST_DB_URL"):
        if os.environ.get("OMEGA_PERSISTENCE_REQUIRED") == "1":
            pytest.fail("hypothesis recovery requires native MORK and a dedicated PostgreSQL database")
        pytest.skip("native MORK and dedicated PostgreSQL required")
    if os.environ.get("OMEGA_HYPOTHESIS_RECOVERY_CASE") != "1":
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", f"{__file__}::test_native_hypothesis_formalization_recovery",
            "--basetemp", str(tmp_path / "native-process")], capture_output=True, text=True, timeout=60,
            env={**os.environ, "OMEGA_HYPOTHESIS_RECOVERY_CASE": "1"})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ["ARTIFACT_TEST_DB_URL"])
    case = HypothesisCase(tmp_path / "receiver", monkeypatch, profile="production")
    try:
        interrupt(case, monkeypatch, "alignment")
        case.restart()
        result = case.run()
        case.assert_complete(result)
        case.restart()
        assert case.run() == result
        assert case.service.store.verify_chain(case.proof) == case.revision + 5
    finally:
        case.service.close()
