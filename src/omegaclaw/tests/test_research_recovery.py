"""Research recovery against real journals/gates and controlled LLM output."""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from alignment.llm_client import MockLLMClient
from commit_gate.canon import canonical_json
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.store import LeaseCandidate
from context_compiler.obstruction_runtime import ObstructionLeaseCompiler, ObstructionRuntimeConfig
from omegaclaw.contracts import ContractError
from omegaclaw.receiver import ReferenceReceiver
from omegaclaw.research_runtime import ResearchWorkflow
from omegaclaw.service import OperationService
from omegaclaw.tests.test_research_runtime import candidate
from persistent_atp_client import AsyncClient
from persistent_atp_client.adapters.local import LocalReceiverAdapter


def commit(service, ops, *, selected="p1/obs-1"):
    lease = service.store.claim_next("p1", [LeaseCandidate(selected, {})],
        actor="fixture-coordinator", worker_class="coordinator",
        base_revision=service.store.head("p1")[0], ttl_seconds=120)
    assert lease is not None
    try:
        result = service.gate.commit(Proposal("p1", lease.actor, lease.worker_class, ops=tuple(ops),
            base_revision=lease.base_revision, lease_id=lease.lease_id,
            fencing_token=lease.fencing_token, selected_move_id=selected))
        assert result.accepted, result.to_dict()
        service.catch_up("p1")
    finally:
        service.store.release_lease("p1", lease.lease_id, fencing_token=lease.fencing_token)


class ResearchCase:
    def __init__(self, root, *, profile="reference"):
        self.root, self.profile = root, profile
        self.llm = MockLLMClient(responses=[{"candidates": [candidate("First lemma"), candidate("Second lemma")]}])
        self.open()
        self.service.submit({"schema_version": 1, "request_id": "create", "workspace": "reference",
            "operation": "proof-create", "arguments": {"spec": json.dumps({
                "proof_id": "p1", "title": "Research recovery fixture", "literal_target": "True"})}})
        self.seed_packet()
        request = {"schema_version": 1, "request_id": "research", "workspace": "reference",
            "operation": "lemma-bridge", "arguments": {"proof": "p1", "obstruction": "p1/obs-1"}}
        record, _ = self.service.jobs.submit(request)
        self.operation_id = record["operation_id"]
        self.revision = self.service.store.head("p1")[0]

    def open(self, *, provider="fixture"):
        self.service = OperationService(self.root, profile=self.profile,
            artifact_db_url=os.environ.get("ARTIFACT_TEST_DB_URL") if self.profile == "production" else None)
        self.workflow = ResearchWorkflow(self.service, SimpleNamespace(provider=provider, timeout=5),
            llm_factory=lambda: self.llm)
        self.service.research = self.workflow

    def restart(self, **kwargs):
        self.service.close()
        self.open(**kwargs)

    def run(self):
        return self.workflow.run("p1", "p1/obs-1", self.operation_id)

    def state(self):
        return self.service.jobs.research_state(self.operation_id)

    def seed_packet(self):
        service = self.service
        environment = service.artifacts.put(b"controlled-environment")
        exact = service.artifacts.put(b"controlled-root-context")
        diagnostic = service.artifacts.put(b"Search exhausted; no refutation is asserted.")
        obstruction = {"obstruction_id": "p1/obs-1", "kind": "search-policy", "formal_run_id": "p1/fr-1",
            "formal_state_ids": ["p1/fs-1"], "environment_hash": environment,
            "minimal_state_artifact": exact, "diagnostic_artifacts": [diagnostic]}
        commit(service, [
            UpsertNode("Environment", "p1/env-1", {"environment_hash": environment}),
            UpsertNode("FormalRun", "p1/fr-1", {"status": "stagnated", "environment_hash": environment}),
            UpsertNode("FormalState", "p1/fs-1", {"status": "open", "hypotheses": [],
                "target": "True", "exact_state_hash": exact}),
            UpsertNode("Obstruction", "p1/obs-1", obstruction),
            AddEdge("RAISED_OBSTRUCTION", "p1/fr-1", "p1/obs-1", "p1/e-raised"),
            AddEdge("RAN_UNDER", "p1/fr-1", "p1/env-1", "p1/e-env"),
            AddEdge("AT_STATE", "p1/obs-1", "p1/fs-1", "p1/e-state"),
            AddEdge("RESOLVES", "p1/c-1", "p1/obs-1", "p1/e-kernel"),
        ])
        lease = service.store.claim_next("p1", [LeaseCandidate("p1/obs-1", {})],
            actor="fixture-compiler", worker_class="coordinator",
            base_revision=service.store.head("p1")[0], ttl_seconds=120)
        try:
            dispatch = ObstructionLeaseCompiler(service.view, service.store, service.artifacts,
                ObstructionRuntimeConfig(max_tokens=8192)).compile(lease)
        finally:
            service.store.release_lease("p1", lease.lease_id, fencing_token=lease.fencing_token)
        packet = service.artifacts.put(dispatch.packet.content.encode())
        manifest = service.artifacts.put(canonical_json(dispatch.manifest))
        payload = service.artifacts.put(canonical_json(dispatch.worker_input))
        commit(service, [UpsertNode("Artifact", packet, {"artifact_hash": packet,
            "report_artifact": manifest, "result_artifact": payload, "media_type": "text/plain; charset=utf-8"}),
            AddEdge("HAS_ARTIFACT", "p1/obs-1", packet, "p1/e-packet")])


@pytest.fixture
def research(tmp_path):
    case = ResearchCase(tmp_path)
    try:
        yield case
    finally:
        case.service.close()


def test_legacy_saved_response_recovers_without_reclassification(research, monkeypatch):
    legacy = {key: value for key, value in candidate('Legacy suggestion').items()
              if key not in {'candidate_kind', 'formalization_spec'}}
    research.llm.responses = [{'candidates': [legacy]}]
    validate, save = ResearchWorkflow._validate_response, research.workflow._save
    def crash(operation, state):
        save(operation, state)
        if state.get('phase') == 'candidate-saved':
            raise SystemExit('old worker interrupted after saving an unclassified response')
    # Produce the saved state accepted by the earlier worker, then recover it
    # with today's strict fresh-response validation and unchanged identities.
    with monkeypatch.context() as patch:
        patch.setattr(ResearchWorkflow, '_validate_response', staticmethod(
            lambda response, **kwargs: validate(response, allow_legacy=True)))
        patch.setattr(research.workflow, '_save', crash)
        with pytest.raises(SystemExit): research.run()
    research.restart()
    outcome = research.run()
    node = research.service.view.node(outcome['hypothesis_ids'][0])
    assert node.fields['statement'] == legacy['statement']
    assert 'candidate_kind' not in node.fields and 'formalization_spec' not in node.fields
    assert len(research.llm.history) == 1
    assert research.service.store.verify_chain('p1') == research.revision + 1


def interrupt_before_commit(case, monkeypatch, *, phase="proposal-ready"):
    save = case.workflow._save
    def crash(operation, state):
        save(operation, state)
        if state.get("phase") == phase:
            raise SystemExit("controlled research interruption")
    with monkeypatch.context() as patch:
        patch.setattr(case.workflow, "_save", crash)
        with pytest.raises(SystemExit):
            case.run()
    return case.state()


@pytest.mark.parametrize("phase", ["candidate-saved", "proposal-ready"])
def test_saved_response_survives_restart_without_duplicate_generation(research, monkeypatch, phase):
    saved = interrupt_before_commit(research, monkeypatch, phase=phase)
    research.restart(provider="replacement-provider")
    outcome = research.run()
    assert len(research.llm.history) == 1
    assert len(outcome["hypothesis_ids"]) == 2
    assert research.service.store.verify_chain("p1") == research.revision + 1
    result = json.loads(research.service.artifacts.get(outcome["result_artifact"]))
    assert result["provider"] == "fixture"
    if phase == "proposal-ready":
        assert outcome["result_artifact"] == saved["result_artifact"]
        assert outcome["hypothesis_ids"] == saved["hypothesis_ids"]
    assert research.run() == outcome
    assert research.service.store.head("p1")[0] == research.revision + 1
    assert not research.service.store.active_leases("p1")


def test_legacy_saved_result_preserves_recorded_provider(research, monkeypatch):
    saved = interrupt_before_commit(research, monkeypatch)
    saved.pop("provider")  # Operations prepared before provider pinning existed.
    research.service.jobs.save_research(research.operation_id, saved)
    research.restart(provider="replacement-provider")
    assert research.run()["result_artifact"] == saved["result_artifact"]
    assert len(research.llm.history) == 1


def test_provider_change_before_generation_requires_reconciliation(research, monkeypatch):
    def crash():
        raise SystemExit("crash before generation")
    with monkeypatch.context() as patch:
        patch.setattr(research.workflow, "_llm_factory", crash)
        with pytest.raises(SystemExit):
            research.run()
    research.restart(provider="replacement-provider")
    with pytest.raises(ContractError, match="provider changed before generation"):
        research.run()
    assert not research.llm.history
    assert research.service.store.head("p1")[0] == research.revision
    assert not research.service.store.active_leases("p1")


def test_journal_commit_is_reconciled_before_projection_and_completion(research, monkeypatch):
    gate_commit = research.service.gate.commit
    def crash(proposal):
        result = gate_commit(proposal)
        assert result.accepted
        raise SystemExit("crash after durable journal append")
    with monkeypatch.context() as patch:
        patch.setattr(research.service.gate, "commit", crash)
        with pytest.raises(SystemExit):
            research.run()
    saved = research.state()
    assert saved["phase"] == "proposal-ready"
    assert all(research.service.view.node(node_id) is None for node_id in saved["hypothesis_ids"])
    # Reuse the lagging view to ensure run() catches it up before reconciliation.
    outcome = research.run()
    assert outcome["hypothesis_ids"] == saved["hypothesis_ids"]
    assert len(research.llm.history) == 1
    assert research.service.store.verify_chain("p1") == research.revision + 1


@pytest.mark.parametrize("case", ["active", "expired", "competing", "stale"])
def test_interrupted_lease_respects_fencing_ownership_and_revision(research, monkeypatch, case):
    saved = interrupt_before_commit(research, monkeypatch)
    old = saved["lease"]
    # Simulate the active lease a process death would leave behind, then expire it.
    active = research.service.store.claim_next("p1", [LeaseCandidate("p1/obs-1", {})],
        actor="omega-research-worker", worker_class="llm-research", base_revision=research.revision, ttl_seconds=1)
    saved["lease"] = active.to_dict()
    research.service.jobs.save_research(research.operation_id, saved)
    if case != "active":
        now = research.service.store._clock_ns()
        monkeypatch.setattr(research.service.store, "_clock_ns", lambda: now + 2_000_000_000)
        assert not research.service.store.active_leases("p1")
    if case == "competing":
        other = research.service.store.claim_next("p1", [LeaseCandidate("p1/obs-1", {})],
            actor="other-worker", worker_class="llm-research", base_revision=research.revision, ttl_seconds=120)
        with pytest.raises(ContractError, match="already leased"):
            research.run()
        assert research.service.store.active_leases("p1") == [other]
    elif case == "stale":
        commit(research.service, [UpsertNode("Claim", "p1/c-2",
            {"status": "provisional", "claim_text": "Another research claim"})], selected="p1/c-1")
        with pytest.raises(ContractError, match="research inputs changed"):
            research.run()
    elif case == "expired":
        # Check the old generation before any new graph revision can mask it.
        stale = research.service.gate.commit(Proposal("p1", active.actor, active.worker_class,
            ops=research.workflow._saved_proposal("p1", "p1/obs-1", research.operation_id, saved),
            base_revision=active.base_revision, lease_id=active.lease_id,
            fencing_token=active.fencing_token, selected_move_id=active.selected_move_id))
        assert not stale.accepted
        assert Reason.LEASE_EXPIRED in {item.reason for item in stale.rejections}
        outcome = research.run()
        assert outcome["result_artifact"] == saved["result_artifact"]
        assert research.state()["lease"]["fencing_token"] > active.fencing_token > old["fencing_token"]
    else:
        assert research.run()["result_artifact"] == saved["result_artifact"]
        assert research.state()["lease"]["lease_id"] == active.lease_id
    assert len(research.llm.history) == 1


@pytest.mark.parametrize("case", ["partial", "conflict", "corrupt-result", "candidate-id", "wrong-operation"])
def test_saved_proposal_integrity_failures_do_not_regenerate_or_commit(research, monkeypatch, case):
    saved = interrupt_before_commit(research, monkeypatch)
    ops = research.workflow._saved_proposal("p1", "p1/obs-1", research.operation_id, saved)
    if case == "partial":
        commit(research.service, ops[:1])
        message = "only some hypotheses"
    elif case == "conflict":
        commit(research.service, ops)
        commit(research.service, [SetField("SpeculativeHypothesis", ops[0].node_id,
            "statement", "Conflicting statement", prior=ops[0].fields["statement"])])
        message = "conflicts with committed hypotheses"
    elif case == "corrupt-result":
        get = research.service.artifacts.get
        monkeypatch.setattr(research.service.artifacts, "get",
            lambda pointer: b"corrupt" if pointer == saved["result_artifact"] else get(pointer))
        message = "missing or corrupt"
    elif case == "candidate-id":
        saved["hypothesis_ids"][0] = "p1/hyp-" + "0" * 32
        research.service.jobs.save_research(research.operation_id, saved)
        message = "candidate identities differ"
    else:
        result = json.loads(research.service.artifacts.get(saved["result_artifact"]))
        result["operation_id"] = "other-operation"
        saved["result_artifact"] = research.service.artifacts.put(canonical_json(result))
        research.service.jobs.save_research(research.operation_id, saved)
        message = "differs from its operation"
    revision = research.service.store.head("p1")[0]
    with pytest.raises(ContractError, match=message):
        research.run()
    assert research.service.store.head("p1")[0] == revision
    assert len(research.llm.history) == 1


def test_receiver_restart_reconciles_saved_research_request(research, monkeypatch):
    save = ResearchWorkflow._save
    interrupted = []
    def crash(workflow, operation, state):
        save(workflow, operation, state)
        if state.get("phase") == "proposal-ready" and not interrupted:
            interrupted.append(dict(state))
            raise SystemExit("receiver worker interrupted")
    receiver = ReferenceReceiver(research.service)
    async def submit():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.lemma_bridge("p1", "p1/obs-1", workspace_id="reference", request_id="receiver-research")
        async with asyncio.timeout(5):
            while receiver._pending:
                await asyncio.sleep(.01)
        assert (await receiver.get_operation(handle.ref)).lifecycle == "running"
        return handle.ref
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ResearchWorkflow, "_save", crash)
            original = asyncio.run(submit())
    finally:
        receiver.close()
    research.restart()
    receiver = ReferenceReceiver(research.service)
    async def recover():
        client = AsyncClient(LocalReceiverAdapter(receiver))
        handle = await client.lemma_bridge("p1", "p1/obs-1", workspace_id="reference", request_id="receiver-research")
        assert handle.ref == original
        snapshot = await handle.wait(timeout=5, poll_interval=.01)
        assert snapshot.lifecycle == "succeeded", snapshot.result
        assert snapshot.result["hypothesis_ids"] == interrupted[0]["hypothesis_ids"]
        assert snapshot.result["result_artifact"] == interrupted[0]["result_artifact"]
    try:
        asyncio.run(recover())
    finally:
        receiver.close()
    assert len(research.llm.history) == 1
    assert research.service.store.verify_chain("p1") == research.revision + 1


def test_native_research_recovery(tmp_path, monkeypatch):
    if not os.environ.get("MORK_LIBRARY") or not os.environ.get("ARTIFACT_TEST_DB_URL"):
        if os.environ.get("OMEGA_PERSISTENCE_REQUIRED") == "1":
            pytest.fail("research persistence requires native MORK and a dedicated PostgreSQL database")
        pytest.skip("native MORK and a dedicated PostgreSQL database required")
    if os.environ.get("OMEGA_RESEARCH_RECOVERY_CASE") != "1":
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", f"{__file__}::test_native_research_recovery",
            "--basetemp", str(tmp_path / "native-process")], capture_output=True, text=True, timeout=60,
            env={**os.environ, "OMEGA_RESEARCH_RECOVERY_CASE": "1"})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from artifacts.schema import apply_schema
    apply_schema(os.environ["ARTIFACT_TEST_DB_URL"])
    research = ResearchCase(tmp_path / "receiver", profile="production")
    try:
        saved = interrupt_before_commit(research, monkeypatch)
        research.restart(provider="replacement-provider")
        outcome = research.run()
        research.restart()
        for op in research.workflow._saved_proposal("p1", "p1/obs-1", research.operation_id, research.state()):
            node = research.service.view.node(op.node_id)
            assert node.label == op.label
            assert dict(node.fields) == dict(op.fields)
        assert research.run() == outcome
        assert outcome["result_artifact"] == saved["result_artifact"]
        assert len(research.llm.history) == 1
        assert research.service.store.verify_chain("p1") == research.revision + 1
    finally:
        research.service.close()
