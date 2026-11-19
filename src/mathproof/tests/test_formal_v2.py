"""Consolidated v2 checks behind the existing public adapter."""

from copy import deepcopy
from unittest.mock import patch

import pytest

import mathproof
from mathproof.formal_v2 import _json, validate
from mathproof.maths_ai_http import MathsAIAPIError, MathsAIHTTPFormalATP
from test_maths_ai_http import setup_adapter


def test_only_one_maths_ai_public_adapter():
    assert not hasattr(mathproof, "FormalV2HTTPATP")
    assert mathproof.MathsAIHTTPFormalATP is MathsAIHTTPFormalATP


@pytest.mark.parametrize("value", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_wire_json_rejects_ambiguous_and_nonfinite_values(value):
    with pytest.raises(ValueError):
        _json(value)


@pytest.mark.parametrize("value", [True, 2.0, "2", -1])
def test_tactic_allowance_is_strict(value):
    _, request, *_ = setup_adapter()
    request["budget"]["max_steps"] = value
    assert validate("formal-search-request", request)


def test_optional_selection_is_resolved_once_by_provider():
    atp, request, ack, run, remote, call, transport = setup_adapter()
    request.pop("backend")
    request.pop("deployment_id")
    from mathproof.formal_v2 import without_nulls
    from commit_gate.canon import content_hash
    for body in (ack, run, run["result"]):
        body["request_digest"] = content_hash(without_nulls(request))
    original_caps = call("GET", "/formal-capabilities")
    original_caps["default"] = {"deployment_id": "mcts-example", "profile": "puct", "seed": 42}
    def selected(method, path, *args, **kwargs):
        return deepcopy(original_caps) if path == "/formal-capabilities" else call(method, path, *args, **kwargs)
    with patch.object(atp, "_call", side_effect=selected), patch.object(atp, "_transport", side_effect=transport):
        assert atp.formal_search_start(request)["disposition"] == "searching"
        canonical = atp.formal_search_status(request["run_id"])
    assert canonical["disposition"] == "proved-pending-replay"
    assert atp.request_for(request["run_id"]).get("backend") is None
    assert atp.binding(request["run_id"])["snapshot"]["resolved"] == run["resolved"]


def test_remote_schema_drift_stops_submission():
    atp, request, _, _, _, call, _ = setup_adapter()
    caps = call("GET", "/formal-capabilities")
    caps["schema_hashes"] = {key: "sha256:" + "0" * 64 for key in caps["schema_hashes"]}
    with patch.object(atp, "_call", return_value=caps) as request_call, pytest.raises(MathsAIAPIError, match="pinned"):
        atp.formal_search_start(request)
    assert request_call.call_count == 1
