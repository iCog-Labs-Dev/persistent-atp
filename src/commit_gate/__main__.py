"""Inspect and repair the graph a running gate validates against.

    scripts/with-mork.sh python -m commit_gate status   --db journal.db p1
    scripts/with-mork.sh python -m commit_gate atoms    --db journal.db p1
    scripts/with-mork.sh python -m commit_gate catch-up --db journal.db

`status` answers the question worth asking after a crash: is the graph level with
the journal? `catch-up` makes it so, and is the one writer while it runs.
"""

from __future__ import annotations

import argparse
import sys

from mork.backend import MorkSpace, MorkUnavailable, MorkView

from .gate import CommitGate, ProjectionError
from .store import JournalStore


def open_gate(db_path: str) -> CommitGate:
    """A gate on `db_path`, validating against and projecting into MORK."""
    return CommitGate(MorkView(MorkSpace()), JournalStore(db_path))


def status(gate: CommitGate, proof_ids: list[str]) -> int:
    behind = 0
    for proof_id in proof_ids or list(gate.store.proof_ids()):
        head = gate.store.head(proof_id)[0]
        mark = gate.view.projected_revision(proof_id)
        level = "level" if mark == head else f"BEHIND by {head - mark}"
        print(f"{proof_id}: journal at {head}, graph at {mark} ({level})")
        behind += mark != head
    if behind:
        print(f"\n{behind} proof(s) behind; `catch-up` replays the difference.")
    return 1 if behind else 0


def atoms(gate: CommitGate, proof_ids: list[str]) -> int:
    for proof_id in proof_ids or list(gate.store.proof_ids()):
        found = gate.view.atoms(proof_id)
        print(f"# {proof_id}: {len(found)} atoms")
        for atom in found:
            print(atom)
    return 0


def catch_up(gate: CommitGate, proof_ids: list[str]) -> int:
    repaired = {}
    for proof_id in proof_ids or [None]:
        repaired.update(gate.catch_up(proof_id))
    if not repaired:
        print("nothing to replay; the graph is level with the journal")
        return 0
    for proof_id, count in repaired.items():
        print(f"{proof_id}: replayed {count} event(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m commit_gate")
    parser.add_argument("--db", default="journal.db", help="journal database path")
    parser.add_argument(
        "command", choices=["status", "atoms", "catch-up"], help="what to do"
    )
    parser.add_argument(
        "proof_ids", nargs="*", help="proofs to act on; default is all of them"
    )
    args = parser.parse_args(argv)

    try:
        gate = open_gate(args.db)
    except MorkUnavailable as exc:
        print(f"MORK is unavailable: {exc}", file=sys.stderr)
        print("Run this through scripts/with-mork.sh.", file=sys.stderr)
        return 69

    handler = {"status": status, "atoms": atoms, "catch-up": catch_up}[args.command]
    try:
        return handler(gate, args.proof_ids)
    except ProjectionError as exc:
        print(f"replay failed at revision {exc.revision}: {exc.cause}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
