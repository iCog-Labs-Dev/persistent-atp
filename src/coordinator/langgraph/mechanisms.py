"""The five-mechanism operator's answer contract (Section 10.5).

A `five-mechanisms` escalation asks a research worker for five genuinely
different ways past the current gap. The coordinator does not write them --
it has no mathematical opinion -- but it fixes what an answer must state and
refuses one that is incomplete or not diverse, before any of it becomes a
queued move. Diversity is checked by technique tags: two mechanisms from the
same technique family are one idea twice.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from commit_gate.state import ReadView

__all__ = [
    "MECHANISM_COUNT",
    "MECHANISM_FIELDS",
    "STANCES",
    "check_mechanisms",
    "mechanism_moves",
]

MECHANISM_COUNT = 5

MECHANISM_FIELDS = (
    "operator",              # cognitive operator and technique family
    "technique_tags",
    "idea",                  # the central idea
    "bridge",                # the exact bridge lemma or reduction
    "why",                   # why it would close or clarify the gap
    "prerequisites",         # prerequisite claim IDs
    "formal_manifestation",  # what it is expected to look like in Lean
    "falsification_test",    # the earliest inexpensive test that could kill it
    "information_if_false",
    "promise",
    "cost",
    "novelty",
    "risk",
    "relation",              # relation to existing branches
    "stance",                # proves, bypasses, generalizes or reframes
    "first_step",            # first executable research or formal step
)

SCORES = ("promise", "cost", "novelty", "risk")
STANCES = frozenset({"proves", "bypasses", "generalizes", "reframes"})


def check_mechanisms(
    mechanisms: Sequence[Mapping[str, Any]], view: ReadView | None = None
) -> list[str]:
    """Every way `mechanisms` fails the contract; empty when it holds."""
    problems: list[str] = []
    if len(mechanisms) != MECHANISM_COUNT:
        problems.append(f"expected {MECHANISM_COUNT} mechanisms, got {len(mechanisms)}")
    seen_tags: dict[str, int] = {}
    for position, mechanism in enumerate(mechanisms):
        name = f"mechanism {position + 1}"
        missing = [field for field in MECHANISM_FIELDS if field not in mechanism]
        if missing:
            problems.append(f"{name} is missing {', '.join(missing)}")
            continue
        if mechanism["stance"] not in STANCES:
            problems.append(f"{name} has stance {mechanism['stance']!r}")
        for score in SCORES:
            value = mechanism[score]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                problems.append(f"{name} scores {score} outside [0, 1]")
        tags = mechanism["technique_tags"]
        if not tags:
            problems.append(f"{name} names no technique tag")
        for tag in tags:
            if tag in seen_tags:
                problems.append(
                    f"{name} repeats technique {tag!r} from mechanism {seen_tags[tag] + 1}"
                )
            else:
                seen_tags[tag] = position
        if view is not None:
            for claim_id in mechanism["prerequisites"]:
                record = view.node(claim_id)
                if record is None or record.label != "Claim":
                    problems.append(f"{name} depends on {claim_id!r}, which is not a claim")
    return problems


def mechanism_moves(mechanisms: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The research result's ``moves`` for a checked answer: one per mechanism."""
    return [
        {
            "detail": mechanism["first_step"],
            "mechanism": mechanism["idea"],
            **{field: mechanism[field] for field in MECHANISM_FIELDS},
        }
        for mechanism in mechanisms
    ]
