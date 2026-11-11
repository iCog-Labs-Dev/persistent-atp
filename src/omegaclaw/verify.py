"""Trusted, exclusive verification pass over a committed Mathematics-ai run.

Stop the receiver first. This command owns the same production state root and
uses separate replayer/coordinator leases; it never writes graph state directly.
"""
from __future__ import annotations

import argparse
import json

from commit_gate.canon import canonical_json, content_hash
from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from .cli import load_config
from .execution import MathsAIExecutor
from .factory import build_service


def _commit(service, proof, move, actor, worker, stage, operations):
    lease_id = f"{proof}/replay-{content_hash([move, stage]).split(':')[1]}"
    lease = service.store.issue_lease(proof, lease_id, actor=actor, worker_class=worker,
        selected_move_id=move, base_revision=service.store.head(proof)[0], ttl_seconds=180)
    try:
        result = service.gate.commit(Proposal(proof, actor, worker, ops=tuple(operations),
            base_revision=lease.base_revision, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
            selected_move_id=move))
        if not result.accepted:
            raise ValueError("verification gate rejected: " + ", ".join(item.reason.value for item in result.rejections))
    finally:
        service.store.release_lease(proof, lease.lease_id, fencing_token=lease.fencing_token)


def verify_run(service, run_id, *, promote=False):
    if not isinstance(service.executor, MathsAIExecutor):
        raise ValueError("verification requires the strict Mathematics-ai executor")
    proof = run_id.split("/", 1)[0]
    service.catch_up(proof)
    run = service.view.node(run_id)
    if run is None or run.label != "FormalRun" or run.fields.get("status") != "proved-pending-replay":
        raise ValueError("run has no committed candidate proof")
    record = service.executions.get(run.fields.get("operation_id"))
    if record is None or record["run_id"] != run_id or record["executor_id"] != service.executor.identity:
        raise ValueError("run has no matching durable provider/request association")
    request = json.loads(service.artifacts.get(record["request_artifact"]))
    if (canonical_json(request) != canonical_json(json.loads(record["request"]))
            or run.fields.get("request_artifact") != record["request_artifact"]):
        raise ValueError("committed canonical request identity differs")
    result = json.loads(service.artifacts.get(record["result_artifact"]))
    if run.fields.get("result_artifact") != record["result_artifact"]:
        raise ValueError("committed result identity differs from the execution record")
    service.executor.adapter._validate_result(result, request)
    certificate = result.get("certificate")
    if not certificate or certificate.get("status") not in {"candidate", "replay-pending"}:
        raise ValueError("stored result does not contain an initial candidate")
    cert_id = certificate["witness"]["certificate_id"]
    cert = service.view.node(cert_id)
    if (cert is None or cert.fields.get("artifact_hash") != certificate["artifact_hash"]
            or cert.fields.get("producer_run_id") != run_id
            or cert.fields.get("environment_hash") != request["environment_hash"]):
        raise ValueError("candidate certificate binding differs from committed proof state")
    if service._aligned(proof, request["claim_id"], request["formal_declaration_id"]) is None:
        raise ValueError("exact statement alignment is not reviewed")
    # This command owns the production root exclusively. Prior verification
    # stages may have committed immediately before their process died, leaving
    # an operational lease behind. Release only those owned verification leases
    # before reassessing the next stage; never renew the original search lease.
    for lease in service.store.active_leases(proof):
        if (lease.selected_move_id == cert_id
                and lease.actor in {"receiver-lean-replayer", "receiver-proof-promotion"}):
            service.store.release_lease(proof, lease.lease_id, fencing_token=lease.fencing_token)
    service.executor.restore(request)
    if cert.fields["status"] == "candidate":
        _commit(service, proof, cert_id, "receiver-lean-replayer", "replayer", "pending",
                [SetField("Certificate", cert_id, "status", "replay-pending", prior="candidate")])
    saved = service.executions.replay(run_id)
    if saved is None:
        observed = service.executor.observe_or_start(request, allow_start=False)
        candidate = observed.get("certificate")
        if candidate is None or candidate["artifact_hash"] != certificate["artifact_hash"]:
            raise ValueError("provider's candidate changed before replay")
        verdict = service.executor.adapter.formal_replay(candidate, request["environment_hash"])
        if verdict.get("status") not in {"verified", "rejected"}:
            raise ValueError("provider returned no authoritative replay verdict")
        pointer = service.artifacts.put(canonical_json(verdict))
        service.executions.save_replay(run_id, certificate["artifact_hash"], request["environment_hash"], pointer)
        saved = service.executions.replay(run_id)
    if (saved["certificate_hash"] != certificate["artifact_hash"]
            or saved["environment_hash"] != request["environment_hash"]):
        raise ValueError("saved replay belongs to a different certificate/environment")
    verdict = json.loads(service.artifacts.get(saved["result_artifact"]))
    verified = verdict.get("status") == "verified"
    replay_id = f"{proof}/lr-{int(content_hash([run_id, certificate['artifact_hash']]).split(':')[1], 16) + 1}"
    if service.view.node(replay_id) is None:
        fields = {"actor": "receiver-lean-replayer", "status": "verified" if verified else "rejected",
                  "sorry_detected": False if verified else verdict.get("sorry_detected"),
                  "environment_hash": request["environment_hash"], "certificate_id": cert_id,
                  "artifact_hash": certificate["artifact_hash"], "report_artifact": saved["result_artifact"]}
        if verified and (verdict.get("sorry_detected") is not False
                         or verdict.get("run_id") != run_id
                         or verdict.get("environment_hash") != request["environment_hash"]
                         or verdict.get("certificate_hash") != certificate["artifact_hash"]):
            raise ValueError("replay lacks exact no-sorry run/certificate evidence")
        operations = [UpsertNode("LeanReplay", replay_id, fields),
            AddEdge("REPLAYED_BY", cert_id, replay_id, f"{replay_id}/certificate"),
            AddEdge("REPLAY_ENVIRONMENT", replay_id, request["environment_id"], f"{replay_id}/environment"),
            SetField("Certificate", cert_id, "status", "replay-accepted" if verified else "replay-rejected",
                     prior="replay-pending")]
        _commit(service, proof, cert_id, "receiver-lean-replayer", "replayer", "verdict", operations)
    if promote and verified:
        claim = service.view.node(request["claim_id"])
        if claim.fields["status"] in {"provisional", "critic-accepted"}:
            _commit(service, proof, cert_id, "receiver-proof-promotion", "coordinator", "closed", [
                AddEdge("PROVED_BY", claim.node_id, cert_id, f"{replay_id}/proved"),
                SetField("Claim", claim.node_id, "status", "formally-closed", prior=claim.fields["status"])])
        claim = service.view.node(request["claim_id"])
        if claim.fields["status"] == "formally-closed":
            _commit(service, proof, cert_id, "receiver-proof-promotion", "coordinator", "verified", [
                SetField("Claim", claim.node_id, "status", "lean-verified", prior="formally-closed")])
        elif claim.fields["status"] != "lean-verified":
            raise ValueError("claim is not eligible for verification promotion")
    return {**service._status(proof), "replay_id": replay_id, "replay_status": verdict["status"],
            "replay_artifact": saved["result_artifact"], "certificate_id": cert_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--promote", action="store_true", help="also propose claim verification through CommitGate")
    args = parser.parse_args(argv)
    config, root = load_config(args.config)
    if config.get("mode") != "production": parser.error("verification requires a production configuration")
    service = build_service(config, root)
    try:
        result = verify_run(service, args.run_id, promote=args.promote)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["replay_status"] == "verified" else 2
    finally: service.close()


if __name__ == "__main__":
    raise SystemExit(main())
