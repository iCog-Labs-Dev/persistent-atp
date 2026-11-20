"""Verify a self-contained project export without admitting graph mutations."""
from __future__ import annotations

import json

from artifacts.hashing import hash_content
from commit_gate.canon import GENESIS_HASH, chain_hash
from commit_gate.apply import apply_ops
from commit_gate.ops import ops_from_dicts
from commit_gate.state import MemoryView

ARTIFACT_FIELDS = frozenset({"target_artifact", "literal_target_hash", "theorem_spec_artifact",
    "lean_source_artifact", "source_hash", "report_artifact", "packet_artifact",
    "result_artifact", "minimal_state_artifact", "certificate_artifact",
    "checkpoint_artifact", "artifact_hash", "binding_artifact_hash",
    "provider_binding_artifact", "provider_result_artifact", "input_artifact_hash",
    "source_artifact_hash", "receipt_artifact", "attestation_artifact_hash",
    "diagnostic_artifact", "revision_spec_artifact"})
ARTIFACT_LIST_FIELDS = frozenset({"diagnostic_artifacts"})


def referenced_artifacts(events: list[dict], *, read=None) -> set[str]:
    """Artifact pointers explicitly carried by committed graph operations."""
    pointers: set[str] = set()
    formal_evidence: set[str] = set()
    for event in events:
        for op in event["ops"]:
            fields = op.get("fields", {})
            if op.get("op") == "set_field":
                fields = {op["field"]: op["value"]}
            for name, value in fields.items():
                if name in ARTIFACT_FIELDS and isinstance(value, str) and value.startswith("sha256:"):
                    pointers.add(value)
                    if name in {"provider_result_artifact", "provider_binding_artifact", "binding_artifact_hash"}:
                        formal_evidence.add(value)
                if name in ARTIFACT_LIST_FIELDS and isinstance(value, list):
                    pointers.update(ref for ref in value if isinstance(ref, str) and ref.startswith("sha256:"))
    if read is not None:
        for pointer in formal_evidence:
            body = json.loads(read(pointer))
            result = body.get("snapshot", {}).get("result") if "snapshot" in body else body
            for artifact in (result or {}).get("artifacts", ()):
                pointers.add(artifact["sha256"])
    return pointers


def check_export(content: bytes) -> dict:
    bundle = json.loads(content)
    if set(bundle) != {"proof_id", "head", "chain", "events", "artifacts"}:
        raise ValueError("export manifest shape is invalid")
    proof_id = bundle["proof_id"]
    previous = GENESIS_HASH
    if len(bundle["chain"]) != len(bundle["events"]):
        raise ValueError("export event count mismatch")
    view = MemoryView()
    for expected_revision, (chain_row, event) in enumerate(zip(bundle["chain"], bundle["events"]), 1):
        revision, event_hash, prev_hash = chain_row
        if revision != expected_revision or prev_hash != previous:
            raise ValueError("export chain is discontinuous")
        if event.get("proof_id") != proof_id or chain_hash(previous, event) != event_hash:
            raise ValueError("export event hash mismatch")
        apply_ops(view, ops_from_dicts(event["ops"]))
        previous = event_hash
    if bundle["head"] != [len(bundle["events"]), previous]:
        raise ValueError("export head mismatch")
    for pointer, hex_bytes in bundle["artifacts"].items():
        if hash_content(bytes.fromhex(hex_bytes)) != pointer:
            raise ValueError("export artifact hash mismatch")
    try:
        expected = referenced_artifacts(bundle["events"], read=lambda key: bytes.fromhex(bundle["artifacts"][key]))
    except (KeyError, TypeError) as exc:
        raise ValueError("export is missing referenced formal evidence") from exc
    if expected != set(bundle["artifacts"]):
        raise ValueError("export artifact manifest differs from committed references")
    return {"proof_id": proof_id, "revision": len(bundle["events"]),
            "event_hash": previous, "artifact_count": len(bundle["artifacts"]),
            "rebuild_node_count": len(view.nodes)}
