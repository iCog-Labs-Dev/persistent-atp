import pytest

from omegaclaw.contracts import ContractError
from omegaclaw.research_runtime import ResearchWorkflow


def candidate(statement="Bridge lemma"):
    return {
        "statement": statement,
        "novelty": "A new intermediate statement.",
        "cost": "One bounded formal search.",
        "risk": "The statement may be false.",
        "falsification_test": "Attempt an independent Lean proof.",
        "expiry_review": "Review after the next formal attempt.",
        "consequences": ["Could reduce the original target."],
    }


def test_research_result_accepts_bounded_typed_candidates():
    result = ResearchWorkflow._validate_response({"candidates": [candidate(), candidate("Second bridge")]})

    assert [item["statement"] for item in result] == ["Bridge lemma", "Second bridge"]


@pytest.mark.parametrize(
    "result",
    [
        {"candidates": [candidate()] * 4},
        {"candidates": [candidate(), candidate()]},
        {"candidates": [{**candidate(), "claim_status": "lean-verified"}]},
        {"candidates": [{**candidate(), "statement": "   "}]},
        {"candidates": []},
    ],
)
def test_research_result_rejects_unbounded_duplicate_or_truth_claims(result):
    with pytest.raises(ContractError):
        ResearchWorkflow._validate_response(result)
