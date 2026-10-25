"""The proof-cycle coordinator: rounds, portfolio and stagnation policy.

The core (`policy`, `progress`, `escalate`, `portfolio`, `rounds`) needs only
the commit gate and the scheduler. `coordinator.graph` wires it as a LangGraph
StateGraph and needs the optional `coordinator` extra.
"""

from .escalate import COORDINATOR_ACTOR, commit_escalations, escalation_ops
from .policy import Escalation, StagnationPolicy, classify
from .portfolio import PORTFOLIO, Slot, SlotScheduler, run_portfolio
from .progress import ProofReading, read_proof
from .rounds import assess, new_track, work
from .workspace import ProofWorkspace, project

__all__ = [
    "COORDINATOR_ACTOR",
    "Escalation",
    "PORTFOLIO",
    "ProofReading",
    "ProofWorkspace",
    "Slot",
    "SlotScheduler",
    "StagnationPolicy",
    "assess",
    "classify",
    "commit_escalations",
    "escalation_ops",
    "new_track",
    "project",
    "read_proof",
    "run_portfolio",
    "work",
]
