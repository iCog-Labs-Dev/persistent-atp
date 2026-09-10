"""Formal search request data model for the Formal ATP service bridge.

Implements the specification in Technical Design §6.2 (Formal-search request).
The request is immutable: a resumed run receives a new budget extension
record but preserves its original declaration and environment identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["FormalSearchBudget", "FormalSearchRequest"]


@dataclass(frozen=True, slots=True)
class FormalSearchBudget:
    """Execution bounds and checkpointing settings for a formal proof search run."""

    wall_seconds: float | None = 1800.0
    max_nodes: int = 500
    max_depth: int = 20
    checkpoint_every_nodes: int = 50

    def __post_init__(self) -> None:
        if self.wall_seconds is not None and self.wall_seconds <= 0:
            raise ValueError(f"wall_seconds must be > 0 or None, got {self.wall_seconds}")
        if self.max_nodes <= 0:
            raise ValueError(f"max_nodes must be > 0, got {self.max_nodes}")
        if self.max_depth <= 0:
            raise ValueError(f"max_depth must be > 0, got {self.max_depth}")
        if self.checkpoint_every_nodes <= 0:
            raise ValueError(f"checkpoint_every_nodes must be > 0, got {self.checkpoint_every_nodes}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_seconds": self.wall_seconds,
            "max_nodes": self.max_nodes,
            "max_depth": self.max_depth,
            "checkpoint_every_nodes": self.checkpoint_every_nodes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FormalSearchBudget:
        return cls(
            wall_seconds=raw.get("wall_seconds", 1800.0),
            max_nodes=raw.get("max_nodes", 500),
            max_depth=raw.get("max_depth", 20),
            checkpoint_every_nodes=raw.get("checkpoint_every_nodes", 50),
        )


@dataclass(frozen=True, slots=True)
class FormalSearchRequest:
    """Request payload sent to the Formal ATP service to start or resume search."""

    proof_id: str
    claim_id: str
    formal_declaration_id: str
    run_id: str
    base_revision: int
    lease_id: str
    fencing_token: int
    lean_source_artifact: str | None = None
    environment_id: str = "env0"
    environment_hash: str = ""
    corpus_manifest: str = ""
    model_bundle: str = ""
    search_policy: str = "gnn-pln-best-first-v1"
    budget: FormalSearchBudget = field(default_factory=FormalSearchBudget)
    goal_statement: str | None = None
    hypotheses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.proof_id:
            raise ValueError("proof_id cannot be empty")
        if not self.claim_id:
            raise ValueError("claim_id cannot be empty")
        if not self.formal_declaration_id:
            raise ValueError("formal_declaration_id cannot be empty")
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        if self.base_revision < 0:
            raise ValueError(f"base_revision must be >= 0, got {self.base_revision}")
        if not self.lease_id:
            raise ValueError("lease_id cannot be empty")
        if self.fencing_token < 0:
            raise ValueError(f"fencing_token must be >= 0, got {self.fencing_token}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "proof_id": self.proof_id,
            "claim_id": self.claim_id,
            "formal_declaration_id": self.formal_declaration_id,
            "run_id": self.run_id,
            "base_revision": self.base_revision,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "environment_id": self.environment_id,
            "environment_hash": self.environment_hash,
            "corpus_manifest": self.corpus_manifest,
            "model_bundle": self.model_bundle,
            "search_policy": self.search_policy,
            "budget": self.budget.to_dict(),
            "hypotheses": list(self.hypotheses),
        }
        if self.lean_source_artifact is not None:
            payload["lean_source_artifact"] = self.lean_source_artifact
        if self.goal_statement is not None:
            payload["goal_statement"] = self.goal_statement
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FormalSearchRequest:
        raw_budget = raw.get("budget", {})
        budget = (
            raw_budget
            if isinstance(raw_budget, FormalSearchBudget)
            else FormalSearchBudget.from_dict(raw_budget)
        )
        raw_hypotheses = raw.get("hypotheses", ())
        return cls(
            proof_id=raw["proof_id"],
            claim_id=raw["claim_id"],
            formal_declaration_id=raw["formal_declaration_id"],
            run_id=raw["run_id"],
            base_revision=raw["base_revision"],
            lease_id=raw["lease_id"],
            fencing_token=raw["fencing_token"],
            lean_source_artifact=raw.get("lean_source_artifact"),
            environment_id=raw.get("environment_id", "env0"),
            environment_hash=raw.get("environment_hash", ""),
            corpus_manifest=raw.get("corpus_manifest", ""),
            model_bundle=raw.get("model_bundle", ""),
            search_policy=raw.get("search_policy", "gnn-pln-best-first-v1"),
            budget=budget,
            goal_statement=raw.get("goal_statement"),
            hypotheses=tuple(raw_hypotheses),
        )
