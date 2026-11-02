"""The coordinator's outer loop as a LangGraph StateGraph (Sections 10.2, 12.1).

    plan_round --Send per active proof--> work --> assess --> plan_round | END

`plan_round` opens a round and fans the active proofs out with `Send`; each
`work` branch runs one proof's portfolio on its own store connection, so the
expensive cycles of different proofs overlap. `assess` joins the round: it
judges every proof from committed state, commits the escalations the
stagnation policy calls for and decides whether another round is worth
running.

The graph state is a cache. It holds round counters, stall counts and
messages, never proof state: every decision is re-read from the journal, and
the only writes are proposals through the commit gate. A checkpointer may be
passed to resume a run, but losing its checkpoints loses no proof work -- a
fresh coordinator on the same journal sees the same escalations on record and
does not raise them again.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .policy import StagnationPolicy
from .portfolio import PORTFOLIO, Slot
from .rounds import ACTIVE, SOLVED, assess, new_track, work
from .workspace import ProofWorkspace

__all__ = ["CoordinatorState", "Coordinator", "HALTED", "BUDGET_EXHAUSTED"]

HALTED = "halted"
"""No proof can move without a replay or a human; each track says which."""
BUDGET_EXHAUSTED = "budget-exhausted"


class CoordinatorState(TypedDict, total=False):
    round: int
    tracks: dict[str, dict[str, Any]]
    reports: Annotated[list[dict[str, Any]], operator.add]
    escalations: Annotated[list[dict[str, Any]], operator.add]
    milestones: Annotated[list[str], operator.add]
    outcome: str | None


class WorkOrder(TypedDict):
    proof_id: str
    round: int


class Coordinator:
    """Runs rounds over several proofs until each is solved or the budget ends."""

    def __init__(
        self,
        workspaces: Sequence[ProofWorkspace],
        *,
        max_rounds: int = 8,
        policy: StagnationPolicy = StagnationPolicy(),
        slots: tuple[Slot, ...] = PORTFOLIO,
    ):
        if max_rounds < 1:
            raise ValueError("a coordinator run needs at least one round")
        self.workspaces = {workspace.proof_id: workspace for workspace in workspaces}
        self.max_rounds = max_rounds
        self.policy = policy
        self.slots = slots

    def build(self, checkpointer=None):
        graph = StateGraph(CoordinatorState)
        graph.add_node("plan_round", self._plan_round)
        graph.add_node("work", self._work)
        graph.add_node("assess", self._assess)
        graph.add_edge(START, "plan_round")
        graph.add_conditional_edges("plan_round", self._fan_out, ["work"])
        graph.add_edge("work", "assess")
        graph.add_conditional_edges("assess", self._route, ["plan_round", END])
        return graph.compile(checkpointer=checkpointer)

    def initial_state(self) -> CoordinatorState:
        return {
            "round": 0,
            "tracks": {proof_id: new_track() for proof_id in self.workspaces},
            "reports": [],
            "escalations": [],
            "milestones": [],
            "outcome": None,
        }

    def run(self, *, checkpointer=None, thread_id: str = "coordinator") -> CoordinatorState:
        app = self.build(checkpointer)
        config = {
            "configurable": {"thread_id": thread_id},
            # Three steps a round, plus the start and a margin.
            "recursion_limit": 3 * self.max_rounds + 5,
        }
        return app.invoke(self.initial_state(), config)

    # -- nodes ---------------------------------------------------------------

    def _plan_round(self, state: CoordinatorState) -> dict[str, Any]:
        return {"round": state["round"] + 1}

    def _fan_out(self, state: CoordinatorState) -> list[Send]:
        return [
            Send("work", {"proof_id": proof_id, "round": state["round"]})
            for proof_id, track in state["tracks"].items()
            if track["status"] == ACTIVE
        ]

    def _work(self, order: WorkOrder) -> dict[str, Any]:
        workspace = self.workspaces[order["proof_id"]]
        return {"reports": [work(workspace, order["round"], slots=self.slots)]}

    def _assess(self, state: CoordinatorState) -> dict[str, Any]:
        tracks = dict(state["tracks"])
        escalations: list[dict[str, Any]] = []
        milestones: list[str] = []
        for report in state["reports"]:
            if report["round"] != state["round"]:
                continue
            proof_id = report["proof_id"]
            track, raised, messages = assess(
                self.workspaces[proof_id], tracks[proof_id], report, policy=self.policy
            )
            tracks[proof_id] = track
            escalations.extend({"proof_id": proof_id, "round": report["round"], **e} for e in raised)
            milestones.extend(messages)

        statuses = {track["status"] for track in tracks.values()}
        outcome = None
        if statuses == {SOLVED}:
            outcome = SOLVED
        elif ACTIVE not in statuses:
            outcome = HALTED
        elif state["round"] >= self.max_rounds:
            outcome = BUDGET_EXHAUSTED
        if outcome is not None:
            milestones.append(f"coordinator: {outcome} after {state['round']} rounds")
        return {
            "tracks": tracks,
            "escalations": escalations,
            "milestones": milestones,
            "outcome": outcome,
        }

    def _route(self, state: CoordinatorState) -> str:
        return END if state.get("outcome") else "plan_round"
