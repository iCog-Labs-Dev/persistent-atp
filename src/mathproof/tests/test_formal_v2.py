"""Pinned wire/lifecycle checks. Injected evidence is not native acceptance."""

import copy
import time

import pytest

from commit_gate.canon import content_hash
from mathproof.formal_v2 import FormalV2Error, FormalV2HTTPATP, _json


def request():
    return {"protocol_version": 2, "proof_id": "p1", "claim_id": "p1/c-1",
        "formal_declaration_id": "p1/fd-1", "run_id": "p1/fr-1", "base_revision": 0,
        "lease_id": "lease-1", "fencing_token": 1, "search_policy": {"deny_sorry": True},
        "environment_id": "fixture", "environment_hash": "sha256:" + "e" * 64,
        "input": {"kind": "goal", "goal_text": "P", "hypotheses": ["P : Prop", "h : P"], "universe_levels": []},
        "budget": {"max_steps": 8, "wall_clock_ms": 1000}}


def admission(client):
    body = client._request(request())
    return {"protocol_version": 2, "run_id": body["run_id"], "request_digest": content_hash(body),
        "status": "admitting", "input_kind": "goal", "attempts": 0,
        "resolved": {"backend": "accepted-gnn", "profile": "best-first", "deployment_id": "fixture",
            "profile_config_hash": "sha256:" + "d" * 64, "environment_id": "fixture",
            "environment_hash": body["environment_hash"], "model": {}, "proposal_mode": "greedy", "seed": None},
        "budget": {"requested_max_steps": 8, "admitted_max_steps": 8, "max_steps_reduced": False,
            "requested_wall_clock_ms": 1000, "admitted_wall_clock_ms": 1000, "wall_clock_reduced": False,
            "consumed_steps": 0, "consumed_steps_exact": True, "remaining_steps": 8, "verification_max_steps": 10},
        "input_artifact_hash": None, "source_artifact_hash": None, "source_origin": None,
        "retry_from": "bound-initial-input"}


def test_pinned_contract_accepts_optional_default_and_strict_input_union():
    client = FormalV2HTTPATP("http://localhost:8000")
    body = request()
    client.validate("start", body)
    source = copy.deepcopy(body)
    source["input"] = {"kind": "source", "artifact_hash": "sha256:" + "a" * 64,
                       "declaration": "Demo.goal", "starting_state": "binders-introduced"}
    client.validate("start", source)
    for field in ("contract_hash", "lean_source_artifact"):
        with pytest.raises(FormalV2Error):
            client.validate("start", {**body, field: "sha256:" + "a" * 64})
    source["input"]["goal_text"] = "P"
    with pytest.raises(FormalV2Error):
        client.validate("start", source)
    body["search_policy"]["max_tactic_steps"] = 2
    with pytest.raises(FormalV2Error):
        client.validate("start", body)


@pytest.mark.parametrize("field,value", [("max_steps", True), ("max_steps", 2.0), ("wall_clock_ms", -1), ("max_steps", "2")])
def test_no_numeric_coercion(field, value):
    client = FormalV2HTTPATP("http://localhost:8000")
    body = request()
    body["budget"][field] = value
    with pytest.raises(FormalV2Error):
        client.validate("start", body)


def test_restore_pins_deployment_and_monotonic_costs():
    client = FormalV2HTTPATP("http://localhost:8000")
    value = admission(client)
    client.restore({"request": request(), "snapshot": value})
    updated = copy.deepcopy(value)
    updated.update(status="running", attempts=1)
    updated["budget"].update(consumed_steps=1, remaining_steps=7)
    client._check(updated, value["run_id"])
    with pytest.raises(FormalV2Error, match="regressed|replenished"):
        client._check(value, value["run_id"])
    changed = copy.deepcopy(updated)
    changed["resolved"]["profile_config_hash"] = "sha256:" + "f" * 64
    with pytest.raises(FormalV2Error, match="deployment"):
        client._check(changed, value["run_id"])
    assert client.latest[value["run_id"]] == updated
    restored = FormalV2HTTPATP("http://localhost:8000")
    assert restored.restore(client.binding(value["run_id"])) == updated


@pytest.mark.parametrize("mutate", [
    lambda ack: ack["budget"].update(remaining_steps=9),
    lambda ack: ack["budget"].update(admitted_max_steps=9, remaining_steps=9),
    lambda ack: ack["budget"].update(admitted_wall_clock_ms=1001),
    lambda ack: ack["budget"].update(consumed_steps=-1, remaining_steps=9),
    lambda ack: ack["budget"].update(max_steps_reduced=True),
    lambda ack: ack.update(request_digest="sha256:" + "f" * 64),
])
def test_inconsistent_admission_never_becomes_persisted_binding(mutate):
    client = FormalV2HTTPATP("http://localhost:8000")
    value = admission(client)
    mutate(value)
    with pytest.raises(FormalV2Error):
        client.restore({"request": request(), "snapshot": value})
    assert not client.requests and not client.latest


def test_schema_drift_rejected_before_work(monkeypatch):
    client = FormalV2HTTPATP("http://localhost:8000")
    calls = []
    def call(method, path, *args, **kwargs):
        calls.append(path)
        return {"protocol_versions": [2], "schema_hashes": {}}
    monkeypatch.setattr(client, "_call", call)
    with pytest.raises(FormalV2Error, match="pinned"):
        client.formal_search_start(request())
    assert calls == ["/formal-capabilities"]


def test_lost_mutating_response_is_not_automatically_reissued(monkeypatch):
    client = FormalV2HTTPATP("http://localhost:8000")
    client.restore({"request": request(), "snapshot": admission(client)})
    client._compatible = True
    calls = []
    def call(method, path, *args, **kwargs):
        calls.append((method, path))
        raise FormalV2Error("lost response")
    monkeypatch.setattr(client, "_call", call)
    with pytest.raises(FormalV2Error, match="lost"):
        client.formal_search_retry("p1/fr-1")
    assert calls == [("POST", "/formal-search/p1/fr-1/retry")]


def test_poll_and_resume_never_create_implicit_work():
    client = FormalV2HTTPATP("http://localhost:8000")
    with pytest.raises(FormalV2Error, match="restore"):
        client.formal_search_status("p1/fr-1")
    with pytest.raises(FormalV2Error, match="unsupported"):
        client.formal_search_resume("p1/fr-1")


def test_artifact_download_content_address_checked(monkeypatch):
    client = FormalV2HTTPATP("http://localhost:8000")
    monkeypatch.setattr(client, "_call", lambda *args, **kwargs: b"tampered bytes")
    with pytest.raises(FormalV2Error, match="digest mismatch"):
        client.get_artifact("sha256:" + "a" * 64)


@pytest.mark.parametrize("value", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_wire_json_has_unique_keys_and_finite_numbers(value):
    with pytest.raises(ValueError):
        _json(value)


def test_latest_provider_router_client_result_and_replay(tmp_path, monkeypatch):
    """Real provider router/scheduler/process; scripted runtime, no Lean claim."""
    provider = pytest.importorskip("maths_ai.formal.tests.test_formal_api")
    from maths_ai.formal.contract import FormalSearchRequest, json_schemas

    with provider._app(tmp_path) as http:
        env_hash = provider._ready(http)
        client = FormalV2HTTPATP("http://fixture", timeout=30)
        def call(method, path, payload=None, *, raw=False, data=None):
            response = http.request(method, path, json=payload, content=data)
            if response.status_code >= 400:
                raise FormalV2Error(response.text, status_code=response.status_code)
            return response.content if raw else response.json()
        monkeypatch.setattr(client, "_call", call)
        assert client.schema_hashes == {name + ".schema.json": content_hash(schema) for name, schema in json_schemas().items()}
        body = provider._request(env_hash)
        body["backend"].pop("proposal_mode")
        body["backend"].pop("seed")
        assert client._request(body) == FormalSearchRequest.model_validate(body).model_dump(mode="json")
        ack = client.formal_search_start(body)
        assert ack["budget"]["max_steps_reduced"] is True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = client.formal_search_status(body["run_id"])
            if snapshot["status"] == "finished":
                break
            time.sleep(.02)
        assert snapshot["disposition"] == "proved-pending-replay"
        result = client.result(body["run_id"])
        assert result["certificate"]["status"] == "replay-pending"
        receipt = client.formal_replay(result["certificate"], env_hash, max_steps=20, wall_clock_ms=1000)
        assert receipt["status"] == "verified"
        assert receipt["certificate_hash"] == result["certificate"]["artifact_hash"]
        assert client.latest[body["run_id"]]["result"]["certificate"]["status"] == "replay-pending"
        changed = copy.deepcopy(snapshot)
        changed["result"]["coordinator_binding"]["fencing_token"] += 1
        with pytest.raises(FormalV2Error, match="coordinator"):
            client._check(changed, body["run_id"])
        changed = copy.deepcopy(snapshot)
        changed["result"]["tactic_edges"][0]["subgoal_count"] += 1
        with pytest.raises(FormalV2Error, match="unsound"):
            client._check(changed, body["run_id"])
