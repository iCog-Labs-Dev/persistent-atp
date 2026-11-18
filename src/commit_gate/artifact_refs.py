"""Verify stored bytes before journalling content-addressed references."""

import hashlib
import json
from .ops import AddEdge, SetField, UpsertNode
from .reasons import Reason, Rejection


def check_artifact_references(proposal, view, artifacts):
    references = set()
    requires_store = False
    bindings = {}
    attestations = {}

    def collect(fields, node_id=None):
        nonlocal requires_store
        requires_store |= bool(fields.get("binding_artifact_hash"))
        if fields.get("binding_artifact_hash"):
            bindings.setdefault(fields["binding_artifact_hash"], []).append((fields, node_id))
        if fields.get("attestation_artifact_hash"):
            attestations.setdefault(fields["attestation_artifact_hash"], []).append(fields)
        for key in ("artifact_hash", "binding_artifact_hash", "attestation_artifact_hash"):
            if fields.get(key):
                references.add(fields[key])

    def collect_certificate(cert_id):
        cert = view.node(cert_id)
        if cert:
            collect(cert.fields, cert_id)
            for edge in view.edges_from(cert_id, "REPLAYED_BY"):
                replay = view.node(edge.dst_id)
                if replay:
                    collect(replay.fields)

    for op in proposal.ops:
        if isinstance(op, UpsertNode) and op.label in {"Certificate", "LeanReplay"}:
            collect(op.fields, op.node_id)
        elif isinstance(op, SetField):
            if op.label in {"Certificate", "LeanReplay"}:
                node = view.node(op.node_id)
                if node:
                    collect(node.fields, op.node_id)
                collect({op.field: op.value})
            if op.label == "Claim" and op.field == "status":
                for edge in view.edges_from(op.node_id, "PROVED_BY"):
                    collect_certificate(edge.dst_id)
        elif isinstance(op, AddEdge):
            if op.rel_type == "HAS_ARTIFACT":
                references.add(op.dst_id)
            elif op.rel_type in {"PROVED_BY", "REPLAYED_BY", "CERTIFIES"}:
                collect_certificate(op.dst_id if op.rel_type == "PROVED_BY" else op.src_id)
    if artifacts is None:
        if requires_store:
            return [Rejection(Reason.ARTIFACT_UNAVAILABLE,
                              "v2 certificates require CommitGate(..., artifacts=store)")]
        return []  # Compatibility for older providers without durable bindings.
    findings = []
    pending = set(references)
    checked = set()
    while pending:
        digest = pending.pop()
        if digest in checked:
            continue
        checked.add(digest)
        try:
            raw = artifacts.get(digest)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("stored bytes do not match their hash")
            if digest in bindings:
                binding = json.loads(raw)
                if binding.get("format") != "persistent-atp-formal-v2-binding-v1":
                    raise ValueError("unsupported v2 run binding")
                request, snapshot = binding["request"], binding["snapshot"]
                expected = snapshot["result"]["certificate"]["artifact_hash"]
                for fields, cert_id in bindings[digest]:
                    if (fields.get("artifact_hash") != expected
                            or fields.get("producer_run_id") != request["run_id"]
                            or fields.get("environment_hash") != request["environment_hash"]):
                        raise ValueError("certificate fields differ from the durable run binding")
                    declarations = {e.dst_id for e in view.edges_from(cert_id, "CERTIFIES")}
                    declarations.update(op.dst_id for op in proposal.ops if isinstance(op, AddEdge)
                                        and op.rel_type == "CERTIFIES" and op.src_id == cert_id)
                    if declarations and declarations != {request["formal_declaration_id"]}:
                        raise ValueError("certificate declaration differs from the durable run binding")
                    if fields.get("protocol_version") == 2 and any(
                        fields.get(key) != snapshot.get(key) for key in
                        ("request_digest", "input_artifact_hash", "source_artifact_hash")
                    ):
                        raise ValueError("v2 certificate fields differ from the stored input/source binding")
                pending.update((expected, snapshot["input_artifact_hash"], snapshot["source_artifact_hash"]))
            if digest in attestations:
                body = json.loads(raw)
                for fields in attestations[digest]:
                    if (body.get("status") != fields.get("status")
                            or body.get("certificate_hash") != fields.get("artifact_hash")
                            or body.get("environment_hash") != fields.get("environment_hash")):
                        raise ValueError("replay fields differ from the stored attestation")
                    if fields.get("protocol_version") == 2:
                        if any(fields.get(key) != body.get(key) for key in
                               ("run_id", "input_artifact_hash", "source_artifact_hash")):
                            raise ValueError("v2 replay changed the stored source/input binding")
                        if fields.get("status") == "verified" and (
                            fields.get("sorry_detected") is not False
                            or body.get("verification_cost", {}).get("sorry_detected") is not False
                        ):
                            raise ValueError("v2 replay lacks native no-sorry evidence")
        except Exception as exc:
            findings.append(Rejection(Reason.ARTIFACT_UNAVAILABLE,
                                      f"cannot commit artifact {digest}: {exc}"))
    return findings
