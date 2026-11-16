"""Real provider/native evidence admitted through real journal leases and gate."""

from dataclasses import replace
import time

import pytest

from commit_gate.canon import content_hash
from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from context_compiler.snapshot_provider import LeaseSnapshotProvider
from mathproof.formal_v2 import FormalV2Error, FormalV2HTTPATP
from mathproof.formal_v2_admission import search_proposal, replay_proposal, promotion_proposal


class Artifacts:
    """Fixture for the existing ArtifactStore.put/get seam, not another journal."""
    def __init__(self):
        self.values = {}

    def put(self, value):
        import hashlib
        key = "sha256:" + hashlib.sha256(value).hexdigest()
        self.values[key] = value
        return key

    def get(self, key):
        return self.values[key]


def lease(store, actor, worker_class, selected):
    return store.issue_lease("native", "lease-" + actor, actor=actor, worker_class=worker_class, selected_move_id=selected)


def proposal(lease, ops):
    return Proposal(proof_id=lease.proof_id, actor=lease.actor, worker_class=lease.worker_class,
        base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
        selected_move_id=lease.selected_move_id, ops=tuple(ops))


def release(store, value):
    store.release_lease(value.proof_id, value.lease_id, fencing_token=value.fencing_token)


def test_native_source_search_independent_replay_and_gate_promotion(tmp_path, monkeypatch):
    native = pytest.importorskip("maths_ai.formal.tests.test_frozen_native")
    provider_path = tmp_path / "provider"
    provider_path.mkdir()
    fixture = native.native.__wrapped__(provider_path)
    http, env_hash, _ = next(fixture)
    try:
        def transport(method, path, payload=None, *, raw=False, data=None):
            response = http.request(method, path, json=payload, content=data)
            if response.status_code >= 400:
                raise FormalV2Error(response.text, status_code=response.status_code)
            return response.content if raw else response.json()
        search = FormalV2HTTPATP("http://fixture", timeout=60)
        monkeypatch.setattr(search, "_call", transport)
        source = b"namespace Demo\ntheorem goal (P : Prop) (h : P) : P := by exact DOES_NOT_EXIST\nend Demo\n"
        source_hash = search.put_artifact(source)
        artifacts, view, store = Artifacts(), MemoryView(), JournalStore()
        gate = CommitGate(view, store)
        def committed():
            # Exercise journal-overlay recovery with a deliberately unprojected view.
            inspection = replace(worker, base_revision=store.head("native")[0])
            return LeaseSnapshotProvider(view, store).for_lease(inspection).view
        seed = lease(store, "seed", "coordinator", "native/c-1")
        seed_ops = [
            UpsertNode("Claim", "native/c-1", {"status": "provisional"}),
            UpsertNode("FormalDeclaration", "native/fd-1", {"status": "searching", "lean_name": "Demo.goal",
                "lean_source_artifact": source_hash}),
            UpsertNode("Environment", "native/env-1", {"environment_hash": env_hash, "provider_environment_id": "init-4.10-rc1"}),
            UpsertNode("FormalRun", "native/fr-1", {"status": "searching", "actor": "searcher", "environment_hash": env_hash}),
            UpsertNode("Alignment", "native/al-1", {"actor": "seed", "lifecycle": "reviewed", "verdict": "aligned"}),
            AddEdge("SEARCHES", "native/fr-1", "native/fd-1", "native/e-search"),
            AddEdge("PINNED_ENVIRONMENT", "native/fd-1", "native/env-1", "native/e-pin"),
            AddEdge("ALIGNS_CLAIM", "native/al-1", "native/c-1", "native/e-align-c"),
            AddEdge("ALIGNS_DECLARATION", "native/al-1", "native/fd-1", "native/e-align-d"),
        ]
        outcome = gate.commit(proposal(seed, seed_ops))
        assert outcome.accepted, outcome.to_dict()
        release(store, seed)
        worker = lease(store, "searcher", "formal-atp", "native/fr-1")
        body = native.request(env_hash, input={"kind": "source", "artifact_hash": source_hash,
            "declaration": "Demo.goal", "starting_state": "binders-introduced"})
        body.update(base_revision=worker.base_revision, lease_id=worker.lease_id, fencing_token=worker.fencing_token)
        search.formal_search_start(body)
        deadline = time.monotonic() + 60
        while search.result(body["run_id"]) is None:
            assert time.monotonic() < deadline
            time.sleep(.03)
        candidate = search_proposal(search, body["run_id"], worker, committed(), artifacts)
        assert not gate.commit(replace(candidate, fencing_token=worker.fencing_token + 1)).accepted
        assert not gate.commit(replace(candidate, base_revision=worker.base_revision - 1)).accepted
        with pytest.raises(FormalV2Error, match="lease/revision"):
            search_proposal(search, body["run_id"], replace(worker, fencing_token=worker.fencing_token + 1), committed(), artifacts)
        bad_ops = tuple(replace(op, fields={**op.fields, "subgoal_count": 1})
                        if isinstance(op, UpsertNode) and op.label == "TacticApplication" else op for op in candidate.ops)
        assert not gate.commit(replace(candidate, ops=bad_ops)).accepted
        outcome = gate.commit(candidate)
        assert outcome.accepted, outcome.to_dict()
        assert committed().node("native/c-1").fields["status"] == "provisional"
        cert = next(op.node_id for op in candidate.ops if isinstance(op, UpsertNode) and op.label == "Certificate")
        assert committed().node(cert).fields["status"] == "replay-pending"
        release(store, worker)

        checker = lease(store, "checker", "replayer", cert)
        independent = FormalV2HTTPATP("http://fixture", timeout=60)
        monkeypatch.setattr(independent, "_call", transport)
        with pytest.raises(FormalV2Error, match="independent replayer"):
            replay_proposal(independent, replace(checker, actor="searcher"), committed(), artifacts,
                            certificate_id=cert, max_steps=32, wall_clock_ms=15000)
        def missing_audit(*args, **kwargs):
            value = transport(*args, **kwargs)
            if args[1] == "/formal-replay":
                value["verification_cost"].pop("sorry_detected", None)
            return value
        monkeypatch.setattr(independent, "_call", missing_audit)
        with pytest.raises(FormalV2Error, match="no-sorry"):
            replay_proposal(independent, checker, committed(), artifacts, certificate_id=cert, max_steps=32, wall_clock_ms=15000)
        assert committed().node(cert).fields["status"] == "replay-pending"
        monkeypatch.setattr(independent, "_call", transport)
        checked = replay_proposal(independent, checker, committed(), artifacts,
                                  certificate_id=cert, max_steps=32, wall_clock_ms=15000)
        outcome = gate.commit(checked)
        assert outcome.accepted, outcome.to_dict()
        assert committed().node(cert).fields["status"] == "replay-accepted"
        assert committed().node("native/c-1").fields["status"] == "provisional"
        release(store, checker)

        coordinator = lease(store, "coordinator", "coordinator", "native/c-1")
        promotion = promotion_proposal(coordinator, committed(), claim_id="native/c-1", certificate_id=cert)
        no_alignment = replace(promotion, ops=(RemoveEdge("ALIGNS_CLAIM", "native/e-align-c"), *promotion.ops))
        assert not gate.commit(no_alignment).accepted
        changed_source = replace(promotion, ops=(SetField("FormalDeclaration", "native/fd-1", "lean_source_artifact",
            "sha256:" + "a" * 64, prior=source_hash), *promotion.ops))
        assert not gate.commit(changed_source).accepted
        outcome = gate.commit(promotion)
        assert outcome.accepted, outcome.to_dict()
        assert committed().node("native/c-1").fields["status"] == "lean-verified"
        assert store.head("native")[0] == 4
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
