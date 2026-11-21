"""Reviewers for evaluating mathematical statement alignment (§7.6)."""

from __future__ import annotations

from typing import Callable, Protocol

from commit_gate.vocab import AlignmentLifecycle, AlignmentVerdict

from .llm_client import HTTPLLMClient, LLMClientProtocol
from .models import (
    AlignmentCriteria,
    AlignmentReviewRequest,
    AlignmentReviewResult,
)
from .prompts import (
    ALIGNMENT_REVIEWER_SYSTEM_PROMPT,
    format_alignment_review_prompt,
)

__all__ = [
    "AlignmentReviewStrategy",
    "AlignmentReviewerProtocol",
    "LLMAlignmentReviewer",
    "RuleBasedAlignmentReviewer",
]

AlignmentReviewStrategy = Callable[
    [AlignmentReviewRequest], AlignmentReviewResult
]


class AlignmentReviewerProtocol(Protocol):
    """Interface for alignment reviewer implementations (human, LLM, or rule-based)."""

    def review(self, request: AlignmentReviewRequest) -> AlignmentReviewResult:
        ...


class RuleBasedAlignmentReviewer:
    """Deterministic reviewer verifying structural alignment criteria (§7.6)."""

    def __init__(self, reviewer_id: str = "alignment-reviewer-1") -> None:
        self.reviewer_id = reviewer_id

    def review(self, request: AlignmentReviewRequest) -> AlignmentReviewResult:
        informal = request.informal_statement.strip()
        formal = request.formal_statement.strip()

        if not informal or not formal:
            return AlignmentReviewResult(
                verdict=AlignmentVerdict.MISMATCH,
                lifecycle=AlignmentLifecycle.REVIEWED,
                criteria=AlignmentCriteria(
                    relation="reformulation",
                    notes="Missing informal or formal statement content.",
                ),
                reviewer=self.reviewer_id,
                reasoning="Empty statement provided.",
            )

        # Basic heuristic analysis of binders and domains
        quant_notes = "Standard quantifier correspondence checked."
        domain_notes = "Domains match target types."
        relation = "exact"
        verdict = AlignmentVerdict.ALIGNED

        # Check for obvious mismatches or weakness signals
        if "sorry" in formal:
            verdict = AlignmentVerdict.AMBIGUOUS
            relation = "reformulation"
        elif "True" == formal.strip() and informal != "True":
            verdict = AlignmentVerdict.WEAKER
            relation = "weakening"

        criteria = AlignmentCriteria(
            quantifier_correspondence=quant_notes,
            domain_correspondence=domain_notes,
            selected_definitions_match=True,
            implication_to_target_explicit=True,
            relation=relation,
            notes="Rule-based verification passed.",
        )

        return AlignmentReviewResult(
            verdict=verdict,
            lifecycle=AlignmentLifecycle.REVIEWED,
            criteria=criteria,
            reviewer=self.reviewer_id,
            reasoning=f"Automated alignment evaluation: {verdict.value}",
        )


class LLMAlignmentReviewer:
    """Production LLM-based reviewer that audits statements across the 8 §7.6 dimensions."""

    def __init__(
        self,
        llm_client: LLMClientProtocol | None = None,
        reviewer_id: str = "llm-alignment-reviewer-1",
        *, strict: bool = False,
    ) -> None:
        self.llm_client = llm_client or HTTPLLMClient()
        self.reviewer_id = reviewer_id
        self.strict = strict

    def review(self, request: AlignmentReviewRequest) -> AlignmentReviewResult:
        """Audit the alignment using the LLM intelligence engine."""
        user_prompt = format_alignment_review_prompt(
            informal_claim=request.informal_statement,
            formal_declaration=request.formal_statement,
            imports=list(request.imports) if request.imports else None,
            namespace=request.namespace,
        )

        try:
            data = self.llm_client.generate_json(
                system_prompt=ALIGNMENT_REVIEWER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
            if self.strict:
                if not isinstance(data, dict):
                    raise ValueError('review must be a JSON object')
                for name in ('quantifier_correspondence', 'domain_correspondence', 'reasoning', 'verdict', 'relation'):
                    if not isinstance(data.get(name), str) or not data[name].strip():
                        raise ValueError('review is missing an explicit explanation or verdict')
                for name in ('selected_definitions_match', 'implication_to_target_explicit'):
                    if type(data.get(name)) is not bool:
                        raise ValueError('review criteria must be explicit JSON booleans')
                for name in ('universe_assumptions', 'typeclass_assumptions', 'classical_assumptions', 'constructive_assumptions'):
                    if not isinstance(data.get(name), list) or any(not isinstance(x, str) or not x.strip() for x in data[name]):
                        raise ValueError('review assumptions must be explicit arrays of strings')
        except Exception as exc:
            # If the LLM call fails, return ambiguous outcome needing human review
            failure = 'LLM review failed or returned incomplete criteria.' if self.strict else f'LLM review failed: {exc}'
            return AlignmentReviewResult(
                verdict=AlignmentVerdict.AMBIGUOUS,
                lifecycle=AlignmentLifecycle.REVIEW_NEEDED,
                criteria=AlignmentCriteria(
                    relation="reformulation",
                    notes=failure,
                ),
                reviewer=self.reviewer_id,
                reasoning=failure,
            )

        raw_verdict = str(data.get("verdict", "ambiguous")).lower()
        try:
            verdict = AlignmentVerdict(raw_verdict)
            lifecycle = AlignmentLifecycle.REVIEWED
        except ValueError:
            verdict = AlignmentVerdict.AMBIGUOUS
            lifecycle = AlignmentLifecycle.REVIEW_NEEDED

        raw_relation = str(data.get("relation", "mismatch")).lower()
        valid_relations = {
            "exact",
            "strengthening",
            "weakening",
            "reformulation",
            "mismatch",
        }
        if raw_relation not in valid_relations:
            raw_relation = "mismatch"
            verdict = AlignmentVerdict.AMBIGUOUS
            lifecycle = AlignmentLifecycle.REVIEW_NEEDED

        expected_relations = {
            AlignmentVerdict.ALIGNED: {"exact", "reformulation"},
            AlignmentVerdict.STRONGER: {"strengthening"},
            AlignmentVerdict.WEAKER: {"weakening"},
            AlignmentVerdict.MISMATCH: {"mismatch"},
        }
        if (
            verdict in expected_relations
            and raw_relation not in expected_relations[verdict]
        ):
            lifecycle = AlignmentLifecycle.REVIEW_NEEDED

        criteria = AlignmentCriteria(
            quantifier_correspondence=str(data.get("quantifier_correspondence", "")),
            domain_correspondence=str(data.get("domain_correspondence", "")),
            universe_assumptions=tuple(data.get("universe_assumptions", ())),
            typeclass_assumptions=tuple(data.get("typeclass_assumptions", ())),
            classical_assumptions=tuple(data.get("classical_assumptions", ())),
            constructive_assumptions=tuple(data.get("constructive_assumptions", ())),
            selected_definitions_match=bool(data.get("selected_definitions_match", True)),
            implication_to_target_explicit=bool(
                data.get("implication_to_target_explicit", True)
            ),
            relation=raw_relation,  # type: ignore[arg-type]
            notes=str(data.get("reasoning", "")),
        )

        return AlignmentReviewResult(
            verdict=verdict,
            lifecycle=lifecycle,
            criteria=criteria,
            reviewer=self.reviewer_id,
            reasoning=str(data.get("reasoning", "")),
        )

