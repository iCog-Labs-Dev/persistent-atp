"""Pinned PR94 formal-v2 lifecycle and evidence client.

Lifecycle calls return provider admissions/snapshots. ``result`` returns the
validated canonical result after settlement. Coordinators persist ``binding``
in their existing journal and restore it after restart; this client owns no
second durable store. Neither a search result nor a replay response promotes a
claim: the commit gate still owns authority, leases, alignment and promotion.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, validators

from commit_gate.canon import canonical_json, content_hash
from .formal_atp import FormalATPAdapter
from .schemas import load_schema
from .soundness import validate_formal_search_result

StrictValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda checker, value: type(value) is int))
TYPES = {
    "start": ("formal-search-request.v2", "FormalSearchRequest"),
    "replay": ("formal-replay-request.v2", "FormalReplayRequest"),
    "admission": ("formal-admission.v2", "Admission"),
    "receipt": ("formal-replay-attestation.v2", "ReplayAttestation"),
    "error": ("formal-error.v2", "ErrorBody"),
    "result": ("formal-result.v2", "FormalResult"),
    "snapshot": ("formal-status.v2", "RunSnapshot"),
}
TERMINATIONS = {
    "certified": "proved-pending-replay",
    **{key: "budget-exhausted" for key in
       ("step-budget", "node-budget", "simulation-budget", "deadline", "lean-resource")},
    "frontier-exhausted": "stagnated", "alternatives-exhausted": "stagnated",
    "invalid-input": "invalid-request", "environment-mismatch": "environment-error",
    "crash": "internal-error", "interrupted": "internal-error", "cancelled": "cancelled",
}
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
TERMINAL = {"finished", "interrupted"}


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
        self.schema_hashes = self.schema["canonical_sha256"]
        # Reconstruct each unmodified provider schema from the deduplicated bundle.
        for stem, title in TYPES.values():
            schema = copy.deepcopy(self.schema["$defs"][title])
            names = self.schema["source_definitions"][stem]
            if names:
                schema["$defs"] = {name: self.schema["$defs"][name] for name in names}
            if content_hash(schema) != self.schema_hashes[stem + ".schema.json"]:
                raise ValueError(f"vendored provider schema digest mismatch: {stem}")
        self.base_url, self.timeout, self.token = base_url.rstrip("/"), timeout, token
        self.requests, self.latest = {}, {}
        self._artifacts = {}
        self._compatible = False

    def validate(self, kind, value):
        title = TYPES[kind][1]
        errors = list(StrictValidator({**self.schema, "$ref": f"#/$defs/{title}"}).iter_errors(value))
        if errors:
            raise FormalV2Error("invalid formal v2 payload: " + "; ".join(error.message for error in errors))
        if kind != "error" and (not isinstance(value, dict) or type(value.get("protocol_version")) is not int
                                or value["protocol_version"] != 2):
            raise FormalV2Error("protocol version must be integer 2")
        return value

    def _defaults(self, value, spec):
        """Match provider model_dump defaults before hashing immutable requests."""
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        if "$ref" in spec:
            spec = self.schema["$defs"][spec["$ref"].rsplit("/", 1)[1]]
        for option in spec.get("anyOf", spec.get("oneOf", [])):
            candidate = self.schema["$defs"][option["$ref"].rsplit("/", 1)[1]] if "$ref" in option else option
            if candidate.get("type") == "object" and (
                "kind" not in value or candidate.get("properties", {}).get("kind", {}).get("const", value["kind"]) == value["kind"]
            ):
                spec = candidate
                break
        result = copy.deepcopy(value)
        for key, field in spec.get("properties", {}).items():
            if key not in result and "default" in field:
                result[key] = copy.deepcopy(field["default"])
            elif key in result:
                result[key] = self._defaults(result[key], field)
        return result

    def _request(self, request):
        value = copy.deepcopy(request)
        self.validate("start", value)
        if any(value[key].split("/", 1)[0] != value["proof_id"] for key in
               ("run_id", "claim_id", "formal_declaration_id")):
            raise FormalV2Error("request identifiers belong to different proofs")
        return self._defaults(value, self.schema["$defs"]["FormalSearchRequest"])

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
        if not isinstance(value, dict) or 2 not in value.get("protocol_versions", []):
            raise FormalV2Error("provider does not support protocol version 2")
        if any(value.get("schema_hashes", {}).get(name) != digest for name, digest in self.schema_hashes.items()):
            raise FormalV2Error("provider does not support the pinned v2 schemas")
        self._compatible = True
        return value

    def _ensure_compatible(self):
        if not self._compatible:
            self.capabilities()

    def put_artifact(self, data):
        if not isinstance(data, bytes):
            raise TypeError("artifact content must be bytes")
        self._ensure_compatible()
        key = "sha256:" + hashlib.sha256(data).hexdigest()
        value = self._call("POST", "/formal-artifacts", data=data)
        if value != {"protocol_version": 2, "artifact_hash": key}:
            raise FormalV2Error("provider changed uploaded content identity")
        return key

    def get_artifact(self, key):
        if not isinstance(key, str) or HASH.fullmatch(key) is None:
            raise FormalV2Error("invalid artifact content address")
        data = self._call("GET", "/formal-artifacts/" + quote(key, safe=""), raw=True)
        if "sha256:" + hashlib.sha256(data).hexdigest() != key:
            raise FormalV2Error("downloaded artifact digest mismatch")
        return data

    def _artifact_json(self, key):
        if key not in self._artifacts:
            try:
                value = _json(self.get_artifact(key))
            except ValueError as error:
                raise FormalV2Error("evidence artifact contains invalid JSON") from error
            if not isinstance(value, dict):
                raise FormalV2Error("evidence artifact must be a JSON object")
            self._artifacts[key] = value
        return self._artifacts[key]

    def binding(self, run_id):
        """Coordinator-owned restart record; journal this alongside the lease."""
        return copy.deepcopy({"request": self.requests[run_id], "snapshot": self.latest[run_id]})

    def restore(self, binding):
        if not isinstance(binding, dict) or set(binding) != {"request", "snapshot"}:
            raise FormalV2Error("restore requires the coordinator-owned request and snapshot")
        request = self._request(binding["request"])
        run_id = request["run_id"]
        if run_id in self.requests and self.requests[run_id] != request:
            raise FormalV2Error("run ID already has another request")
        self.requests[run_id] = request
        try:
            return self._check(binding["snapshot"], run_id)
        except Exception:
            if run_id not in self.latest:
                self.requests.pop(run_id, None)
            raise

    def _known(self, run_id):
        if run_id not in self.requests:
            raise FormalV2Error("restore the coordinator-owned run binding before polling")
        return self.requests[run_id]

    def _check(self, value, run_id):
        self.validate("snapshot" if isinstance(value, dict) and "result" in value else "admission", value)
        request = self._known(run_id)
        if value["run_id"] != run_id or value["request_digest"] != content_hash(request):
            raise FormalV2Error("provider changed immutable run identity")
        resolved, budget = value["resolved"], value["budget"]
        if value["input_kind"] != request["input"]["kind"]:
            raise FormalV2Error("provider changed input kind")
        for field in ("environment_id", "environment_hash", "deployment_id"):
            if request.get(field) is not None and resolved[field] != request[field]:
                raise FormalV2Error(f"provider changed requested {field}")
        selected = request.get("backend") or {}
        for key, target in (("name", "backend"), ("profile", "profile"), ("proposal_mode", "proposal_mode"), ("seed", "seed")):
            if selected.get(key) is not None and selected[key] != resolved.get(target):
                raise FormalV2Error("provider changed selected backend")
        for assertion in (request.get("model_bundle"), request.get("corpus_manifest")):
            if assertion and any(item is not None and resolved["model"].get(key) != item for key, item in assertion.items()):
                raise FormalV2Error("provider changed asserted model or corpus")
        requested = request["budget"]
        if budget["requested_max_steps"] != requested["max_steps"] or budget["requested_wall_clock_ms"] != requested["wall_clock_ms"]:
            raise FormalV2Error("provider changed requested limits")
        cap, used, remaining = (budget[key] for key in ("admitted_max_steps", "consumed_steps", "remaining_steps"))
        wall = budget["admitted_wall_clock_ms"]
        if not 1 <= cap <= requested["max_steps"] or not 0 <= used <= cap or remaining != cap - used or wall < 1:
            raise FormalV2Error("provider raised or replenished a requested limit")
        if requested["wall_clock_ms"] is not None and wall > requested["wall_clock_ms"]:
            raise FormalV2Error("provider raised requested deadline")
        if budget["max_steps_reduced"] != (cap < requested["max_steps"]) or budget["wall_clock_reduced"] != (
            requested["wall_clock_ms"] is not None and wall < requested["wall_clock_ms"]
        ) or value["attempts"] < 0:
            raise FormalV2Error("provider reported inconsistent admission accounting")
        previous = self.latest.get(run_id)
        if previous:
            if previous["resolved"] != resolved or value["attempts"] < previous["attempts"]:
                raise FormalV2Error("provider changed pinned deployment or regressed attempt identity")
            fixed = ("admitted_max_steps", "admitted_wall_clock_ms", "verification_max_steps")
            if any(previous["budget"][key] != budget[key] for key in fixed):
                raise FormalV2Error("provider changed admitted limits")
            if used < previous["budget"]["consumed_steps"] or (
                not previous["budget"]["consumed_steps_exact"] and budget["consumed_steps_exact"]
            ):
                raise FormalV2Error("provider replenished consumed run budget")
            for key in ("input_artifact_hash", "source_artifact_hash", "source_origin"):
                if previous[key] is not None and previous[key] != value[key]:
                    raise FormalV2Error("provider changed admitted input identity")
        if value["input_artifact_hash"] is not None:
            self._check_input(value, request)
        if "result" in value:
            result = value["result"]
            if (value["status"] in TERMINAL) != (result is not None):
                raise FormalV2Error("provider settled before evidence was available")
            if result is not None:
                self._check_result(result, value, request)
        self.latest[run_id] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def _check_input(self, value, request):
        manifest = self._artifact_json(value["input_artifact_hash"])
        expected = {"format": "maths-ai-formal-input-v1", "kind": request["input"]["kind"],
                    "input": request["input"], "environment_id": request["environment_id"],
                    "environment_hash": request["environment_hash"],
                    "source_artifact_hash": value["source_artifact_hash"], "source_origin": value["source_origin"]}
        if any(manifest.get(key) != item for key, item in expected.items()):
            raise FormalV2Error("admitted input differs from the original request")
        origin = "caller-artifact" if value["input_kind"] == "source" else "generated-from-text"
        if value["source_origin"] != origin or (origin == "caller-artifact" and
                                               value["source_artifact_hash"] != request["input"]["artifact_hash"]):
            raise FormalV2Error("provider replaced requested source")
        self.get_artifact(value["source_artifact_hash"])
        return manifest

    def _check_result(self, result, snapshot, request):
        self.validate("result", result)
        expected = {"run_id": request["run_id"], "proof_id": request["proof_id"],
                    "declaration_id": request["formal_declaration_id"], "environment_hash": request["environment_hash"],
                    "request_digest": snapshot["request_digest"], "input_kind": snapshot["input_kind"],
                    **{key: snapshot[key] for key in ("input_artifact_hash", "source_artifact_hash", "source_origin", "attempts", "disposition")},
                    "coordinator_binding": {key: request[key] for key in ("claim_id", "base_revision", "lease_id", "fencing_token")}}
        if any(result.get(key) != item for key, item in expected.items()):
            raise FormalV2Error("result changed run, source or coordinator binding")
        resolved = snapshot["resolved"]
        if result["backend"] != {"name": resolved["backend"], **{key: resolved[key] for key in
                                                              ("profile", "deployment_id", "profile_config_hash")}}:
            raise FormalV2Error("result changed pinned deployment")
        if result["termination"]["reason"] != snapshot["termination"] or TERMINATIONS[snapshot["termination"]] != result["disposition"]:
            raise FormalV2Error("result outcome contradicts termination")
        if result["budget"] != {key: snapshot["budget"][key] for key in
                                ("admitted_max_steps", "remaining_steps", "admitted_wall_clock_ms")} or any(
            result["budget_used"][key] != snapshot["budget"][target] for key, target in
            (("tactic_steps", "consumed_steps"), ("tactic_steps_exact", "consumed_steps_exact"))
        ):
            raise FormalV2Error("result changed cumulative accounting")
        # Reuse the established graph schema and AND/OR soundness validator.
        # The versioned envelope is checked above, never silently discarded.
        graph_schema = load_schema("formal-search-result")
        graph = {key: copy.deepcopy(result[key]) for key in graph_schema["properties"] if key in result and result[key] is not None}
        graph["budget_used"] = {"tactic_steps": result["budget_used"]["tactic_steps"]}
        errors = [error.message for error in StrictValidator(graph_schema).iter_errors(graph)]
        state_ids = [state.get("state_id") for state in graph["states"]]
        edge_ids = [edge.get("tactic_id") for edge in graph["tactic_edges"]]
        if errors:
            raise FormalV2Error("invalid canonical graph: " + "; ".join(errors))
        if len(set(state_ids)) != len(state_ids) or len(set(edge_ids)) != len(edge_ids) or result["root_state_id"] not in state_ids:
            raise FormalV2Error("canonical graph has duplicate or missing identities")
        if any(node_id.split("/", 1)[0] != request["proof_id"] for node_id in (*state_ids, *edge_ids)) or any(
            obstruction["formal_run_id"] != request["run_id"] for obstruction in graph.get("obstructions", [])
        ):
            raise FormalV2Error("canonical graph changed proof or run scope")
        if any(edge["source_state_id"] not in state_ids for edge in graph["tactic_edges"]):
            raise FormalV2Error("canonical graph has unknown tactic source")
        findings = validate_formal_search_result(graph, pinned_environment_hash=request["environment_hash"])
        if findings:
            raise FormalV2Error("unsound canonical graph: " + "; ".join(item.detail for item in findings))
        certificate = result.get("certificate")
        if certificate:
            if certificate["producer_run_id"] != request["run_id"]:
                raise FormalV2Error("certificate changed producer identity")
            evidence = self._artifact_json(certificate["artifact_hash"])
            bindings = {key: result[key] for key in ("run_id", "input_artifact_hash", "source_artifact_hash", "environment_hash")}
            bindings.update(backend=resolved["backend"], attempt=snapshot["attempts"])
            if any(evidence.get(key) != item for key, item in bindings.items()):
                raise FormalV2Error("candidate certificate is not bound to its run")
            manifest = self._artifact_json(result["input_artifact_hash"])
            if resolved["backend"] == "accepted-gnn":
                valid = evidence.get("format") == "maths-ai-accepted-certificate-v1" and evidence.get("statement") == manifest.get("statement")
            else:
                valid = evidence.get("format") == "maths-ai-mcts-certificate-v1" and (
                    evidence.get("artifact", {}).get("statement", {}).get("expression") == manifest.get("statement", {}).get("expression")
                )
            if not valid or not isinstance(evidence.get("proof_trace"), dict):
                raise FormalV2Error("certificate changed admitted statement or proof format")

    def formal_search_start(self, request):
        value = self._request(request)
        key = value["run_id"]
        if key in self.requests and self.requests[key] != value:
            raise FormalV2Error("run ID already has another request")
        self._ensure_compatible()
        self.requests[key] = value
        # Start is idempotent. Retain the request even after a lost HTTP response.
        return self._check(self._call("POST", "/formal-search/start", value), key)

    def formal_search_status(self, run_id):
        self._known(run_id)
        self._ensure_compatible()
        value = self._call("GET", "/formal-search/" + quote(run_id, safe="/"))
        self.validate("snapshot", value)
        return self._check(value, run_id)

    def _operation(self, run_id, operation):
        self._known(run_id)
        self._ensure_compatible()
        # PR94 has no operation idempotency key. Never retry these POSTs
        # automatically after transport failure; reconcile status first.
        value = self._call("POST", "/formal-search/" + quote(run_id, safe="/") + "/" + operation, data=b"")
        self.validate("snapshot", value)
        return self._check(value, run_id)

    def formal_search_cancel(self, run_id):
        return self._operation(run_id, "cancel")

    def formal_search_retry(self, run_id):
        return self._operation(run_id, "retry")

    def formal_search_resume(self, run_id, budget=None):
        raise FormalV2Error("saved-frontier resume is unsupported; retry is explicit and does not increase allowance")

    def result(self, run_id):
        snapshot = self.formal_search_status(run_id)
        if snapshot["result"] is None:
            return None
        return copy.deepcopy(snapshot["result"])

    def submit_replay(self, run_id, *, max_steps, wall_clock_ms=None):
        snapshot = self.formal_search_status(run_id)
        result = snapshot["result"]
        if result is None or result["disposition"] != "proved-pending-replay":
            raise FormalV2Error("run has no candidate proof")
        body = {"protocol_version": 2, "run_id": run_id, "certificate_hash": result["certificate"]["artifact_hash"],
                **{key: result[key] for key in ("input_artifact_hash", "source_artifact_hash", "environment_hash")},
                "budget": {"max_steps": max_steps, "wall_clock_ms": wall_clock_ms}}
        self.validate("replay", body)
        if wall_clock_ms is not None and wall_clock_ms / 1000 > self.timeout:
            raise FormalV2Error("synchronous replay needs a transport timeout at least as long as its wall-clock allowance")
        receipt = self.validate("receipt", self._call("POST", "/formal-replay", body))
        for key in ("run_id", "certificate_hash", "input_artifact_hash", "source_artifact_hash"):
            if receipt[key] != body[key]:
                raise FormalV2Error("replay receipt changed its evidence binding")
        if receipt["backend"] != snapshot["resolved"]["backend"] or receipt["source_origin"] != result["source_origin"]:
            raise FormalV2Error("replay receipt changed backend or source identity")
        if receipt["status"] == "verified" and receipt["environment_hash"] != body["environment_hash"]:
            raise FormalV2Error("replay did not attest the actual expected environment")
        cost = receipt["verification_cost"]
        allowance = cost.get("allowance_max_steps")
        if allowance is not None and (type(allowance) is not int or not 1 <= allowance <= max_steps):
            raise FormalV2Error("provider raised replay allowance")
        issued = cost.get("tactic_steps")
        if issued is not None and (type(issued) is not int or not 0 <= issued <= max_steps):
            raise FormalV2Error("provider reported invalid replay accounting")
        return copy.deepcopy(receipt)

    def formal_replay(self, certificate, environment_hash, *, max_steps, wall_clock_ms=None):
        """Retain the full attestation for an independently authorized replayer."""
        run_id = certificate.get("producer_run_id")
        self._known(run_id)
        result = self.latest[run_id].get("result")
        if not result or certificate.get("artifact_hash") != result.get("certificate", {}).get("artifact_hash") or environment_hash != result["environment_hash"]:
            raise FormalV2Error("certificate handle differs from the stored run")
        return self.submit_replay(run_id, max_steps=max_steps, wall_clock_ms=wall_clock_ms)
