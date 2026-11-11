"""Verify a self-contained project export without admitting graph mutations."""
from __future__ import annotations

import json

from artifacts.hashing import hash_content
from commit_gate.canon import GENESIS_HASH, chain_hash
from commit_gate.apply import apply_ops
from commit_gate.ops import ops_from_dicts
from commit_gate.state import MemoryView

ARTIFACT_FIELDS = frozenset({"target_artifact", "literal_target_hash",
    "lean_source_artifact", "source_hash", "report_artifact", "packet_artifact",
    "request_artifact", "result_artifact", "minimal_state_artifact", "certificate_artifact",
    "checkpoint_artifact"})
ARTIFACT_LIST_FIELDS = frozenset({"diagnostic_artifacts"})


def referenced_artifacts(events: list[dict]) -> set[str]:
    """Artifact pointers explicitly carried by committed graph operations."""
    pointers: set[str] = set()
    for event in events:
        for op in event["ops"]:
            for name, value in op.get("fields", {}).items():
                if name in ARTIFACT_FIELDS and isinstance(value, str) and value.startswith("sha256:"):
                    pointers.add(value)
                if name in ARTIFACT_LIST_FIELDS and isinstance(value, list):
                    pointers.update(ref for ref in value if isinstance(ref, str) and ref.startswith("sha256:"))
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
    if referenced_artifacts(bundle["events"]) != set(bundle["artifacts"]):
        raise ValueError("export artifact manifest differs from committed references")
    for pointer, hex_bytes in bundle["artifacts"].items():
        if hash_content(bytes.fromhex(hex_bytes)) != pointer:
            raise ValueError("export artifact hash mismatch")
    return {"proof_id": proof_id, "revision": len(bundle["events"]),
            "event_hash": previous, "artifact_count": len(bundle["artifacts"]),
            "rebuild_node_count": len(view.nodes)}
