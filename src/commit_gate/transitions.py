"""Legal status transitions and immutable fields for the commit gate.

Derived from NEO4J_SCHEMA.md §5 (Vocabulary) and §11 (Architecture).
"""

from __future__ import annotations

from typing import Final

__all__ = ["STATUS_TRANSITIONS", "IMMUTABLE_FIELDS"]

# (Label, field) -> {from_value -> {allowed_to_values}}
STATUS_TRANSITIONS: Final[dict[tuple[str, str], dict[str, frozenset[str]]]] = {
    ("FormalState", "status"): {
        "open": frozenset({"expanded", "formally-closed", "failed", "pruned", "tainted", "stale"}),
        "expanded": frozenset({"formally-closed", "failed", "pruned", "tainted", "stale"}),
        "formally-closed": frozenset({"lean-verified", "tainted", "stale"}),
        "lean-verified": frozenset({"tainted", "stale"}),
        "tainted": frozenset({"reopened", "pruned", "stale"}),
        "reopened": frozenset(
            {"expanded", "formally-closed", "failed", "pruned", "tainted", "stale"}
        ),
        "failed": frozenset({"stale"}),
        "pruned": frozenset({"stale"}),
        "stale": frozenset(),
    },
    ("Claim", "status"): {
        "conjectural": frozenset(
            {"empirical", "provisional", "tainted", "refuted", "retracted", "stale"}
        ),
        "empirical": frozenset({"provisional", "tainted", "refuted", "retracted", "stale"}),
        "provisional": frozenset(
            {"critic-accepted", "formally-closed", "tainted", "refuted", "retracted", "stale"}
        ),
        "critic-accepted": frozenset(
            {"formally-closed", "tainted", "refuted", "retracted", "stale"}
        ),
        "formally-closed": frozenset(
            {"lean-verified", "tainted", "refuted", "retracted", "stale"}
        ),
        "lean-verified": frozenset({"tainted", "stale"}),
        "tainted": frozenset({"refuted", "retracted", "stale"}),
        "refuted": frozenset({"stale"}),
        "retracted": frozenset({"stale"}),
        "stale": frozenset(),
    },
    ("FormalDeclaration", "status"): {
        "draft": frozenset({"aligned", "searching", "stale"}),
        "aligned": frozenset({"searching", "stale"}),
        "searching": frozenset({"certificate-produced", "stale"}),
        "certificate-produced": frozenset({"replay-pending", "stale"}),
        "replay-pending": frozenset({"replay-accepted", "replay-rejected", "stale"}),
        "replay-accepted": frozenset({"stale"}),
        "replay-rejected": frozenset({"stale"}),
        "stale": frozenset(),
    },
    ("Certificate", "status"): {
        "candidate": frozenset({"replay-pending", "stale"}),
        "replay-pending": frozenset({"replay-accepted", "replay-rejected", "stale"}),
        "replay-accepted": frozenset({"stale"}),
        "replay-rejected": frozenset({"stale"}),
        "stale": frozenset(),
    },
    ("Alignment", "lifecycle"): {
        "draft": frozenset({"review-needed", "superseded", "stale"}),
        "review-needed": frozenset({"reviewed", "superseded", "stale"}),
        "reviewed": frozenset({"superseded", "stale"}),
        "superseded": frozenset({"stale"}),
        "stale": frozenset(),
    },
    ("FormalRun", "status"): {
        "searching": frozenset(
            {
                "proved-pending-replay",
                "budget-exhausted",
                "stagnated",
                "counterexample",
                "invalid-request",
                "environment-error",
                "internal-error",
                "cancelled",
            }
        ),
        # All of the above are terminal (no further transitions possible)
        "proved-pending-replay": frozenset(),
        "budget-exhausted": frozenset(),
        "stagnated": frozenset(),
        "counterexample": frozenset(),
        "invalid-request": frozenset(),
        "environment-error": frozenset(),
        "internal-error": frozenset(),
        "cancelled": frozenset(),
    },
    # A graph Move is a TacticApplication: queued/leased are the leasing
    # lifecycle (4.7), refuted/dominated/exhausted the pruning outcomes.
    ("TacticApplication", "status"): {
        "pending": frozenset({"queued", "open", "closed", "dead"}),
        "queued": frozenset({"leased", "open", "dominated", "dead"}),
        "leased": frozenset({"open", "closed", "refuted", "exhausted", "dead"}),
        "open": frozenset(
            {"leased", "closed", "refuted", "dominated", "exhausted", "dead"}
        ),
        "closed": frozenset({"reopened"}),
        "reopened": frozenset(
            {"leased", "closed", "refuted", "dominated", "exhausted", "dead"}
        ),
        "refuted": frozenset(),
        "dominated": frozenset(),
        "exhausted": frozenset(),
        "dead": frozenset(),
    },
}

# Fields that can be set in UpsertNode but never changed via SetField.
# Mapped by node label. Derived from the "Immutable" notes in the schema.
IMMUTABLE_FIELDS: Final[dict[str, frozenset[str]]] = {
    "FormalState": frozenset({"goal_text", "exact_hash", "is_theorem"}),
    "TacticApplication": frozenset(
        {
            "tactic_label",
            "tactic_family",
            "subgoal_count",
            "executor_result",
            "diagnostic_artifact",
        }
    ),
    "Claim": frozenset({"claim_text", "tag"}),
    "FormalDeclaration": frozenset(
        {"lean_name", "lean_type", "lean_value", "module_path", "universe_level"}
    ),
    "FormalRun": frozenset({"actor", "start_time"}),
    "Certificate": frozenset({"actor", "producer_run_id", "artifact_hash"}),
    "LeanReplay": frozenset( {"actor", "replayed_at", "status", "rejection_reason", "sorry_detected"} ),
    "Obstruction": frozenset({"kind", "description", "actor"}),
    "Proof": frozenset({"actor"}),
    "Alignment": frozenset({"actor"}),
    "Attempt": frozenset({"actor", "worker_class"}),
    "Environment": frozenset({"toolchain", "lake_manifest_hash", "mathlib_commit"}),
    "FormalCheckpoint": frozenset({"epoch_ms", "actor"}),
}
