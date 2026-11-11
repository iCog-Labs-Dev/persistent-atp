"""A minimal supervised MORK host: seed journal, catch up, declare readiness.

Spawned by test_supervisor.py, which kills this process with SIGKILL and
requires the supervisor to restart it. A fresh process sees a non-empty
journal, skips seeding, replays the journal into its empty in-memory
projection, and only then writes readiness.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    db_path, ready_path, event_count = sys.argv[1:4]

    from commit_gate import CommitGate, JournalStore
    from commit_gate.ops import UpsertNode
    from commit_gate.proposal import Proposal
    from mork.backend import MorkSpace, MorkView

    store = JournalStore(db_path)
    proof = "sup"
    if store.head(proof)[0] == 0:
        for index in range(int(event_count)):
            proposal = Proposal(
                proof_id=proof,
                actor="child",
                worker_class="child",
                ops=[UpsertNode("FormalState", f"{proof}/s{index}", {})],
            )
            store.append(proposal.to_dict())

    bundle = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    if bundle is not None and store.head("sup-other")[0] == 0:
        store.append(Proposal(proof_id="sup-other", actor="child", worker_class="child",
            ops=[UpsertNode("FormalState", "sup-other/s", {"payload": "λ" * 6000})]
        ).to_dict())
    space = MorkSpace()
    projection = MorkView(space)
    gate = CommitGate(projection, store)
    if bundle is not None and bundle.exists():
        from mork.snapshot import restore_backup
        restore_backup(space, projection, store, bundle)
    gate.catch_up()  # rebuild every projection before readiness
    if bundle is not None and not bundle.exists():
        from mork.snapshot import create_backup
        create_backup(space, projection, store, bundle)

    head, _ = store.head(proof)
    state = {
        "ready": all(projection.projected_revision(p) == store.head(p)[0]
                     for p in store.proof_ids()),
        "proofs": {p: [store.head(p)[0], projection.projected_revision(p)]
                   for p in store.proof_ids()},
        "atoms_sha256": hashlib.sha256("\n".join(space.atoms()).encode()).hexdigest(),
        "pid": os.getpid(),
        "head": head,
        "watermark": projection.projected_revision(proof),
    }
    tmp = Path(f"{ready_path}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, ready_path)
    while True:  # serve until the supervisor or a test kills us
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
