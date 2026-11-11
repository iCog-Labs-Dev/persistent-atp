from __future__ import annotations

from typing import Any, Optional

from neo4j import Driver

from .constants import GRAPH_LABELS
from .session import translate_errors

# Relationship types accepted by the generic add_relation() linker.
# Keeping an explicit allowlist means relationship type is never interpolated
# from untrusted input.
REL_WHITELIST = {
    # search DAG
    "SUPERSEDES", "ALTERNATIVE_TO", "GENERALIZES", "REFORMULATES", "FORMALIZES",
    "CONTRADICTS", "STRENGTHENS_ROUTE", "LEAVES_OPEN", "REDUCES_TARGET",
    "EXPOSES_BARRIER", "BYPASSES",
    # justification DAG
    "SUPPORTED_BY", "PROVED_BY", "CONTRADICTED_BY", "VERIFIED_BY",
    "VERIFIED_BY_EXPERIMENT", "INVALIDATES",
    # state -> claim reference (used for taint reopening)
    "USES_CLAIM",
    # speculative layer
    "SUGGESTS", "EXPECTS", "SOURCE_CONCEPT", "RELATED_TO", "FALSIFIED_BY",
    "ELABORATED_INTO",
}

# All node labels in the metagraph, from the shared vocabulary.
LABELS = tuple(sorted(GRAPH_LABELS))


def ensure_constraints(driver: Driver, database: Optional[str] = None) -> None:
    """Create composite (proof_id, id) UNIQUE constraints and status indexes.

    Schema commands cannot share a transaction with other statements, so these
    stay one statement per transaction; ``IF NOT EXISTS`` keeps them idempotent,
    which is what makes a partial run harmless.
    """
    with translate_errors("ensure_constraints"):
        with driver.session(database=database) as s:
            for label in LABELS:
                s.run(
                    f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE (n.proof_id, n.id) IS UNIQUE"
                )
            for label, prop in (("State", "status"), ("Move", "status"), ("Claim", "status")):
                s.run(
                    f"CREATE INDEX {label.lower()}_{prop} IF NOT EXISTS "
                    f"FOR (n:{label}) ON (n.proof_id, n.{prop})"
                )
