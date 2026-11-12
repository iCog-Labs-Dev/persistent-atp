#!/usr/bin/env python3
"""Demonstrate that CommitGate audits a rejection without mutating MORK."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]


def _ensure_mork_preloaded() -> None:
    library = os.environ.get("MORK_LIBRARY", "")
    if library and library in os.environ.get("LD_PRELOAD", "").split(":"):
        return
    wrapper = REPO / "scripts" / "with-mork.sh"
    os.execv(
        str(wrapper),
        [str(wrapper), sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=REPO / "runs" / "rejection-demo")
    parser.add_argument("--run-id", help="artifact directory name; must not already exist")
    return parser.parse_args()


def main() -> int:
    _ensure_mork_preloaded()
    args = _parse_args()

    from commit_gate.gate import CommitGate
    from commit_gate.ops import UpsertNode
    from commit_gate.proposal import Proposal
    from commit_gate.store import JournalStore
    from mork.backend import MorkSpace, MorkView

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    run_dir = args.output_root.resolve() / run_id
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise SystemExit(f"rejection-demo artifacts already exist: {run_dir}") from exc

    proof_id = "rejection-demo-" + uuid4().hex[:12]
    journal = run_dir / "proof.sqlite"
    result_path = run_dir / "result.json"
    atoms_path = run_dir / "mork-state.metta"
    store = JournalStore(str(journal))
    view = MorkView(MorkSpace())
    gate = CommitGate(view, store)
    statement = "For every natural number n, n + 0 = n."
    lean_type = "∀ (n : Nat), n + 0 = n"
    exact_hash = "sha256:" + sha256(lean_type.encode("utf-8")).hexdigest()

    seed_lease = f"{proof_id}/seed"
    seed_token = store.acquire_lease(
        proof_id,
        seed_lease,
        actor="demo-coordinator",
        worker_class="coordinator",
    )
    try:
        seed = gate.commit(Proposal(
            proof_id=proof_id,
            actor="demo-coordinator",
            worker_class="coordinator",
            base_revision=0,
            lease_id=seed_lease,
            fencing_token=seed_token,
            ops=(
                UpsertNode(
                    "Claim",
                    f"{proof_id}/claim-1",
                    {"status": "provisional", "claim_text": statement},
                ),
                UpsertNode(
                    "FormalDeclaration",
                    f"{proof_id}/declaration-1",
                    {"exact_hash": exact_hash, "lean_type": lean_type},
                ),
            ),
        ))
    finally:
        store.release_lease(proof_id, seed_lease, fencing_token=seed_token)
    if not seed.accepted:
        raise RuntimeError(f"seed proposal was rejected: {seed.to_dict()}")

    head_before = store.head(proof_id)
    projected_before = view.projected_revision(proof_id)
    atoms_before = view.atoms(proof_id)

    bad_lease = f"{proof_id}/invalid-formal-atp"
    bad_token = store.acquire_lease(
        proof_id,
        bad_lease,
        actor="broken-formal-atp",
        worker_class="formal-atp",
    )
    try:
        rejected = gate.commit(Proposal(
            proof_id=proof_id,
            actor="broken-formal-atp",
            worker_class="formal-atp",
            base_revision=head_before[0],
            lease_id=bad_lease,
            fencing_token=bad_token,
            ops=(
                UpsertNode(
                    "TacticApplication",
                    f"{proof_id}/invalid-tactic",
                    {"executor_result": "lean-accepted"},
                ),
            ),
        ))
    finally:
        store.release_lease(proof_id, bad_lease, fencing_token=bad_token)

    head_after = store.head(proof_id)
    projected_after = view.projected_revision(proof_id)
    atoms_after = view.atoms(proof_id)
    audit = list(store.read_rejections(proof_id))
    checks = {
        "proposal_rejected": not rejected.accepted,
        "journal_head_unchanged": head_after == head_before,
        "mork_revision_unchanged": projected_after == projected_before,
        "mork_atoms_unchanged": atoms_after == atoms_before,
        "rejection_audited": len(audit) == len(rejected.rejections) and bool(audit),
        "journal_chain_valid": store.verify_chain(proof_id) == head_after[0],
    }
    if not all(checks.values()):
        raise RuntimeError(f"rejection safety checks failed: {checks}")

    atoms_path.write_text("\n".join(atoms_after) + "\n", encoding="utf-8")
    summary = {
        "demo": "commit-gate-rejection",
        "proof_id": proof_id,
        "mork_used": True,
        "accepted": rejected.accepted,
        "rejections": [item.to_dict() for item in rejected.rejections],
        "journal_revision_before": head_before[0],
        "journal_revision_after": head_after[0],
        "mork_revision_before": projected_before,
        "mork_revision_after": projected_after,
        "checks": checks,
        "artifacts": {
            "journal": str(journal),
            "result": str(result_path),
            "mork_atoms": str(atoms_path),
        },
    }
    result_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    store.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
