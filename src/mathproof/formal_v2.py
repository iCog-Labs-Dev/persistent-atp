"""Version-2 HTTP lifecycle for the provisional issue-93 integration slice.

This client consumes the same pinned schema bytes as the provider. It does not
convert a provider search graph into the older hypergraph contract or promote a
claim. Coordinators persist returned snapshots/receipts through their existing
store and call restore() after restart; no second journal is created here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import time
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import uuid

from jsonschema import Draft202012Validator, validators
from commit_gate.canon import canonical_json, content_hash
from .formal_atp import FormalATPAdapter

StrictValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda checker, value: type(value) is int))


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON: {value}")))


class FormalV2Error(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class FormalV2HTTPATP(FormalATPAdapter):
    def __init__(self, base_url, *, contract_path=None, contract_hash=None, timeout=10, token=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("formal endpoint requires an HTTP(S) URL without query or fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("transport timeout must be positive and bounded")
        path = Path(contract_path) if contract_path else Path(__file__).resolve().parents[2] / "schemas/formal-v2.schema.json"
        if contract_path is None and not path.is_file():
            path = Path(sys.prefix) / "share/persistent-atp/schemas/formal-v2.schema.json"
        data = path.read_bytes()
        self.contract_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        if contract_hash is not None and contract_hash != self.contract_hash:
            raise ValueError("local formal schema differs from expected contract digest")
        self.schema = _json(data)
        Draft202012Validator.check_schema(self.schema)
        self.base_url, self.timeout, self.token = base_url.rstrip("/"), timeout, token
        self.requests, self.latest, self.replay_requests = {}, {}, {}

    def validate(self, kind, value):
        errors = list(StrictValidator({**self.schema, "$ref": f"#/$defs/{kind}"}).iter_errors(value))
        if errors:
            raise FormalV2Error("invalid formal v2 payload: " + "; ".join(error.message for error in errors))
        if value.get("contract_hash", self.contract_hash) != self.contract_hash:
            raise FormalV2Error("formal schema revision mismatch")
        if "protocol_version" in value and type(value["protocol_version"]) is not int:
            raise FormalV2Error("protocol version must be an integer")
        return value

    def _call(self, method, path, payload=None, *, raw=False, data=None):
        if payload is not None:
            data = canonical_json(payload)
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if payload is None else "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with urlopen(Request(self.base_url + path, data=data, headers=headers, method=method), timeout=self.timeout) as response:
                body = response.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    raise FormalV2Error("formal response exceeds transport bound")
        except HTTPError as error:
            raise FormalV2Error(error.read(8000).decode("utf-8", errors="replace"), status_code=error.code) from error
        except (URLError, TimeoutError, OSError) as error:
            raise FormalV2Error(f"formal transport failure: {error}") from error
        if raw:
            return body
        try:
            return _json(body)
        except ValueError as error:
            raise FormalV2Error("provider returned invalid JSON") from error

    def capabilities(self):
        value = self._call("GET", "/formal-capabilities")
        if value.get("protocol_version") != 2 or value.get("contract_hash") != self.contract_hash:
            raise FormalV2Error("provider does not support the pinned v2 contract")
        return value

    def put_artifact(self, data):
        if not isinstance(data, bytes):
            raise TypeError("artifact content must be bytes")
        key = "sha256:" + hashlib.sha256(data).hexdigest()
        value = self._call("PUT", "/formal-artifacts/" + quote(key, safe=""), data=data)
        if value != {"artifact_hash": key}:
            raise FormalV2Error("provider changed uploaded content identity")
        return key

    def get_artifact(self, key):
        if not isinstance(key, str) or len(key) != 71 or not key.startswith("sha256:"):
            raise FormalV2Error("invalid artifact content address")
        data = self._call("GET", "/formal-artifacts/" + quote(key, safe=""), raw=True)
        if "sha256:" + hashlib.sha256(data).hexdigest() != key:
            raise FormalV2Error("downloaded artifact digest mismatch")
        return data

    def restore(self, snapshot):
        """Restore a snapshot from the coordinator-owned durable run record."""
        self.validate("snapshot", snapshot)
        request = snapshot["request"]
        self.validate("start", request)
        if snapshot["run_id"] != request["run_id"] or snapshot["request_hash"] != content_hash(request):
            raise FormalV2Error("persisted run request binding mismatch")
        self.requests[snapshot["run_id"]] = copy.deepcopy(request)
        self.latest[snapshot["run_id"]] = copy.deepcopy(snapshot)
        return self._check(snapshot, snapshot["run_id"])

    def _check(self, value, run_id):
        self.validate("snapshot", value)
        request = self.requests[run_id]
        if value["run_id"] != run_id or value["request"] != request or value["request_hash"] != content_hash(request):
            raise FormalV2Error("provider changed immutable run identity")
        resolved = value["resolved"]
        for field in ("environment_id", "environment_hash", "deployment_id", "deployment_hash"):
            if field in request and resolved[field] != request[field]:
                raise FormalV2Error(f"provider changed requested {field}")
        selected = request.get("backend", {})
        if selected.get("name", resolved["backend"]) != resolved["backend"] or selected.get("profile", resolved["profile"]) != resolved["profile"]:
            raise FormalV2Error("provider changed selected backend")
        for field, maximum in request["budget"].items():
            if resolved["budget"][field] > maximum:
                raise FormalV2Error("provider raised a requested limit")
        policy_max = request.get("search_policy", {}).get("max_tactic_steps")
        if policy_max is not None and resolved["budget"]["max_steps"] > policy_max:
            raise FormalV2Error("provider ignored the policy step cap")
        previous = self.latest.get(run_id)
        if previous:
            if previous["resolved"] != resolved or value["attempt"] < previous["attempt"]:
                raise FormalV2Error("provider changed pinned deployment or regressed attempt identity")
            for field in ("tactic_steps", "model_calls", "wall_clock_ms"):
                if value["usage"][field] < previous["usage"][field]:
                    raise FormalV2Error("provider replenished consumed run budget")
        if value["certificate_hash"]:
            certificate = self.validate("certificate", _json(self.get_artifact(value["certificate_hash"])))
            for field in ("run_id", "attempt", "request_hash", "input_artifact_hash"):
                if certificate[field] != value[field]:
                    raise FormalV2Error("candidate certificate is not bound to its run")
            if certificate["formal_declaration_id"] != request["formal_declaration_id"] or any(
                certificate[field] != resolved[field] for field in ("deployment_hash", "environment_hash")):
                raise FormalV2Error("candidate certificate changed declaration or deployment")
            admitted = _json(self.get_artifact(value["input_artifact_hash"]))
            if admitted.get("request_hash") != value["request_hash"] or admitted.get("original_input") != request["input"]:
                raise FormalV2Error("candidate input differs from the original request")
            if request["input"]["kind"] == "source" and admitted.get("source_artifact_hash") != request["input"]["artifact_hash"]:
                raise FormalV2Error("candidate replaced the requested source")
            self.get_artifact(admitted["source_artifact_hash"])
        self.latest[run_id] = copy.deepcopy(value)
        return value

    def formal_search_start(self, request):
        value = {"protocol_version": 2, "contract_hash": self.contract_hash, **request}
        self.validate("start", value)
        key = value["run_id"]
        if key in self.requests and self.requests[key] != value:
            raise FormalV2Error("run ID already has another request")
        self.requests[key] = copy.deepcopy(value)
        # Repeated starts are safe because the v2 provider admits by immutable
        # run/request identity, including when a previous HTTP response was lost.
        return self._check(self._call("POST", "/formal-search/start", value), key)

    def formal_search_status(self, run_id):
        if run_id not in self.requests:
            raise FormalV2Error("restore the coordinator-owned run binding before polling")
        return self._check(self._call("GET", "/formal-search/" + quote(run_id, safe="/")), run_id)

    def _operation(self, run_id, operation, operation_id=None, attempt=None):
        snapshot = self.latest[run_id]
        body = {"protocol_version": 2, "contract_hash": self.contract_hash, "operation_id": operation_id or uuid.uuid4().hex,
            "request_hash": snapshot["request_hash"], "attempt": snapshot["attempt"] if attempt is None else attempt}
        self.validate("operation", body)
        return self._check(self._call("POST", "/formal-search/" + quote(run_id, safe="/") + "/" + operation, body), run_id)

    def formal_search_cancel(self, run_id, *, operation_id=None, attempt=None):
        return self._operation(run_id, "cancel", operation_id, attempt)

    def formal_search_retry(self, run_id, *, operation_id=None, attempt=None):
        return self._operation(run_id, "retry", operation_id, attempt)

    def formal_search_resume(self, run_id, budget=None):
        raise FormalV2Error("saved-frontier resume is unsupported; retry is explicit and does not increase allowance")

    def submit_replay(self, run_id, *, replay_id=None, wall_clock_ms=30000):
        snapshot = self.latest[run_id]
        if snapshot["disposition"] != "proved-pending-replay":
            raise FormalV2Error("run has no candidate proof")
        # A lost response or coordinator restart must not implicitly purchase
        # another verification. A fresh replay needs an explicit new replay_id.
        body = {"protocol_version": 2, "contract_hash": self.contract_hash,
            "replay_id": replay_id or "verify-" + snapshot["certificate_hash"][7:],
            "run_id": run_id, "request_hash": snapshot["request_hash"], "certificate_hash": snapshot["certificate_hash"],
            "input_artifact_hash": snapshot["input_artifact_hash"], "environment_hash": snapshot["resolved"]["environment_hash"],
            "budget": {"wall_clock_ms": wall_clock_ms}}
        self.validate("replay", body)
        key = body["replay_id"]
        if key in self.replay_requests and self.replay_requests[key] != body:
            raise FormalV2Error("replay ID already has another request")
        self.replay_requests[key] = body
        return self._check_replay(self._call("POST", "/formal-replay", body), key)

    def _check_replay(self, value, key):
        self.validate("receipt", value)
        request = self.replay_requests[key]
        for field in ("replay_id", "run_id", "request_hash", "certificate_hash", "input_artifact_hash", "environment_hash"):
            if value[field] != request[field]:
                raise FormalV2Error("replay receipt changed its evidence binding")
        if value["verdict"] == "verified":
            if value["actual_environment_hash"] != request["environment_hash"]:
                raise FormalV2Error("replay did not attest the actual expected environment")
            verification = _json(self.get_artifact(value["verification_artifact_hash"]))
            if verification.get("actual_environment_hash") != request["environment_hash"] or verification.get("verification", {}).get("status") != "passed":
                raise FormalV2Error("replay artifact does not witness the receipt")
            if any(verification.get("binding", {}).get(field) != value[field] for field in
                   ("run_id", "request_hash", "certificate_hash", "input_artifact_hash", "environment_hash", "source_artifact_hash")):
                raise FormalV2Error("replay artifact changed the checked proof or source")
            admitted = _json(self.get_artifact(value["input_artifact_hash"]))
            if value["source_artifact_hash"] != admitted.get("source_artifact_hash"):
                raise FormalV2Error("replay source differs from the admitted input")
        return value

    def replay_status(self, replay_id):
        if replay_id not in self.replay_requests:
            raise FormalV2Error("restore the verification request before polling")
        return self._check_replay(self._call("GET", "/formal-replay/" + quote(replay_id, safe="/")), replay_id)

    def formal_replay(self, certificate, environment_hash, *, wall_clock_ms=30000, replay_id=None):
        """Logical independent-replay call; retain the full v2 receipt for admission."""
        run_id = certificate.get("producer_run_id")
        snapshot = self.latest.get(run_id)
        if not snapshot or certificate.get("artifact_hash") != snapshot["certificate_hash"] or environment_hash != snapshot["resolved"]["environment_hash"]:
            raise FormalV2Error("certificate handle differs from the stored run")
        receipt = self.submit_replay(run_id, wall_clock_ms=wall_clock_ms, replay_id=replay_id)
        deadline = time.monotonic() + wall_clock_ms / 1000 + self.timeout
        while receipt["status"] != "stopped":
            if time.monotonic() >= deadline:
                raise FormalV2Error("replay observation timed out; this is not proof rejection")
            time.sleep(.05)
            receipt = self.replay_status(receipt["replay_id"])
        return receipt
