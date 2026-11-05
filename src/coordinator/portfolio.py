"""A round's portfolio of cycles (Section 8.6): exploit, explore, critique.

`GlobalScheduler` ranks one frontier for every worker class, so a formal
worker can be handed a critic task. A portfolio slot pins a worker class to
its frontier category: the exploitation slot continues formal search, the
exploration slot works research moves (including the coordinator's own
escalations) and the critic slot reviews provisional claims. Each slot is one
`run_cycle`; within a proof the slots run in turn, because a proposal must be
based on the head it commits onto and two cycles on one proof would race.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from commit_gate.gate import CommitGate
from commit_gate.state import ReadView
from commit_gate.store import JournalStore
from mathproof.cycle import CycleDigest, run_cycle
from mathproof.scheduler import Candidate, GlobalScheduler, SchedulerStatistics

from .environment import pin_aligned_declarations
from .replay import finalize_accepted_replay
from .workspace import ProofWorkspace, project

__all__ = ["Slot", "PORTFOLIO", "AGENT_PORTFOLIO", "SlotScheduler", "frontier_by_category", "run_portfolio"]


@dataclass(frozen=True, slots=True)
class Slot:
    role: str
    category: str
    worker_class: str


PORTFOLIO = (
    Slot("exploit", "formal-run", "formal-atp"),
    Slot("explore", "research-move", "llm-research"),
    Slot("critic", "critic-task", "critic"),
)

AGENT_PORTFOLIO = (
    Slot("critic", "critic-task", "critic"),
    Slot("formalize", "formalization-task", "formalizer"),
    Slot("alignment", "alignment-task", "alignment-reviewer"),
    Slot("exploit", "formal-run", "formal-atp"),
    Slot("replay", "replay-task", "replayer"),
    Slot("explore", "research-move", "llm-research"),
    Slot("statement-check", "statement-check", "experiment"),
)


class SlotScheduler(GlobalScheduler):
    """The global scheduler, restricted to one frontier category."""

    def __init__(self, view, store, *, category: str, statistics: SchedulerStatistics):
        super().__init__(view, store, statistics=statistics)
        self.category = category

    def _build_frontier(
        self, proof_id: str, view: ReadView, node_ids: Mapping[str, None]
    ) -> list[Candidate]:
        return [
            candidate
            for candidate in super()._build_frontier(proof_id, view, node_ids)
            if candidate.category == self.category
        ]


def frontier_by_category(workspace: ProofWorkspace, store: JournalStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in GlobalScheduler(workspace.view, store).build_frontier(workspace.proof_id):
        counts[candidate.category] = counts.get(candidate.category, 0) + 1
    return counts


def run_portfolio(
    workspace: ProofWorkspace,
    store: JournalStore,
    *,
    slots: tuple[Slot, ...] = PORTFOLIO,
    round_number: int | None = None,
) -> list[dict[str, Any]]:
    """Run one cycle per slot that has work; report each as plain data."""
    proof_id = workspace.proof_id
    gate = CommitGate(workspace.view, store)
    reports = []
    for slot in slots:
        project(workspace.view, store, proof_id)
        _pin_environment(workspace, store)
        _finalize_replay(workspace, store)
        if not frontier_by_category(workspace, store).get(slot.category):
            workspace.emit(
                "slot_skipped", round=round_number, slot=slot.role,
                worker_class=slot.worker_class, reason="empty_frontier",
            )
            continue
        workspace.emit(
            "slot_started", round=round_number, slot=slot.role,
            worker_class=slot.worker_class,
        )
        scheduler = SlotScheduler(
            workspace.view, store, category=slot.category, statistics=workspace.statistics
        )
        def emit(event: str, fields: Mapping[str, Any]) -> None:
            workspace.emit(
                event, round=round_number, slot=slot.role, **dict(fields)
            )

        digest = run_cycle(
            proof_id,
            view=workspace.view,
            gate=gate,
            scheduler=scheduler,
            dispatcher=workspace.dispatcher,
            adapters=workspace.adapters,
            worker_class=slot.worker_class,
            maintenance=lambda proposal, commit: project(workspace.view, store, proof_id),
            capture_dispatch_errors=True,
            event_sink=emit,
        )
        report = _slot_report(slot, digest)
        reports.append(report)
        workspace.emit(
            "slot_completed",
            round=round_number,
            slot=slot.role,
            worker_class=slot.worker_class,
            move_id=digest.selected_move_id,
            revision=digest.revision,
            status=(
                "failed" if digest.error is not None
                else "accepted" if digest.accepted
                else "rejected"
            ),
        )
    project(workspace.view, store, proof_id)
    _pin_environment(workspace, store)
    _finalize_replay(workspace, store)
    return reports


def _pin_environment(workspace: ProofWorkspace, store: JournalStore) -> None:
    """Commit infrastructure-owned pins before a formal slot reads its frontier."""
    if workspace.environment is None:
        return
    result = pin_aligned_declarations(
        workspace.proof_id,
        workspace.environment,
        view=workspace.view,
        store=store,
    )
    if result is None:
        return
    if not result.accepted:
        reasons = ", ".join(rejection.reason.value for rejection in result.rejections)
        raise ValueError(f"environment pin rejected: {reasons}")
    project(workspace.view, store, workspace.proof_id)
    workspace.emit(
        "environment_pinned",
        revision=result.revision,
        environment_hash=workspace.environment.environment_hash,
    )


def _finalize_replay(workspace: ProofWorkspace, store: JournalStore) -> None:
    result = finalize_accepted_replay(
        workspace.proof_id, view=workspace.view, store=store
    )
    if result is None:
        return
    if not result.accepted:
        reasons = ", ".join(rejection.reason.value for rejection in result.rejections)
        raise ValueError(f"replay finalization rejected: {reasons}")
    project(workspace.view, store, workspace.proof_id)
    workspace.emit("replay_finalized", revision=result.revision)


def _slot_report(slot: Slot, digest: CycleDigest) -> dict[str, Any]:
    report = {
        "slot": slot.role,
        "worker_class": slot.worker_class,
        "move_id": digest.selected_move_id,
        "accepted": digest.accepted,
        "revision": digest.revision,
        "rejections": [rejection.reason.value for rejection in digest.rejections],
    }
    if digest.error is not None:
        report["error"] = {"type": digest.error_type, "message": digest.error}
    return report
