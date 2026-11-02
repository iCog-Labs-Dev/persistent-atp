"""The stagnation policy (Section 10.4) as an ordered rule table.

`classify` reads committed state and returns the escalations due now, in the
order the policy applies them:

1. a refuted research attempt              -> repair it at the critic's gap
2. ``non_progress_rounds`` without progress -> the five-mechanism operator
3. one failure family repeated in a search  -> diversify the formal policy
4. runs stuck at one residual / a missing lemma -> bridge lemma (or a
   representation change when the residual is a representation mismatch)
5. a statement the formal side cannot match -> alignment review, which holds
   rules 3 and 4 for that declaration: searching harder on the wrong
   statement wastes the budget
6. global stagnation or an empty frontier  -> audit, literature, theorem
   review

The coordinator decides *that* something must happen, never the mathematics
of it: each escalation becomes a queued research move for a worker to take.
An escalation already on record is not raised again -- per target for the
local rules, per progress level for the stall rules (2 and 6) -- so the same
journal always yields the same decisions, whatever the coordinator cached.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from commit_gate.state import MemoryView
from shared.vocab import ObstructionKind, RunDisposition

from .progress import (
    ProofReading,
    coordinator_moves,
    misaligned_declarations,
    obstructions_by_run,
    refuted_reviews,
    residual_of,
    run_declaration,
)

__all__ = [
    "Escalation",
    "StagnationPolicy",
    "classify",
    "REPAIR",
    "FIVE_MECHANISMS",
    "DIVERSIFY",
    "BRIDGE_LEMMA",
    "ALIGNMENT_REVIEW",
    "GLOBAL_AUDIT",
    "RULES",
]

REPAIR = "repair"
FIVE_MECHANISMS = "five-mechanisms"
DIVERSIFY = "diversify-formal"
BRIDGE_LEMMA = "bridge-lemma"
ALIGNMENT_REVIEW = "alignment-review"
GLOBAL_AUDIT = "global-audit"
RULES = (REPAIR, FIVE_MECHANISMS, DIVERSIFY, BRIDGE_LEMMA, ALIGNMENT_REVIEW, GLOBAL_AUDIT)

STALL_RULES = frozenset({FIVE_MECHANISMS, GLOBAL_AUDIT})
"""Raised once per progress level rather than once per target."""

MISSING_KNOWLEDGE = frozenset(
    {
        ObstructionKind.MISSING_LEMMA.value,
        ObstructionKind.MISSING_PREMISE.value,
        ObstructionKind.LIBRARY_GAP.value,
        ObstructionKind.REPRESENTATION_MISMATCH.value,
    }
)
STATEMENT_DOUBTS = frozenset(
    {
        ObstructionKind.STATEMENT_TOO_STRONG.value,
        ObstructionKind.STATEMENT_TOO_WEAK.value,
        ObstructionKind.LIKELY_FALSE.value,
    }
)
"""Obstructions that question the statement, not the search."""


@dataclass(frozen=True, slots=True)
class Escalation:
    """One action the policy calls for, before it is proposed."""

    rule: str
    target_id: str
    detail: str
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class StagnationPolicy:
    """Thresholds, as configuration rather than code."""

    non_progress_rounds: int = 2
    failure_family_repeat: int = 2
    global_stall_rounds: int = 4


def classify(
    view: MemoryView,
    proof_id: str,
    reading: ProofReading,
    *,
    stalled_rounds: int,
    frontier_empty: bool,
    policy: StagnationPolicy = StagnationPolicy(),
) -> list[Escalation]:
    """The escalations due for this proof, in policy order, minus those on record."""
    if reading.solved:
        return []
    raised: list[Escalation] = []

    # Rule 1: a refuted attempt is repaired where the critic found the gap.
    for attempt, claim_id in refuted_reviews(view, proof_id):
        gap = attempt.fields.get("earliest_gap")
        where = f" at {gap}" if gap else ""
        raised.append(
            Escalation(REPAIR, attempt.node_id, f"repair {claim_id}{where}", claim_id=claim_id)
        )

    # A certificate waiting for replay is not stagnation: the formal side is
    # paused, not stuck (Section 10.3), so the stall rules wait for it.
    stall_rules_apply = not reading.awaiting_replay

    # Rule 2: repeated non-progress calls for genuinely different ideas.
    if stall_rules_apply and stalled_rounds >= policy.non_progress_rounds:
        raised.append(
            Escalation(
                FIVE_MECHANISMS,
                proof_id,
                f"no verified progress in {stalled_rounds} rounds; propose five mechanisms",
            )
        )

    # Statements in doubt are found before rules 3 and 4, which skip them.
    doubted = dict(misaligned_declarations(view, proof_id))
    by_run = obstructions_by_run(view, proof_id)
    for run_id, obstructions in by_run.items():
        declaration_id = run_declaration(view, run_id)
        if declaration_id is None:
            continue
        for obstruction in obstructions:
            if obstruction.fields.get("kind") in STATEMENT_DOUBTS:
                doubted.setdefault(declaration_id, obstruction.fields["kind"])

    # Rule 3: the same failure family again and again on one declaration.
    families: dict[str, Counter] = defaultdict(Counter)
    for run_id, obstructions in by_run.items():
        declaration_id = run_declaration(view, run_id)
        if declaration_id is None or declaration_id in doubted:
            continue
        for obstruction in obstructions:
            kind = obstruction.fields.get("kind")
            if kind not in MISSING_KNOWLEDGE | STATEMENT_DOUBTS:
                families[declaration_id][kind] += 1
    for declaration_id, counts in families.items():
        for kind, seen in sorted(counts.items()):
            if seen >= policy.failure_family_repeat:
                raised.append(
                    Escalation(
                        DIVERSIFY,
                        declaration_id,
                        f"{kind} obstruction raised {seen} times; diversify the formal policy",
                    )
                )

    # Rule 4: a missing piece of knowledge, or several runs at one residual.
    residuals: dict[str, set[str]] = defaultdict(set)
    first_state: dict[str, str] = {}
    kinds: dict[str, set[str]] = defaultdict(set)
    for run_id, obstructions in by_run.items():
        declaration_id = run_declaration(view, run_id)
        if declaration_id in doubted:
            continue
        stuck = view.node(run_id).fields.get("status") == RunDisposition.STAGNATED.value
        for obstruction in obstructions:
            residual = residual_of(view, obstruction.node_id)
            if residual is None:
                continue
            state_id, signature = residual
            kind = obstruction.fields.get("kind")
            first_state.setdefault(signature, state_id)
            residuals[signature].add(run_id)
            if stuck and kind in MISSING_KNOWLEDGE:
                kinds[signature].add(kind)
    for signature, runs in residuals.items():
        if len(runs) < 2 and not kinds[signature]:
            continue
        change = (
            "a representation change"
            if kinds[signature] == {ObstructionKind.REPRESENTATION_MISMATCH.value}
            else "a bridge lemma"
        )
        why = f"{len(runs)} runs converge" if len(runs) > 1 else "the search lacks a lemma"
        raised.append(
            Escalation(
                BRIDGE_LEMMA,
                first_state[signature],
                f"{why} on {first_state[signature]}; propose {change}",
            )
        )

    # Rule 5: check the statement before searching it harder.
    for declaration_id, why in sorted(doubted.items()):
        raised.append(
            Escalation(
                ALIGNMENT_REVIEW,
                declaration_id,
                f"{why}: review the statement of {declaration_id} before more search",
            )
        )

    # Rule 6: nothing left to lease, or a long stall everywhere.
    if stall_rules_apply and (frontier_empty or stalled_rounds >= policy.global_stall_rounds):
        why = "the frontier is empty" if frontier_empty else f"{stalled_rounds} rounds stalled"
        raised.append(
            Escalation(
                GLOBAL_AUDIT,
                proof_id,
                f"{why}; audit claims, refresh literature, review the theorem",
            )
        )

    return _not_on_record(view, proof_id, reading, raised)


def _not_on_record(
    view: MemoryView, proof_id: str, reading: ProofReading, raised: list[Escalation]
) -> list[Escalation]:
    on_record = set()
    for move in coordinator_moves(view, proof_id):
        rule = move.fields["escalation"]
        basis = move.fields.get("basis") if rule in STALL_RULES else None
        on_record.add((rule, move.fields.get("target_id"), basis))
    fresh = []
    for escalation in raised:
        basis = reading.basis if escalation.rule in STALL_RULES else None
        key = (escalation.rule, escalation.target_id, basis)
        if key not in on_record:
            on_record.add(key)
            fresh.append(escalation)
    order = {rule: position for position, rule in enumerate(RULES)}
    return sorted(fresh, key=lambda escalation: order[escalation.rule])
