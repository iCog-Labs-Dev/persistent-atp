"""Lease-bound v2 evidence proposals for the existing commit gate.

No journal, promotion bypass, invented replay actor or environment is created.
Callers submit returned proposals through CommitGate.commit. Snapshot revision
and live lease/fencing authority remain the gate's final atomic decision.
"""

import copy

from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, RemoveEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from .formal_v2 import FormalV2Error, _json


def _id(proof, kind, *parts):
    return f"{proof}/{kind}-{int(content_hash(list(parts))[7:], 16)}"


def _proposal(lease, ops):
    return Proposal(proof_id=lease.proof_id, actor=lease.actor, worker_class=lease.worker_class,
                    base_revision=lease.base_revision, lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token, selected_move_id=lease.selected_move_id, ops=tuple(ops))


def _node(view, node_id, label):
    node = view.node(node_id)
    if node is None or node.label != label:
        raise FormalV2Error(f"missing committed {label}: {node_id}")
    return node


def _put(artifacts, data):
    import hashlib
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    if artifacts.put(data) != expected:
        raise FormalV2Error("coordinator artifact store changed evidence content address")
    return expected


def _get(artifacts, key):
    import hashlib
    data = artifacts.get(key)
    if "sha256:" + hashlib.sha256(data).hexdigest() != key:
        raise FormalV2Error("persisted coordinator binding digest mismatch")
    return _json(data)


def _edge(proof, relation, src, dst, **fields):
    return AddEdge(relation, src, dst, _id(proof, "e", relation, src, dst), fields)


def _environment(view, declaration, expected):
    pins = view.edges_from(declaration, "PINNED_ENVIRONMENT")
    if len(pins) != 1:
        raise FormalV2Error("declaration needs exactly one committed environment pin")
    node = _node(view, pins[0].dst_id, "Environment")
    if node.fields.get("environment_hash") != expected:
        raise FormalV2Error("provider evidence differs from the declaration's pinned environment")
    return node.node_id


def search_proposal(client, run_id, lease, view, artifacts):
    """Import one settled, validated result under its original formal-atp lease."""
    if lease.worker_class != "formal-atp" or lease.selected_move_id != run_id:
        raise FormalV2Error("search evidence requires the run's formal-atp lease")
    snapshot = client.formal_search_status(run_id)
    request = client.requests[run_id]
    for field in ("proof_id", "base_revision", "lease_id", "fencing_token"):
        if request[field] != getattr(lease, field):
            raise FormalV2Error("search evidence differs from original lease/revision/fencing")
    result = snapshot["result"]
    if result is None:
        raise FormalV2Error("search has not settled")
    declaration = _node(view, request["formal_declaration_id"], "FormalDeclaration")
    _node(view, request["claim_id"], "Claim")
    run = _node(view, run_id, "FormalRun")
    env_id = _environment(view, declaration.node_id, request["environment_hash"])
    existing_searches = {edge.dst_id for edge in view.edges_from(run_id, "SEARCHES")}
    if existing_searches != {declaration.node_id}:
        raise FormalV2Error("committed run does not uniquely search this declaration")
    source = request["input"]
    if source["kind"] == "source":
        if (declaration.fields.get("lean_source_artifact") != source["artifact_hash"]
                or declaration.fields.get("lean_name") != source["declaration"]):
            raise FormalV2Error("requested source is not the committed formal declaration")
    elif (declaration.fields.get("goal_input") != source
          or declaration.fields.get("lean_source_artifact") != result["source_artifact_hash"]):
        raise FormalV2Error("goal evidence needs a formalizer-bound generated source and complete goal input")
    binding_hash = _put(artifacts, canonical_json(client.binding(run_id)))
    result_hash = _put(artifacts, canonical_json(result))
    for artifact in result["artifacts"]:
        _put(artifacts, client.get_artifact(artifact["sha256"]))
    pinned = {"actor": lease.actor, "environment_hash": request["environment_hash"],
              "request_digest": snapshot["request_digest"], "provider_binding_artifact": binding_hash,
              "provider_result_artifact": result_hash, "provider_attempt": snapshot["attempts"],
              "input_artifact_hash": result["input_artifact_hash"], "source_artifact_hash": result["source_artifact_hash"]}
    if any(run.fields.get(key) != pinned[key] for key in ("actor", "environment_hash")):
        raise FormalV2Error("run must be prepared with its producing actor and pinned environment before dispatch")
    if any(run.fields.get(key) not in (None, value) for key, value in pinned.items()):
        raise FormalV2Error("run already has a different producing actor or evidence binding")
    ops = [SetField("FormalRun", run_id, key, value, prior=run.fields.get(key))
           for key, value in pinned.items() if key not in {"actor", "environment_hash"} and run.fields.get(key) != value]
    ops += [SetField("FormalRun", run_id, "status", result["disposition"], prior=run.fields.get("status")),
           _edge(lease.proof_id, "RAN_UNDER", run_id, env_id)]
    # Provider row IDs restart at fs-1/ta-1. Allocate deterministic run/attempt
    # occurrence IDs; never merge these using pretty text or semantic signatures.
    scope = (snapshot["request_digest"], snapshot["attempts"])
    states = {row["state_id"]: _id(lease.proof_id, "fs", *scope, row["state_id"]) for row in result["states"]}
    for row in result["states"]:
        fields = {key: copy.deepcopy(value) for key, value in row.items() if key != "state_id"}
        fields.update(environment_hash=result["environment_hash"], exact_hash_scope="run-occurrence", transposition_eligible=False)
        ops.append(UpsertNode("FormalState", states[row["state_id"]], fields))
    for row in result["tactic_edges"]:
        identity = _id(lease.proof_id, "ta", *scope, row["tactic_id"])
        fields = {key: copy.deepcopy(value) for key, value in row.items() if key not in {"tactic_id", "source_state_id"}}
        fields["produced_goal_ids"] = [states[key] for key in row["produced_goal_ids"]]
        ops.extend([UpsertNode("TacticApplication", identity, fields),
                    _edge(lease.proof_id, "HAS_TACTIC", states[row["source_state_id"]], identity)])
        for index, child in enumerate(row["produced_goal_ids"]):
            ops.append(_edge(lease.proof_id, "FORMAL_REQUIRES", identity, states[child], child_index=index))
        if row["subgoal_count"] == 0 and row["executor_result"] == "lean-accepted":
            ops.append(_edge(lease.proof_id, "CLOSES_STATE", identity, states[row["source_state_id"]]))
    for root in view.edges_from(run_id, "HAS_ROOT"):
        ops.append(RemoveEdge("HAS_ROOT", root.edge_id))
    ops.append(_edge(lease.proof_id, "HAS_ROOT", run_id, states[result["root_state_id"]]))
    if result["disposition"] == "stagnated":
        obstruction = _id(lease.proof_id, "obs", *scope)
        ops.extend([UpsertNode("Obstruction", obstruction, {"kind": "search-policy", "actor": lease.actor,
            "description": "Bounded search exhausted alternatives; no refutation is asserted.",
            "environment_hash": result["environment_hash"], "diagnostic_artifact": result_hash}),
            _edge(lease.proof_id, "RAISED_OBSTRUCTION", run_id, obstruction)])
    if result.get("certificate"):
        certificate = result["certificate"]
        identity = _id(lease.proof_id, "cert", certificate["artifact_hash"])
        ops.extend([UpsertNode("Certificate", identity, {"actor": lease.actor, "status": "replay-pending",
            "protocol_version": 2, "input_kind": source["kind"], "source_declaration": source.get("declaration"),
            "artifact_hash": certificate["artifact_hash"], "producer_run_id": run_id,
            "environment_hash": result["environment_hash"], "request_digest": snapshot["request_digest"],
            "input_artifact_hash": result["input_artifact_hash"], "source_artifact_hash": result["source_artifact_hash"]}),
            _edge(lease.proof_id, "PRODUCED_CERTIFICATE", run_id, identity),
            _edge(lease.proof_id, "CERTIFIES", identity, declaration.node_id),
            _edge(lease.proof_id, "CERTIFICATE_ENVIRONMENT", identity, env_id)])
    return _proposal(lease, ops)


def replay_proposal(client, lease, view, artifacts, *, certificate_id, max_steps, wall_clock_ms):
    """Call fresh replay under a real independent lease, then propose its evidence."""
    if lease.worker_class != "replayer" or lease.selected_move_id != certificate_id:
        raise FormalV2Error("replay evidence requires the selected certificate's replayer lease")
    certificate = _node(view, certificate_id, "Certificate")
    if certificate.fields.get("actor") == lease.actor:
        raise FormalV2Error("search producer cannot act as its independent replayer")
    if certificate.fields.get("status") != "replay-pending":
        raise FormalV2Error("certificate is not pending independent replay")
    run_id = certificate.fields["producer_run_id"]
    run = _node(view, run_id, "FormalRun")
    binding = _get(artifacts, run.fields["provider_binding_artifact"])
    client.restore(binding)
    result = client.latest[run_id]["result"]
    for field in ("artifact_hash", "producer_run_id"):
        if certificate.fields[field] != result["certificate"][field]:
            raise FormalV2Error("committed certificate differs from the persisted provider evidence")
    declarations = view.edges_from(certificate_id, "CERTIFIES")
    if len(declarations) != 1:
        raise FormalV2Error("certificate needs one original declaration")
    env_id = _environment(view, declarations[0].dst_id, certificate.fields["environment_hash"])
    declaration = _node(view, declarations[0].dst_id, "FormalDeclaration")
    if declaration.fields.get("lean_source_artifact") != certificate.fields["source_artifact_hash"]:
        raise FormalV2Error("declaration source changed since search admission")
    receipt = client.formal_replay(result["certificate"], result["environment_hash"],
                                  max_steps=max_steps, wall_clock_ms=wall_clock_ms)
    receipt_hash = _put(artifacts, canonical_json(receipt))
    if receipt["status"] == "inconclusive":
        raise FormalV2Error("replay is inconclusive; receipt retained without logical rejection or promotion")
    if receipt["status"] == "verified" and receipt["verification_cost"].get("sorry_detected") is not False:
        raise FormalV2Error("verified receipt lacks explicit native no-sorry evidence")
    replay_id = _id(lease.proof_id, "replay", certificate_id, lease.lease_id, lease.fencing_token, receipt_hash)
    ops = [UpsertNode("LeanReplay", replay_id, {"actor": lease.actor, "status": receipt["status"],
        "protocol_version": 2, "request_digest": certificate.fields["request_digest"], "run_id": run_id,
        "sorry_detected": receipt["verification_cost"].get("sorry_detected"), "receipt_artifact": receipt_hash,
        "artifact_hash": certificate.fields["artifact_hash"], "environment_hash": receipt["environment_hash"],
        "certificate_id": certificate_id, "input_artifact_hash": receipt["input_artifact_hash"],
        "source_artifact_hash": receipt["source_artifact_hash"]}),
        _edge(lease.proof_id, "REPLAYED_BY", certificate_id, replay_id),
        _edge(lease.proof_id, "REPLAY_ENVIRONMENT", replay_id, env_id),
        SetField("Certificate", certificate_id, "status", "replay-accepted" if receipt["status"] == "verified" else "replay-rejected",
                 prior=certificate.fields["status"])]
    return _proposal(lease, ops)


def promotion_proposal(lease, view, *, claim_id, certificate_id):
    """Coordinator proposal only; alignment/replay/authority remain gate checks."""
    if lease.worker_class != "coordinator" or lease.selected_move_id != claim_id:
        raise FormalV2Error("claim promotion requires its coordinator lease")
    claim = _node(view, claim_id, "Claim")
    certificate = _node(view, certificate_id, "Certificate")
    if certificate.fields.get("status") != "replay-accepted":
        raise FormalV2Error("claim promotion requires independently replayed evidence")
    status = claim.fields.get("status")
    if status not in {"provisional", "critic-accepted", "formally-closed"}:
        raise FormalV2Error("claim is not in a promotable lifecycle state")
    ops = [_edge(lease.proof_id, "PROVED_BY", claim_id, certificate_id)]
    if status != "formally-closed":
        ops.append(SetField("Claim", claim_id, "status", "formally-closed", prior=status))
    ops.append(SetField("Claim", claim_id, "status", "lean-verified", prior="formally-closed"))
    return _proposal(lease, ops)


def _lease_view(gate, lease):
    from context_compiler.snapshot_provider import LeaseSnapshotProvider
    return LeaseSnapshotProvider(gate.view, gate.store).for_lease(lease).view


def admit_search(client, run_id, lease, gate, artifacts):
    """Journal validated search evidence through the current leased snapshot."""
    return gate.commit(search_proposal(client, run_id, lease, _lease_view(gate, lease), artifacts))


def admit_replay(client, lease, gate, artifacts, *, certificate_id, max_steps, wall_clock_ms):
    return gate.commit(replay_proposal(client, lease, _lease_view(gate, lease), artifacts,
        certificate_id=certificate_id, max_steps=max_steps, wall_clock_ms=wall_clock_ms))


def admit_promotion(lease, gate, *, claim_id, certificate_id):
    return gate.commit(promotion_proposal(lease, _lease_view(gate, lease),
        claim_id=claim_id, certificate_id=certificate_id))
