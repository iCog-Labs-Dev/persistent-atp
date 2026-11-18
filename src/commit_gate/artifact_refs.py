"""Verify durable evidence against the graph a proposal would commit."""

import hashlib
import json

from .ops import AddEdge, RemoveEdge, SetField, UpsertNode
from .reasons import Reason, Rejection
from .state import JournalOverlayView


def _object(raw):
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("evidence must be a JSON object")
    return body


def _check_v2_replay(body, fields, expected):
    # Import at execution time: the protocol package also imports gate types.
    from mathproof.formal_v2 import validate

    errors = validate("formal-replay-attestation", body)
    if errors or body.get("protocol_version") != 2:
        raise ValueError(f"invalid v2 replay attestation: {errors or 'missing protocol_version 2'}")
    for key, value in expected.items():
        if body.get(key) != value:
            raise ValueError(f"replay attestation {key} differs from the durable run binding")
    if (body["status"] != fields.get("status")
            or body["certificate_hash"] != fields.get("artifact_hash")
            or body["environment_hash"] != fields.get("environment_hash")):
        raise ValueError("replay fields differ from the stored attestation")
    if body.get("verification_cost", {}).get("sorry_detected") is not False:
        raise ValueError("v2 replay lacks native no-sorry evidence")
    if fields.get("protocol_version") == 2:
        if any(fields.get(key) != body.get(key) for key in
               ("run_id", "input_artifact_hash", "source_artifact_hash")):
            raise ValueError("v2 replay changed the stored source/input binding")
        if fields.get("sorry_detected") is not False:
            raise ValueError("v2 replay lacks native no-sorry evidence")


def check_artifact_references(proposal, view, artifacts):
    # Use applied semantics, including additions, removals and field writes in
    # this proposal. Looking only at committed edges misses atomic promotions.
    effective = JournalOverlayView(view, proposal.base_revision or 0, proposal.ops)
    references = set()
    certificates = {}
    replays = {}
    visited = set()

    def collect(node_id):
        if node_id in visited:
            return
        visited.add(node_id)
        node = effective.node(node_id)
        if node is None:
            return
        if node.label == "Claim":
            for edge in effective.edges_from(node_id, "PROVED_BY"):
                collect(edge.dst_id)
            return
        if node.label not in {"Certificate", "LeanReplay"}:
            return
        fields = node.fields
        for key in ("artifact_hash", "binding_artifact_hash", "attestation_artifact_hash"):
            if fields.get(key):
                references.add(fields[key])
        if node.label == "Certificate":
            certificates[node_id] = fields
            for edge in effective.edges_from(node_id, "REPLAYED_BY"):
                collect(edge.dst_id)
        else:
            replays[node_id] = fields
            if fields.get("certificate_id"):
                collect(fields["certificate_id"])

    for op in proposal.ops:
        if isinstance(op, (UpsertNode, SetField)):
            collect(op.node_id)
        elif isinstance(op, AddEdge):
            if op.rel_type == "HAS_ARTIFACT":
                references.add(op.dst_id)
            collect(op.src_id)
            collect(op.dst_id)
        elif isinstance(op, RemoveEdge):
            edge = view.edge(op.edge_id)
            if edge is not None:
                # Follow surviving evidence from its owner, not removed replay
                # nodes that are being detached to repair historical evidence.
                collect(edge.src_id)

    requires_store = any(f.get("binding_artifact_hash") for f in certificates.values())
    if artifacts is None:
        if requires_store:
            return [Rejection(Reason.ARTIFACT_UNAVAILABLE,
                              "v2 certificates require CommitGate(..., artifacts=store)")]
        return []  # Compatibility for older providers without durable bindings.

    findings = []
    loaded = {}

    def load(digest):
        if digest not in loaded:
            raw = artifacts.get(digest)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("stored bytes do not match their hash")
            loaded[digest] = raw
        return loaded[digest]

    def reject(identity, exc):
        findings.append(Rejection(
            Reason.ARTIFACT_UNAVAILABLE, f"cannot commit evidence {identity}: {exc}",
        ))

    for digest in sorted(references):
        try:
            load(digest)
        except Exception as exc:
            reject(digest, exc)

    for cert_id, fields in certificates.items():
        digest = fields.get("binding_artifact_hash")
        try:
            # The certificate's format identifies v2 even if a proposal omits
            # its binding field in an attempt to take the legacy path.
            try:
                certificate = _object(load(fields["artifact_hash"]))
            except (ValueError, UnicodeDecodeError):
                certificate = {}  # Older providers may store opaque traces.
            v2_format = certificate.get("format") in {
                "maths-ai-mcts-certificate-v1", "maths-ai-accepted-certificate-v1",
            }
            if not digest:
                if v2_format:
                    raise ValueError("v2 certificate requires a durable run binding")
                continue
            binding = _object(load(digest))
            if binding.get("format") != "persistent-atp-formal-v2-binding-v1":
                raise ValueError("unsupported v2 run binding")
            request, snapshot = binding["request"], binding["snapshot"]
            expected = snapshot["result"]["certificate"]["artifact_hash"]
            if (fields.get("artifact_hash") != expected
                    or fields.get("producer_run_id") != request["run_id"]
                    or fields.get("environment_hash") != request["environment_hash"]):
                raise ValueError("certificate fields differ from the durable run binding")
            if fields.get("protocol_version") == 2 and any(
                fields.get(key) != snapshot.get(key) for key in
                ("request_digest", "input_artifact_hash", "source_artifact_hash")
            ):
                raise ValueError("v2 certificate fields differ from the stored input/source binding")
            declarations = {e.dst_id for e in effective.edges_from(cert_id, "CERTIFIES")}
            if declarations and declarations != {request["formal_declaration_id"]}:
                raise ValueError("certificate declaration differs from the durable run binding")
            for address in (expected, snapshot["input_artifact_hash"], snapshot["source_artifact_hash"]):
                load(address)

            resolved = snapshot["resolved"]
            selector = request.get("backend") or {}
            if any(selector.get(key) is not None and selector[key] != resolved[target]
                   for key, target in (("name", "backend"), ("profile", "profile"))):
                raise ValueError("resolved backend differs from the requested selection")
            if request.get("deployment_id") is not None and request["deployment_id"] != resolved["deployment_id"]:
                raise ValueError("resolved deployment differs from the requested selection")
            expected_replay = {
                "status": "verified", "run_id": request["run_id"],
                "certificate_hash": expected,
                "input_artifact_hash": snapshot["input_artifact_hash"],
                "source_artifact_hash": snapshot["source_artifact_hash"],
                "source_origin": snapshot["source_origin"],
                "backend": resolved["backend"],
                "environment_hash": request["environment_hash"],
            }
            expected_format = ("maths-ai-mcts-certificate-v1"
                               if expected_replay["backend"] == "mcts-actor-critic"
                               else "maths-ai-accepted-certificate-v1")
            if certificate.get("format") != expected_format:
                raise ValueError("certificate backend format differs from the durable run binding")
            for key in ("run_id", "input_artifact_hash", "source_artifact_hash", "backend", "environment_hash"):
                if certificate.get(key) != expected_replay[key]:
                    raise ValueError(f"certificate bytes {key} differ from the durable run binding")
            manifest = _object(load(snapshot["input_artifact_hash"]))
            expected_input = {
                "format": "maths-ai-formal-input-v1", "input": request["input"],
                "kind": request["input"]["kind"],
                "environment_id": request["environment_id"],
                "environment_hash": request["environment_hash"],
                "source_artifact_hash": snapshot["source_artifact_hash"],
                "source_origin": {"source": "caller-artifact", "goal": "generated-from-text"}[request["input"]["kind"]],
            }
            if any(manifest.get(k) != v for k, v in expected_input.items()):
                raise ValueError("admitted input bytes differ from the durable run binding")
            if expected_replay["source_origin"] != expected_input["source_origin"]:
                raise ValueError("source origin differs from the admitted input kind")
            linked = {e.dst_id for e in effective.edges_from(cert_id, "REPLAYED_BY")}
            for replay_id, replay in replays.items():
                if replay_id not in linked and replay.get("certificate_id") != cert_id:
                    continue
                if replay.get("status") != "verified":
                    continue
                attestation = replay.get("attestation_artifact_hash")
                if not attestation:
                    raise ValueError(f"verified v2 replay {replay_id!r} requires a stored attestation")
                if replay.get("certificate_id") != cert_id:
                    raise ValueError("verified replay names a different certificate")
                _check_v2_replay(_object(load(attestation)), replay, expected_replay)
        except Exception as exc:
            reject(cert_id, exc)

    # Legacy replay artifacts retain their existing field checks. V2 replays
    # additionally pass the mandatory full binding checks above.
    for replay_id, fields in replays.items():
        digest = fields.get("attestation_artifact_hash")
        if not digest:
            continue
        try:
            body = _object(load(digest))
            if (body.get("status") != fields.get("status")
                    or body.get("certificate_hash") != fields.get("artifact_hash")
                    or body.get("environment_hash") != fields.get("environment_hash")):
                raise ValueError("replay fields differ from the stored attestation")
        except Exception as exc:
            reject(replay_id, exc)
    return findings
