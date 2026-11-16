"""Strict shared-schema and state-binding checks; no prover dependency."""

import copy
import json

import pytest

from commit_gate.canon import canonical_json, content_hash
from mathproof.formal_v2 import FormalV2Error, FormalV2HTTPATP, _json


def request(client):
    return {"protocol_version": 2, "contract_hash": client.contract_hash, "proof_id": "p1", "claim_id": "p1/c-1",
        "formal_declaration_id": "p1/fd-1", "run_id": "p1/fr-1", "base_revision": 0, "lease_id": "lease-1", "fencing_token": 1,
        "environment_id": "fixture", "environment_hash": "sha256:" + "e" * 64,
        "input": {"kind": "goal", "goal_text": "P", "hypotheses": ["P : Prop", "h : P"], "universe_levels": []},
        "budget": {"max_steps": 8, "wall_clock_ms": 1000}}


def snapshot(client):
    body = request(client)
    return {"protocol_version": 2, "contract_hash": client.contract_hash, "run_id": body["run_id"], "request_hash": content_hash(body),
        "request": body, "attempt": 1, "status": "queued", "disposition": "searching", "termination_reason": None,
        "resolved": {"backend": "accepted-gnn", "profile": "frozen-best-first", "deployment_id": "fixture", "deployment_hash": "sha256:" + "d" * 64,
            "environment_id": "fixture", "environment_hash": body["environment_hash"], "budget": body["budget"]},
        "usage": {"tactic_steps": 0, "model_calls": 0, "wall_clock_ms": 0, "has_unknown_costs": False},
        "input_artifact_hash": None, "certificate_hash": None, "report_artifact_hash": None, "error": None}


def test_shared_contract_accepts_optional_default_and_strict_input_union():
    client = FormalV2HTTPATP("http://localhost:8000")
    body = request(client)
    client.validate("start", body)
    source = copy.deepcopy(body)
    source["input"] = {"kind": "source", "artifact_hash": "sha256:" + "a" * 64, "declaration": "Demo.goal", "starting_state": "declaration-body"}
    client.validate("start", source)
    with pytest.raises(FormalV2Error):
        client.validate("start", {**body, "lean_source_artifact": "sha256:" + "a" * 64})
    source["input"]["goal_text"] = "P"
    with pytest.raises(FormalV2Error):
        client.validate("start", source)


@pytest.mark.parametrize("field,value", [("max_steps", True), ("max_steps", 2.0), ("wall_clock_ms", -1), ("max_steps", "2")])
def test_no_numeric_coercion(field, value):
    client = FormalV2HTTPATP("http://localhost:8000")
    body = request(client)
    body["budget"][field] = value
    with pytest.raises(FormalV2Error):
        client.validate("start", body)


def test_restore_then_validate_pinned_deployment_and_monotonic_costs():
    client = FormalV2HTTPATP("http://localhost:8000")
    value = snapshot(client)
    client.restore(value)
    updated = copy.deepcopy(value)
    updated.update(status="running")
    updated["usage"]["tactic_steps"] = 1
    client._check(updated, value["run_id"])
    with pytest.raises(FormalV2Error, match="replenished"):
        client._check(value, value["run_id"])
    changed = copy.deepcopy(updated)
    changed["resolved"]["deployment_hash"] = "sha256:" + "f" * 64
    with pytest.raises(FormalV2Error, match="deployment"):
        client._check(changed, value["run_id"])
    assert client.latest[value["run_id"]] == updated


def test_poll_and_resume_never_create_implicit_work():
    client = FormalV2HTTPATP("http://localhost:8000")
    with pytest.raises(FormalV2Error, match="restore"):
        client.formal_search_status("p1/fr-1")
    with pytest.raises(FormalV2Error, match="unsupported"):
        client.formal_search_resume("p1/fr-1")


def test_artifact_download_content_address_is_checked(monkeypatch):
    client = FormalV2HTTPATP("http://localhost:8000")
    monkeypatch.setattr(client, "_call", lambda *args, **kwargs: b"tampered bytes")
    with pytest.raises(FormalV2Error, match="digest mismatch"):
        client.get_artifact("sha256:" + "a" * 64)


@pytest.mark.parametrize("value", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_wire_json_has_unique_keys_and_finite_numbers(value):
    with pytest.raises(ValueError):
        _json(value)
