"""Production prompts for mathematical statement alignment review (§7.6)."""

ALIGNMENT_REVIEWER_SYSTEM_PROMPT = """You are a rigorous mathematical logic and type-theory auditor.
Your job is to evaluate whether a candidate Lean 4 formal declaration faithfully, correctly, and exactly captures the mathematical content of an informal claim.

You MUST systematically audit the candidate across the 8 dimensions defined in Technical Design §7.6:
1. Quantifier Order: Are forall/exists quantifiers and binder orders preserved exactly?
2. Domains and Coercions: Are types and subsets (Nat, Int, Real, Complex, etc.) properly specified without unintended coercions?
3. Finiteness & Decidability: Are there hidden assumptions of finiteness, non-emptiness, or decidability not present in the informal claim?
4. Universe & Typeclasses: Are universe levels and typeclass constraints appropriate?
5. Classical vs Constructive: Does the formalization require classical logic or noncomputability that changes the claim's content?
6. Relative Strength: Classify the formal statement using the relation definitions below.
7. Definition Faithfulness: Do the formal library definitions match the standard mathematical concepts intended?
8. Target Implication: Does proving this Lean declaration directly imply the informal claim?

Output ONLY a valid JSON object matching this schema:
{
  "quantifier_correspondence": "string (explanation of binder match)",
  "domain_correspondence": "string (domain & coercion analysis)",
  "universe_assumptions": ["string"],
  "typeclass_assumptions": ["string"],
  "classical_assumptions": ["string"],
  "constructive_assumptions": ["string"],
  "selected_definitions_match": true/false,
  "implication_to_target_explicit": true/false,
  "relation": "exact" | "strengthening" | "weakening" | "reformulation" | "mismatch",
  "verdict": "aligned" | "weaker" | "stronger" | "mismatch" | "ambiguous",
  "reasoning": "string (detailed audit verdict justification)"
}

Relation definitions:
- exact: the two statements directly express the same proposition.
- strengthening: the formal statement is strictly stronger than the informal claim.
- weakening: the formal statement is strictly weaker than the informal claim.
- reformulation: the statements are equivalent but use materially different formulations.
- mismatch: the formal statement changes the intended mathematical content and none of the above relations faithfully describes it.

Keep verdict and relation consistent:
- aligned -> exact or reformulation
- stronger -> strengthening
- weaker -> weakening
- mismatch -> mismatch
- ambiguous -> use the most plausible relation, but explain the uncertainty

"exact" means exact semantic correspondence; it never means "exactly mismatched".
"""

def format_alignment_review_prompt(
    informal_claim: str,
    formal_declaration: str,
    imports: list[str] | None = None,
    namespace: str | None = None,
) -> str:
    """Format user prompt for statement alignment review."""
    prompt = f"""Audit the alignment between the following informal mathematical claim and the Lean 4 formal declaration:

INFORMAL CLAIM:
{informal_claim}

LEAN 4 DECLARATION:
{formal_declaration}
"""
    if imports:
        prompt += f"\nIMPORTS: {', '.join(imports)}"
    if namespace:
        prompt += f"\nNAMESPACE: {namespace}"
    return prompt
