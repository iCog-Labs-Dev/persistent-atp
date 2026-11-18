"""Adversarial promotion proposals over adapter evidence and a durable journal.

Historical evidence is journalled before opening the gate, modelling records
accepted before the replay-binding fix. Negative tests assert atomic rejection,
not merely that a helper raises or an HTTP request fails.
"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from commit_gate.canon import canonical_json
from commit_gate.gate import CommitGate
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from commit_gate.tests.test_artifact_refs import DiskArtifacts
from commit_gate.tests.test_soundness_gates import wire
from mathproof.tests.test_maths_ai_http import attestation, proved


class PromotionCase:
    def __init__(self, root, *, kind="source", backend="mcts-actor-critic"):
        atp, self.request, result, run, _ = proved(kind, backend)
        self.artifacts = DiskArtifacts(root / "blobs")
        for raw in atp.artifacts.blobs.values():
            self.artifacts.put(raw)
        self.body = attestation(self.request, run)
        self.certificate = {**result["certificate"], "status": "replay-accepted"}
        self.path = str(root / "journal.sqlite")
        self.journal = JournalStore(self.path)
        self.token = self.journal.acquire_lease(
            "example", "lease", actor="coordinator", worker_class="coordinator",
        )
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.journal, artifacts=self.artifacts)

    def evidence(self, body, *, omit_hash=False):
        fields = {"artifact_hash": self.certificate["artifact_hash"]}
        if not omit_hash:
            fields["attestation_artifact_hash"] = self.artifacts.put(canonical_json(body))
        graph = MemoryView()
        wire(
            graph, claim_id=self.request["claim_id"], cert_id="example/cert-1",
            replay_id="example/replay-1", alignment_id="example/alignment-1",
            declaration_id=self.request["formal_declaration_id"],
            run_id=self.request["run_id"], environment_id="example/env-1",
            cert_fields={**self.certificate, "environment_hash": self.request["environment_hash"]},
            replay_fields={**fields, "environment_hash": self.request["environment_hash"]},
        )
        for node_id in (self.request["run_id"], "example/env-1"):
            graph.set_field(node_id, "environment_hash", self.request["environment_hash"])
        return [UpsertNode(n.label, n.node_id, dict(n.fields))
                for n in graph.list_nodes("example")] + [
            AddEdge(e.rel_type, e.src_id, e.dst_id, e.edge_id, dict(e.fields))
            for e in graph.list_edges("example")
        ]

    def seed(self, ops):
        # Deliberately bypass the new validator only for historical records.
        proposal = self.proposal(ops)
        self.journal.append(proposal.to_dict())
        self.project()

    def project(self):
        while self.view.snapshot("example")[0] < self.journal.head("example")[0]:
            self.view.apply_journal_event(
                self.journal, "example", self.view.snapshot("example")[0] + 1,
            )

    def proposal(self, ops):
        return Proposal(
            proof_id="example", actor="coordinator", worker_class="coordinator",
            ops=tuple(ops), base_revision=self.journal.head("example")[0],
            lease_id="lease", fencing_token=self.token,
        )

    def promotion(self):
        return SetField("Claim", self.request["claim_id"], "status", "lean-verified",
                        prior="formally-closed")

    def prepare(self, body, placement, *, omit_hash=False):
        ops = self.evidence(body, omit_hash=omit_hash)
        if placement == "existing":
            self.seed(ops)
            return [self.promotion()]
        if placement == "replay-in-proposal":
            replay_ops = [op for op in ops if (
                isinstance(op, UpsertNode) and op.label == "LeanReplay"
                or isinstance(op, AddEdge) and op.rel_type in {"REPLAYED_BY", "REPLAY_ENVIRONMENT"}
            )]
            self.seed([op for op in ops if op not in replay_ops])
            return replay_ops + [self.promotion()]
        assert placement == "all-in-proposal"
        # Include creation directly at the promoted status; no SetField shortcut.
        return [replace(op, fields={**op.fields, "status": "lean-verified"})
                if isinstance(op, UpsertNode) and op.label == "Claim" else op
                for op in ops]

    def assert_rejected(self, ops, reason=Reason.ARTIFACT_UNAVAILABLE):
        head = self.journal.head("example")
        before = (self.view.list_nodes("example"), self.view.list_edges("example"))
        result = self.gate.commit(self.proposal(ops))
        assert not result.accepted, result
        assert reason in {r.reason for r in result.rejections}, result.rejections
        assert self.journal.head("example") == head
        assert self.view.snapshot("example")[0] == head[0]
        assert (self.view.list_nodes("example"), self.view.list_edges("example")) == before

    def restart(self):
        self.journal.close()
        self.journal = JournalStore(self.path)
        self.artifacts = DiskArtifacts(self.artifacts.path)
        self.view = MemoryView()
        self.gate = CommitGate(self.view, self.journal, artifacts=self.artifacts)
        self.project()


PLACEMENTS = ["existing", "replay-in-proposal", "all-in-proposal"]


@pytest.fixture
def case(tmp_path):
    value = PromotionCase(tmp_path)
    yield value
    value.journal.close()


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_verified_replay_without_attestation_is_never_authority(case, placement):
    case.assert_rejected(case.prepare(case.body, placement, omit_hash=True))


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("field,value", [
    ("run_id", "example/fr-99"),
    ("certificate_hash", "sha256:" + "01" * 32),
    ("input_artifact_hash", "sha256:" + "02" * 32),
    ("source_artifact_hash", "sha256:" + "03" * 32),
    ("source_origin", "generated-from-text"),
    ("backend", "accepted-gnn"),
    ("environment_hash", "sha256:" + "04" * 32),
    ("status", "inconclusive"),
])
def test_attestation_from_another_identity_cannot_promote(case, placement, field, value):
    body = {**case.body, field: value}
    case.assert_rejected(case.prepare(body, placement))


@pytest.mark.parametrize("field", [
    "run_id", "certificate_hash", "input_artifact_hash", "source_artifact_hash",
    "source_origin", "backend", "environment_hash", "verification_cost", "protocol_version",
])
def test_incomplete_attestation_is_rejected_after_restart(case, field):
    body = dict(case.body)
    del body[field]
    ops = case.prepare(body, "existing")
    case.restart()
    case.assert_rejected(ops)


@pytest.mark.parametrize("change", ["protocol-v1", "boolean-version", "float-version", "unknown-field", "cost-type"])
def test_schema_invalid_attestation_with_matching_identities_is_rejected(case, change):
    body = deepcopy(case.body)
    if change == "unknown-field":
        body["allow_unchecked"] = True
    elif change == "cost-type":
        body["verification_cost"] = "one step"
    else:
        body["protocol_version"] = {"protocol-v1": 1, "boolean-version": True, "float-version": 2.0}[change]
    case.assert_rejected(case.prepare(body, "existing"))


@pytest.mark.parametrize("failure", ["missing", "corrupt", "invalid-json", "non-object", "no-store"])
def test_stored_attestation_failure_cannot_change_journal_or_projection(case, failure):
    evidence = case.evidence(case.body)
    replay = next(op for op in evidence if isinstance(op, UpsertNode) and op.label == "LeanReplay")
    digest = replay.fields["attestation_artifact_hash"]
    if failure == "missing":
        (case.artifacts.path / digest).unlink()
    elif failure == "corrupt":
        (case.artifacts.path / digest).write_bytes(b"tampered")
    elif failure in {"invalid-json", "non-object"}:
        digest = case.artifacts.put(b"not json" if failure == "invalid-json" else b"[]")
        # Historical journals can contain syntactically bad artifacts too.
        evidence = [replace(op, fields={**op.fields, "attestation_artifact_hash": digest})
                    if op is replay else op for op in evidence]
    case.seed(evidence)
    if failure == "no-store":
        case.gate = CommitGate(case.view, case.journal)
    case.assert_rejected([case.promotion()])


def test_removing_valid_replay_cannot_leave_mismatched_replay_as_authority(case):
    case.seed(case.evidence(case.body))
    bad = {**case.body, "run_id": "example/fr-99"}
    replay_fields = dict(case.view.node("example/replay-1").fields)
    replay_fields["attestation_artifact_hash"] = case.artifacts.put(canonical_json(bad))
    case.seed([
        UpsertNode("LeanReplay", "example/replay-2", replay_fields),
        AddEdge("REPLAYED_BY", "example/cert-1", "example/replay-2", "example/bad-replay"),
        AddEdge("REPLAY_ENVIRONMENT", "example/replay-2", "example/env-1", "example/bad-env"),
    ])
    edge = case.view.edges_to("example/replay-1", "REPLAYED_BY")[0]
    case.assert_rejected([RemoveEdge(edge.rel_type, edge.edge_id), case.promotion()])


@pytest.mark.parametrize("field", ["input_artifact_hash", "source_artifact_hash", "source_origin", "backend"])
def test_changing_binding_and_attestation_together_cannot_rebind_certificate(case, field):
    binding = json.loads(case.artifacts.get(case.certificate["binding_artifact_hash"]))
    body = deepcopy(case.body)
    value = (case.artifacts.put(b"unrelated input or source") if field.endswith("hash")
             else "generated-from-text" if field == "source_origin" else "accepted-gnn")
    body[field] = value
    if field == "backend":
        binding["request"]["backend"]["name"] = value
    else:
        binding["snapshot"][field] = value
    case.certificate["binding_artifact_hash"] = case.artifacts.put(canonical_json(binding))
    case.assert_rejected(case.prepare(body, "all-in-proposal"))


def test_v2_certificate_cannot_downgrade_to_unbound_legacy_evidence(case):
    del case.certificate["binding_artifact_hash"]
    case.assert_rejected(case.prepare(case.body, "all-in-proposal", omit_hash=True))


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_valid_evidence_is_accepted_in_each_atomic_placement(case, placement):
    result = case.gate.commit(case.proposal(case.prepare(case.body, placement)))
    assert result.accepted, result.rejections


@pytest.mark.parametrize("field,value", [
    ("actor", "producer-alpha"), ("actor", "coordinator"),
    ("sorry_detected", True), ("sorry_detected", None),
])
def test_bound_attestation_does_not_bypass_independence_or_sorry_checks(case, field, value):
    ops = case.prepare(case.body, "replay-in-proposal")
    ops = [replace(op, fields={**op.fields, field: value})
           if isinstance(op, UpsertNode) and op.label == "LeanReplay" else op
           for op in ops]
    case.assert_rejected(ops, Reason.PROMOTION_WITHOUT_REPLAY)


def test_bound_attestation_does_not_bypass_fencing(case):
    ops = case.prepare(case.body, "existing")
    case.token -= 1
    case.assert_rejected(ops, Reason.FENCING_TOKEN_SUPERSEDED)


def test_unprojected_journal_cannot_hide_unbound_replay(case):
    ops = case.prepare({**case.body, "run_id": "example/fr-99"}, "existing")
    case.view = MemoryView()
    case.gate = CommitGate(case.view, case.journal, artifacts=case.artifacts)
    head = case.journal.head("example")
    result = case.gate.commit(case.proposal(ops))
    assert not result.accepted
    assert Reason.ARTIFACT_UNAVAILABLE in {r.reason for r in result.rejections}
    assert case.journal.head("example") == head
    assert case.view.list_nodes("example") == ()


@pytest.mark.parametrize("kind,backend", [
    ("source", "mcts-actor-critic"), ("goal", "mcts-actor-critic"), ("goal", "accepted-gnn"),
])
def test_bound_promotion_survives_full_journal_reconstruction(tmp_path, kind, backend):
    case = PromotionCase(tmp_path, kind=kind, backend=backend)
    try:
        case.seed(case.evidence(case.body))
        case.restart()
        result = case.gate.commit(case.proposal([case.promotion()]))
        assert result.accepted, result.rejections
        case.restart()
        assert case.view.node(case.request["claim_id"]).fields["status"] == "lean-verified"
        replay = case.view.node("example/replay-1")
        assert json.loads(case.artifacts.get(replay.fields["attestation_artifact_hash"])) == case.body
        assert case.journal.head("example")[0] == case.view.snapshot("example")[0] == 2
    finally:
        case.journal.close()
