import pytest

from omegaclaw.contracts import ContractError
from omegaclaw.research_runtime import ResearchWorkflow


def candidate(statement="Bridge lemma"):
    return {
        "candidate_kind": "mathematical-claim",
        "formalization_spec": {"theorem": statement, "definitions": {}, "assumptions": [],
            "quantifier_domains": {}, "allowed_background": [], "targets": [statement],
            "verification_policy": {"independent_lean_replay": True}},
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
        {"candidates": [{**candidate(), 'candidate_kind': 'research-move'}]},
        {"candidates": [{k: v for k, v in candidate().items() if k != 'candidate_kind'}]},
        {"candidates": [{**candidate(), 'formalization_spec': None}]},
        {"candidates": [{**candidate(), 'formalization_spec': {
            **candidate()['formalization_spec'], 'theorem': 'Different proposition'}}]},
    ],
)
def test_research_result_rejects_unbounded_duplicate_or_truth_claims(result):
    with pytest.raises(ContractError):
        ResearchWorkflow._validate_response(result)


def test_research_moves_and_legacy_saved_responses_remain_speculative():
    move = {**candidate('Audit the dependencies'), 'candidate_kind': 'research-move', 'formalization_spec': None}
    assert ResearchWorkflow._validate_response({'candidates': [move]}) == [move]
    legacy = {key: value for key, value in candidate().items() if key not in {'candidate_kind', 'formalization_spec'}}
    assert ResearchWorkflow._validate_response({'candidates': [legacy]}, allow_legacy=True) == [legacy]
