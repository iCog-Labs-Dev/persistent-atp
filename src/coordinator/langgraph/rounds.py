"""One coordinator round for one proof, without the graph around it.

`work` is the expensive half -- the portfolio's cycles -- and is what runs in
parallel across proofs. `assess` is the cheap half: read the committed
outcome, update the stall count, raise and commit escalations, and say what
happened in milestone messages (Section 10.7). Both exchange plain data only,
so whatever carries them between steps (a LangGraph checkpoint, say) holds a
cache of the journal and never a second copy of the proof.
"""

from __future__ import annotations

from typing import Any

from .escalate import commit_escalations
from .policy import StagnationPolicy, classify
from .portfolio import PORTFOLIO, Slot, frontier_by_category, run_portfolio
from .progress import ProofReading, read_proof
from .workspace import ProofWorkspace, project

__all__ = ["ACTIVE", "SOLVED", "PARKED", "STALLED", "new_track", "work", "assess"]

ACTIVE = "active"
SOLVED = "solved"
PARKED = "parked"
"""Waiting on replay with nothing else to lease: no cycle can move it."""
STALLED = "stalled"
"""Nothing to lease and every escalation already on record: a human decides."""


def new_track() -> dict[str, Any]:
    """What the coordinator remembers about a proof between rounds."""
    return {"status": ACTIVE, "progress": None, "stalled_rounds": 0, "awaiting_replay": False}


def work(
    workspace: ProofWorkspace, round_number: int, *, slots: tuple[Slot, ...] = PORTFOLIO
) -> dict[str, Any]:
    """Run this round's portfolio on one proof and report it."""
    with workspace.lock:
        store = workspace.open_store()
        revision = project(workspace.view, store, workspace.proof_id)
        before = read_proof(workspace.view, workspace.proof_id, revision)
        return {
            "proof_id": workspace.proof_id,
            "round": round_number,
            "before": list(before.progress),
            "slots": run_portfolio(workspace, store, slots=slots),
        }


def assess(
    workspace: ProofWorkspace,
    track: dict[str, Any],
    report: dict[str, Any],
    *,
    policy: StagnationPolicy = StagnationPolicy(),
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Judge the round from committed state; commit what the policy calls for.

    Returns the updated track, the escalations committed, and the milestones.
    """
    proof_id = workspace.proof_id
    round_number = report["round"]
    with workspace.lock:
        store = workspace.open_store()
        revision = project(workspace.view, store, proof_id)
        reading = read_proof(workspace.view, proof_id, revision)
        frontier = frontier_by_category(workspace, store)
        earlier = _reading_from(report["before"], revision)
        milestones = _progress_milestones(proof_id, earlier, reading)
        stalled_rounds = 0 if reading.advanced_from(earlier) else track["stalled_rounds"] + 1
        if reading.awaiting_replay and not track["awaiting_replay"]:
            milestones.append(f"{proof_id}: a certificate awaits replay; formal search pauses")

        escalations = classify(
            workspace.view,
            proof_id,
            reading,
            stalled_rounds=stalled_rounds,
            frontier_empty=not frontier,
            policy=policy,
        )
        committed: list[dict[str, Any]] = []
        commit = commit_escalations(
            store, workspace.view, proof_id, escalations, reading=reading, round_number=round_number
        )
        if commit is not None and commit.accepted:
            project(workspace.view, store, proof_id)
            for escalation in escalations:
                committed.append(
                    {"rule": escalation.rule, "target_id": escalation.target_id, "detail": escalation.detail}
                )
                milestones.append(f"{proof_id}: {escalation.rule} -- {escalation.detail}")
        elif commit is not None:
            reasons = ", ".join(rejection.reason.value for rejection in commit.rejections)
            milestones.append(f"{proof_id}: escalation proposal rejected ({reasons})")

        status = ACTIVE
        if reading.solved:
            status = SOLVED
            milestones.append(f"{proof_id}: solved; every declaration replayed")
        elif not frontier_by_category(workspace, store):
            if reading.awaiting_replay:
                status = PARKED
            else:
                status = STALLED
                milestones.append(
                    f"{proof_id}: nothing left to lease and every escalation is on record; "
                    "needs a human decision"
                )
        return (
            {
                "status": status,
                "progress": list(reading.progress),
                "stalled_rounds": stalled_rounds,
                "awaiting_replay": reading.awaiting_replay,
            },
            committed,
            milestones,
        )


def _reading_from(progress: list[int], revision: int) -> ProofReading:
    settled, closed, replayed = progress
    return ProofReading(
        revision=revision,
        settled_claims=settled,
        closed_states=closed,
        replayed_declarations=replayed,
    )


def _progress_milestones(
    proof_id: str, earlier: ProofReading, reading: ProofReading
) -> list[str]:
    messages = []
    for name, now, then in (
        ("claims settled", reading.settled_claims, earlier.settled_claims),
        ("formal states closed", reading.closed_states, earlier.closed_states),
        ("declarations replayed", reading.replayed_declarations, earlier.replayed_declarations),
    ):
        if now > then:
            messages.append(f"{proof_id}: {now - then} {name}")
    return messages
