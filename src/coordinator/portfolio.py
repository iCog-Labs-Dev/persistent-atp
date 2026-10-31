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

from .workspace import ProofWorkspace, project

__all__ = ["Slot", "PORTFOLIO", "SlotScheduler", "frontier_by_category", "run_portfolio"]


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
) -> list[dict[str, Any]]:
    """Run one cycle per slot that has work; report each as plain data."""
    proof_id = workspace.proof_id
    gate = CommitGate(workspace.view, store)
    reports = []
    for slot in slots:
        project(workspace.view, store, proof_id)
        if not frontier_by_category(workspace, store).get(slot.category):
            continue
        scheduler = SlotScheduler(
            workspace.view, store, category=slot.category, statistics=workspace.statistics
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
        )
        reports.append(_slot_report(slot, digest))
    project(workspace.view, store, proof_id)
    return reports


def _slot_report(slot: Slot, digest: CycleDigest) -> dict[str, Any]:
    return {
        "slot": slot.role,
        "worker_class": slot.worker_class,
        "move_id": digest.selected_move_id,
        "accepted": digest.accepted,
        "revision": digest.revision,
        "rejections": [rejection.reason.value for rejection in digest.rejections],
    }
