"""Canonical closed vocabularies for committed proof state.

Single source of truth for every status string, node label and enum literal
used by both the commit gate (``src/commit_gate/``) and the neo4j projection
(``neo4j/``). Section references are to the Unified Dual-Loop Architecture v3
technical design.

Conventions (see ``src/shared/tests/test_vocab_drift.py`` for the guards):

* **Separator**: multi-word literals use a hyphen (``critic-accepted``), never
  an underscore. ``LEGACY_STATUS_ALIASES`` maps the retired underscore forms.
* **Labels**: the gate's names are canonical (``FormalState``, not ``State``).
  ``GRAPH_TO_GATE_LABEL`` / ``GATE_TO_GRAPH_LABEL`` translate to and from the
  short labels the graph stores.
* **Statuses are a union**: statuses that only the graph used (``tainted``,
  ``reopened``, ``queued``, ``leased`` ...) are declared here too, so neither
  side can write a literal the other cannot read.
"""

from enum import StrEnum

__all__ = [
    "AttemptStatus",
    "ClaimStatus",
    "FormalStateStatus",
    "StateStatus",
    "StateKind",
    "ResearchStateStatus",
    "ResearchMoveStatus",
    "DeclarationStatus",
    "CertificateStatus",
    "AlignmentLifecycle",
    "AlignmentVerdict",
    "RunDisposition",
    "ExecutorResult",
    "TacticStatus",
    "MoveStatus",
    "ReplayStatus",
    "ObstructionKind",
    "EvidenceKind",
    "WorkerClass",
    "ANNOTATION_FIELDS",
    "TERMINAL_EXECUTOR_FAILURES",
    "NON_KERNEL_TACTICS",
    "STANDARD_LEAN_AXIOMS",
    "GATE_LABELS",
    "GRAPH_LABELS",
    "GRAPH_ONLY_LABELS",
    "GRAPH_TO_GATE_LABEL",
    "GATE_TO_GRAPH_LABEL",
    "LEGACY_STATUS_ALIASES",
    "gate_label",
    "graph_label",
    "canonical_status",
    "values",
]


class ClaimStatus(StrEnum):
    """Research claim status (11.1)."""

    CONJECTURAL = "conjectural"
    EMPIRICAL = "empirical"
    PROVISIONAL = "provisional"
    CRITIC_ACCEPTED = "critic-accepted"
    FORMALLY_CLOSED = "formally-closed"
    LEAN_VERIFIED = "lean-verified"
    TAINTED = "tainted"
    REFUTED = "refuted"
    RETRACTED = "retracted"
    STALE = "stale"


class FormalStateStatus(StrEnum):
    """Formal state operational status (11.1).

    ``TAINTED`` / ``REOPENED`` are the graph-side taint lifecycle (4.10): a
    state whose supporting claim was refuted is tainted, and a closed state
    that must be searched again is reopened.
    """

    OPEN = "open"
    EXPANDED = "expanded"
    FORMALLY_CLOSED = "formally-closed"
    LEAN_VERIFIED = "lean-verified"
    TAINTED = "tainted"
    REOPENED = "reopened"
    FAILED = "failed"
    PRUNED = "pruned"
    STALE = "stale"


class StateKind(StrEnum):
    """AND/OR search-graph role of a formal state (9.6)."""

    OR = "or"
    AND = "and"
    GOAL = "goal"


class ResearchStateStatus(StrEnum):
    """Research state operational status.

    The minimal lifecycle the global scheduler needs: a move whose parent
    state is ``superseded`` or ``refuted`` leaves the eligible frontier
    til the taint is resolved.
    """

    OPEN = "open"
    SUPERSEDED = "superseded"
    REFUTED = "refuted"
    STALE = "stale"


class ResearchMoveStatus(StrEnum):
    """Research move status: leasing plus pruning outcomes.

    ``queued``/``open`` are the frontier statuses the scheduler leases from;
    ``leased`` marks an active dispatch; ``refuted``/``dominated``/
    ``exhausted`` are pruning outcomes.
    """

    QUEUED = "queued"
    OPEN = "open"
    LEASED = "leased"
    CLOSED = "closed"
    REFUTED = "refuted"
    DOMINATED = "dominated"
    EXHAUSTED = "exhausted"
    STALE = "stale"


class TacticStatus(StrEnum):
    """Tactic application status, derived from child closure (B.3).

    Also the status of a graph ``Move``: ``QUEUED``/``LEASED`` are the leasing
    lifecycle (4.7) and ``REFUTED``/``DOMINATED``/``EXHAUSTED`` are the
    pruning outcomes.
    """

    PENDING = "pending"
    QUEUED = "queued"
    OPEN = "open"
    LEASED = "leased"
    CLOSED = "closed"
    REOPENED = "reopened"
    REFUTED = "refuted"
    DOMINATED = "dominated"
    EXHAUSTED = "exhausted"
    DEAD = "dead"


class AttemptStatus(StrEnum):
    """Provenance attempt outcome (11.2)."""

    PENDING = "pending"
    SUPPORTED = "supported"
    CRITIC_ACCEPTED = "critic-accepted"
    REFUTED = "refuted"
    RETRACTED = "retracted"


class DeclarationStatus(StrEnum):
    """Formal declaration lifecycle (11.1)."""

    DRAFT = "draft"
    ALIGNED = "aligned"
    SEARCHING = "searching"
    CERTIFICATE_PRODUCED = "certificate-produced"
    REPLAY_PENDING = "replay-pending"
    REPLAY_ACCEPTED = "replay-accepted"
    REPLAY_REJECTED = "replay-rejected"
    STALE = "stale"


class CertificateStatus(StrEnum):
    """Certificate lifecycle (11.4)."""

    CANDIDATE = "candidate"
    REPLAY_PENDING = "replay-pending"
    REPLAY_ACCEPTED = "replay-accepted"
    REPLAY_REJECTED = "replay-rejected"
    STALE = "stale"


class AlignmentLifecycle(StrEnum):
    """Review stage of an alignment record (5.4)."""

    DRAFT = "draft"
    REVIEW_NEEDED = "review-needed"
    REVIEWED = "reviewed"
    SUPERSEDED = "superseded"
    STALE = "stale"


class AlignmentVerdict(StrEnum):
    """Reviewer's field-by-field conclusion (C.4)."""

    ALIGNED = "aligned"
    WEAKER = "weaker"
    STRONGER = "stronger"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"


class RunDisposition(StrEnum):
    """Formal run result disposition (6.10)."""

    SEARCHING = "searching"
    PROVED_PENDING_REPLAY = "proved-pending-replay"
    BUDGET_EXHAUSTED = "budget-exhausted"
    STAGNATED = "stagnated"
    COUNTEREXAMPLE = "counterexample"
    INVALID_REQUEST = "invalid-request"
    ENVIRONMENT_ERROR = "environment-error"
    INTERNAL_ERROR = "internal-error"
    CANCELLED = "cancelled"


class ExecutorResult(StrEnum):
    """What the Lean/Pantograph executor reported (11.3)."""

    LEAN_ACCEPTED = "lean-accepted"
    LEAN_REJECTED = "lean-rejected"
    TIMEOUT = "timeout"
    BACKEND_MISSING = "backend-missing"
    PARSE_FAILURE = "parse-failure"
    CRASH = "crash"
    EMPTY_OUTPUT = "empty-output"


TERMINAL_EXECUTOR_FAILURES = frozenset(
    {
        ExecutorResult.TIMEOUT,
        ExecutorResult.BACKEND_MISSING,
        ExecutorResult.PARSE_FAILURE,
        ExecutorResult.CRASH,
        ExecutorResult.EMPTY_OUTPUT,
    }
)
"""Executor outcomes that are infrastructure failures, not mathematical ones."""

NON_KERNEL_TACTICS = frozenset({"PLN_fallback"})
"""Tactic labels that close branches without Lean kernel evidence.

The hybrid reasoner's PLN fallback marks a branch solved when its STV score
is high enough -- no tactic ever ran. An edge with one of these labels can
never carry `lean-accepted` semantics or close a state, no matter what an
adapter or worker claims in other fields.
"""

STANDARD_LEAN_AXIOMS = frozenset(
    {"propext", "Classical.choice", "Quot.sound"}
)
"""The three axioms Lean 4's standard library is built on.

Nearly every mathlib proof transitively depends on them, so they are the
default allowlist for replay axiom policy: what is denied is a *fresh*
`axiom` declaration outside this closure -- the actual soundness hole --
not the logical foundation of the ecosystem.
"""


class ReplayStatus(StrEnum):
    """Independent replay outcome (6.11)."""

    VERIFIED = "verified"
    REJECTED = "rejected"


class ObstructionKind(StrEnum):
    """Typed obstruction taxonomy (7.4)."""

    MISSING_LEMMA = "missing-lemma"
    MISSING_PREMISE = "missing-premise"
    REPRESENTATION_MISMATCH = "representation-mismatch"
    STATEMENT_TOO_STRONG = "statement-too-strong"
    STATEMENT_TOO_WEAK = "statement-too-weak"
    LIBRARY_GAP = "library-gap"
    ELABORATION = "elaboration"
    TYPECLASS = "typeclass"
    COERCION = "coercion"
    RESOURCE = "resource"
    SEARCH_POLICY = "search-policy"
    LIKELY_FALSE = "likely-false"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """Evidence classes from the 11.5 permitted-conclusion table."""

    LEAN_REPLAY = "lean-replay"
    RANDOM_SAMPLING = "random-sampling"
    EXHAUSTIVE_FINITE_SEARCH = "exhaustive-finite-search"
    EXACT_SYMBOLIC = "exact-symbolic"
    INTERVAL_ARITHMETIC = "interval-arithmetic"
    CHECKABLE_CERTIFICATE = "checkable-certificate"
    HEURISTIC_OPTIMIZER = "heuristic-optimizer"
    MODEL_SCORE = "model-score"
    CRITIC_REVIEW = "critic-review"
    HUMAN_GUIDANCE = "human-guidance"


class WorkerClass(StrEnum):
    """Proposal actors (3.2)."""

    COORDINATOR = "coordinator"
    LLM_RESEARCH = "llm-research"
    HYPERON = "hyperon"
    FORMAL_ATP = "formal-atp"
    REPLAYER = "replayer"
    CRITIC = "critic"
    EXPERIMENT = "experiment"
    ALIGNMENT_REVIEWER = "alignment-reviewer"
    HUMAN = "human"
    MAINTENANCE = "maintenance"


ANNOTATION_FIELDS = frozenset(
    {
        "gnn_tactic_prior",
        "argument_probability",
        "premise_relevance",
        "pln_strength",
        "pln_confidence",
        "proof_number",
        "disproof_number",
        "depth",
        "estimated_execution_cost",
        "state_novelty",
        "transposition_count",
        "failure_family",
        "dependency_centrality",
        "expected_information_gain",
        "verification_value",
        "repeated_failure_risk",
        "expected_theorem_impact",
        "novelty_and_mechanism_diversity",
        "formalization_readiness",
        "estimated_cost",
        "human_priority",
        "availability_of_suitable_worker_or_model_or_tool",
        "derived_priority",
    }
)
"""Heuristic score fields (2.4, A.5). Writes to these are annotation-class."""


# Graph-side aliases. The graph calls a FormalState a State and a
# TacticApplication a Move; the vocabulary behind both names is the same.
StateStatus = FormalStateStatus
MoveStatus = TacticStatus


GRAPH_TO_GATE_LABEL: dict[str, str] = {
    "Proof": "Proof",
    "State": "FormalState",
    "Move": "TacticApplication",
    "Claim": "Claim",
    "Attempt": "Attempt",
    "Artifact": "Artifact",
    "Hypothesis": "SpeculativeHypothesis",
    # Graph-only layers, not yet modelled by the gate: identity mapping keeps
    # the translation total instead of raising on labels it has never seen.
    "Route": "Route",
    "Context": "Context",
    "Concept": "Concept",
    "Critique": "Critique",
    "Experiment": "Experiment",
    "Verification": "Verification",
}
"""Short graph label -> canonical gate label."""

GATE_TO_GRAPH_LABEL: dict[str, str] = {
    gate: graph for graph, gate in GRAPH_TO_GATE_LABEL.items()
}

GRAPH_LABELS = frozenset(GRAPH_TO_GATE_LABEL)

GRAPH_ONLY_LABELS = frozenset(
    {"Route", "Context", "Concept", "Critique", "Experiment", "Verification"}
)
"""Graph labels with no gate counterpart yet (tracked by #8)."""

GATE_LABELS = frozenset(
    {
        "Proof",
        "FormalState",
        "TacticApplication",
        "FormalDeclaration",
        "FormalRun",
        "FormalCheckpoint",
        "Certificate",
        "LeanReplay",
        "Environment",
        "Alignment",
        "Claim",
        "SpeculativeHypothesis",
        "Obstruction",
        "Attempt",
        "Artifact",
        "ResearchState",
        "ResearchMove",
    }
    | GRAPH_ONLY_LABELS
)
"""Every label the gate may validate a proposal against."""

LEGACY_STATUS_ALIASES: dict[str, str] = {
    "critic_accepted": ClaimStatus.CRITIC_ACCEPTED.value,
    "lean_verified": ClaimStatus.LEAN_VERIFIED.value,
    # The graph used to close a State with the same literal as a Move.
    "closed": FormalStateStatus.FORMALLY_CLOSED.value,
}
"""Retired literals -> canonical literal, resolved by `canonical_status`."""


def values(enum_class: type[StrEnum]) -> frozenset[str]:
    """The literal set of an enum, for membership checks in either package."""
    return frozenset(member.value for member in enum_class)


def gate_label(label: str) -> str:
    """Canonical gate label for a graph label (identity for gate labels)."""
    if label in GATE_LABELS and label not in GRAPH_TO_GATE_LABEL:
        return label
    try:
        return GRAPH_TO_GATE_LABEL[label]
    except KeyError:
        raise ValueError(f"unknown label {label!r}") from None


def graph_label(label: str) -> str:
    """Short graph label for a gate label (identity for graph labels)."""
    if label in GRAPH_LABELS and label not in GATE_TO_GRAPH_LABEL:
        return label
    try:
        return GATE_TO_GRAPH_LABEL[label]
    except KeyError:
        raise ValueError(f"unknown label {label!r}") from None


def canonical_status(value: str, allowed: frozenset[str], field: str = "status") -> str:
    """Return `value` as its canonical literal, or raise `ValueError`.

    Accepts the retired underscore spellings so data written before the
    vocabularies were merged still loads, but never returns them.
    """
    if value in allowed:
        return value
    hyphenated = value.replace("_", "-")
    if hyphenated in allowed:
        return hyphenated
    aliased = LEGACY_STATUS_ALIASES.get(value)
    if aliased is not None and aliased in allowed:
        return aliased
    raise ValueError(f"invalid {field} {value!r}; expected one of {sorted(allowed)}")
