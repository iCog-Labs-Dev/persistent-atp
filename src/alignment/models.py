"""Data structures for statement alignment review (Technical Design §5.4, §7.6)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict

__all__ = [
    "AlignmentCriteria",
    "AlignmentRecord",
    "AlignmentReviewRequest",
    "AlignmentReviewResult",
    "RelationType",
    "compute_content_hash",
]

RelationType = Literal["exact", "strengthening", "weakening", "reformulation"]


def compute_content_hash(text: str) -> str:
    """Compute sha256:<hex> content hash for statement or source."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True, slots=True)
class AlignmentCriteria:
    """The 8 alignment review criteria from §7.6."""

    quantifier_correspondence: str = ""
    domain_correspondence: str = ""
    universe_assumptions: tuple[str, ...] = ()
    typeclass_assumptions: tuple[str, ...] = ()
    classical_assumptions: tuple[str, ...] = ()
    constructive_assumptions: tuple[str, ...] = ()
    selected_definitions_match: bool = True
    implication_to_target_explicit: bool = True
    relation: RelationType = "exact"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantifier_correspondence": self.quantifier_correspondence,
            "domain_correspondence": self.domain_correspondence,
            "universe_assumptions": list(self.universe_assumptions),
            "typeclass_assumptions": list(self.typeclass_assumptions),
            "classical_assumptions": list(self.classical_assumptions),
            "constructive_assumptions": list(self.constructive_assumptions),
            "selected_definitions_match": self.selected_definitions_match,
            "implication_to_target_explicit": self.implication_to_target_explicit,
            "relation": self.relation,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class AlignmentReviewRequest:
    """Input request for statement alignment review."""

    proof_id: str
    claim_id: str
    declaration_id: str
    informal_statement: str
    formal_statement: str
    imports: tuple[str, ...] = ()
    namespace: str | None = None
    statement_hash: str = ""
    source_hash: str = ""

    def __post_init__(self) -> None:
        if not self.statement_hash and self.informal_statement:
            object.__setattr__(
                self,
                "statement_hash",
                compute_content_hash(self.informal_statement),
            )
        if not self.source_hash and self.formal_statement:
            object.__setattr__(
                self, "source_hash", compute_content_hash(self.formal_statement)
            )


@dataclass(frozen=True, slots=True)
class AlignmentReviewResult:
    """The outcome of an alignment review."""

    verdict: AlignmentVerdict
    lifecycle: AlignmentLifecycle
    criteria: AlignmentCriteria
    reviewer: str
    reasoning: str = ""


@dataclass(frozen=True, slots=True)
class AlignmentRecord:
    """Full committed/proposed Alignment representation matching schema."""

    alignment_id: str
    claim_id: str
    declaration_id: str
    statement_hash: str
    source_hash: str
    reviewer: str
    verdict: AlignmentVerdict
    lifecycle: AlignmentLifecycle
    criteria: AlignmentCriteria = field(default_factory=AlignmentCriteria)
    imports: tuple[str, ...] = ()
    namespace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize into a dictionary matching statement-alignment.schema.json."""
        status_val: str
        if self.lifecycle == AlignmentLifecycle.REVIEWED:
            status_val = "aligned" if self.verdict == AlignmentVerdict.ALIGNED else "mismatch"
        else:
            status_val = self.lifecycle.value

        payload: dict[str, Any] = {
            "alignment_id": self.alignment_id,
            "claim_id": self.claim_id,
            "declaration_id": self.declaration_id,
            "statement_hash": self.statement_hash,
            "source_hash": self.source_hash,
            "reviewer": self.reviewer,
            "reviewer_verdict": self.verdict.value,
            "status": status_val,
        }
        if self.imports:
            payload["imports"] = list(self.imports)
        if self.namespace:
            payload["namespace"] = self.namespace

        crit = self.criteria.to_dict()
        for key in (
            "quantifier_correspondence",
            "domain_correspondence",
            "universe_assumptions",
            "typeclass_assumptions",
            "classical_assumptions",
            "constructive_assumptions",
            "relation",
        ):
            val = crit.get(key)
            if val:  # only include if non-empty
                payload[key] = val

        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlignmentRecord:
        """Construct an AlignmentRecord from a serialized dictionary."""
        criteria = AlignmentCriteria(
            quantifier_correspondence=data.get("quantifier_correspondence", ""),
            domain_correspondence=data.get("domain_correspondence", ""),
            universe_assumptions=tuple(data.get("universe_assumptions", ())),
            typeclass_assumptions=tuple(data.get("typeclass_assumptions", ())),
            classical_assumptions=tuple(data.get("classical_assumptions", ())),
            constructive_assumptions=tuple(data.get("constructive_assumptions", ())),
            relation=data.get("relation", "exact"),
            notes=data.get("notes", ""),
        )
        return cls(
            alignment_id=data["alignment_id"],
            claim_id=data["claim_id"],
            declaration_id=data["declaration_id"],
            statement_hash=data["statement_hash"],
            source_hash=data.get("source_hash", ""),
            reviewer=data.get("reviewer", ""),
            verdict=AlignmentVerdict(data.get("reviewer_verdict", "aligned")),
            lifecycle=AlignmentLifecycle(
                "reviewed" if data.get("status") in ("aligned", "mismatch") else data.get("status", "draft")
            ),
            criteria=criteria,
            imports=tuple(data.get("imports", ())),
            namespace=data.get("namespace"),
        )
