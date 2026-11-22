"""Mathematics-ai-atp protocol v2 with pinned contract validation.

The caller owns the durable artifact repository. Provider evidence is fetched,
re-hashed and stored before any candidate certificate leaves this adapter.
Retry repeats the bound initial input with the remaining allowance; it does
not resume an MCTS frontier. No provider Python dependency is required.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import time
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

from commit_gate.canon import canonical_json, content_hash
from mathproof.formal_atp import FormalATPAdapter
from mathproof.formal_v2 import ArtifactRepository, CONTRACT_DIR, _json, address, validate, without_nulls
from mathproof.schemas import validate as validate_canonical
from mathproof.soundness import validate_formal_search_result
from mathproof._bounded_http import request as bounded_request

__all__ = ["MathsAIAPIError", "MathsAIHTTPFormalATP", "maths_ai_http_atp"]


class MathsAIAPIError(RuntimeError):
    """Transport, contract, binding or artifact failure; never proof rejection."""

    def __init__(self, message: str, *, status_code: int | None = None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _check(schema, body):
    errors = validate(schema, body)
    if errors:
        raise MathsAIAPIError(f"invalid {schema}.v2: " + "; ".join(errors))
    if schema != "formal-capabilities" and body.get("protocol_version") != 2:
        raise MathsAIAPIError("provider did not return protocol_version 2")
    return body


class MathsAIHTTPFormalATP(FormalATPAdapter):
    """A v2 service client with verified local copies of provider artifacts."""

    _ACTIVE = {"admitting", "queued", "running", "cancelling"}

    def __init__(self, base_url: str, *, artifacts: ArtifactRepository,
                 token: str | None = None, timeout: float = 30.0,
                 replay_budget: Mapping[str, int] | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP(S) URL without query or fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token, self.timeout, self.artifacts = token, timeout, artifacts
        self.observation_deadline = None
        self.replay_budget = dict(replay_budget or {"max_steps": 100, "wall_clock_ms": 30000})
        self._requests: dict[str, dict] = {}
        self._snapshots: dict[str, dict] = {}
        self._latest: dict[str, dict] = {}
        self._replays: dict[tuple[str, str], dict] = {}

    def _transport(self, method, path, data=None, *, binary=False,
                   expected_statuses=frozenset({200})):
        deadline = time.monotonic() + self.timeout
        if self.observation_deadline is not None:
            left = self.observation_deadline - time.time()
            if left <= 0:
                raise MathsAIAPIError('formal observation deadline elapsed')
            deadline = min(deadline, time.monotonic() + left)
        headers = {"Accept": "application/octet-stream" if binary else "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if isinstance(data, bytes) else "application/json"
            if not isinstance(data, bytes):
                data = canonical_json(data)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            status, raw, digest = bounded_request(method, self.base_url + path, headers, data, deadline=deadline)
        except TimeoutError as exc:
            raise MathsAIAPIError('formal transport deadline elapsed', code='FORMAL_TRANSPORT_TIMEOUT') from exc
        except (OSError, ValueError) as exc:
            raise MathsAIAPIError(f'{method} {path}: {exc}') from exc
        if status not in expected_statuses:
            detail, code = f'HTTP {status}', None
            try:
                error = _check("formal-error", _json(raw))
                detail, code = error["detail"], error["category"]
            except (ValueError, MathsAIAPIError):
                pass
            raise MathsAIAPIError(f'{method} {path}: {detail}', status_code=status, code=code)
        if binary:
            return raw, digest
        try:
            body = _json(raw)
        except ValueError as exc:
            raise MathsAIAPIError('provider returned invalid JSON') from exc
        if not isinstance(body, dict):
            raise MathsAIAPIError("provider response must be a JSON object")
        return body

    def _call(self, method, path, payload=None, *, expected_statuses=frozenset({200})):
        return self._transport(method, path, payload, expected_statuses=expected_statuses)

    def formal_capabilities(self):
        body = _check("formal-capabilities", self._call("GET", "/formal-capabilities"))
        manifest = _json((CONTRACT_DIR / "manifest.json").read_bytes())
        expected = {path.rsplit("/", 1)[-1]: digest for path, digest in manifest["schema_canonical_sha256"].items()}
        if body.get("schema_hashes") != expected:
            raise MathsAIAPIError("provider does not support the pinned v2 schemas")
        return body

    def _local_bytes(self, digest):
        try:
            raw = self.artifacts.get(digest)
        except Exception as exc:
            raise MathsAIAPIError(f"cannot read local artifact {digest}: {exc}") from exc
        if address(raw) != digest:
            raise MathsAIAPIError(f"local artifact hash mismatch: {digest}")
        return raw

    def _put(self, raw):
        try:
            digest = self.artifacts.put(raw)
            if digest != address(raw) or self._local_bytes(digest) != raw:
                raise MathsAIAPIError("artifact repository did not persist the expected bytes")
            return digest
        except MathsAIAPIError:
            raise
        except Exception as exc:
            raise MathsAIAPIError(f"cannot persist provider artifact: {exc}") from exc

    def _fetch_artifact(self, digest):
        raw, header = self._transport("GET", "/formal-artifacts/" + quote(digest, safe=":"), binary=True)
        if header != digest or address(raw) != digest:
            raise MathsAIAPIError(f"provider artifact hash mismatch: {digest}")
        self._put(raw)
        return raw

    def upload_artifact(self, digest):
        """Upload existing local source bytes and verify the provider's address."""
        raw = self._local_bytes(digest)
        if len(raw) > 8388608:
            raise ValueError("source artifact exceeds the v2 8 MiB limit")
        reply = _check("formal-artifact-upload", self._call(
            "POST", "/formal-artifacts", raw, expected_statuses=frozenset({201})))
        if reply["artifact_hash"] != digest:
            raise MathsAIAPIError("uploaded source artifact hash mismatch")
        return reply

    def put_artifact(self, raw):
        digest = self._put(raw)
        self.upload_artifact(digest)
        return digest

    def get_artifact(self, digest):
        return self._fetch_artifact(digest)

    def prepare(self, request):
        """Validate an original request without contacting the provider."""
        request = deepcopy(dict(request))
        _check("formal-search-request", request)
        if any(not request[k].startswith(request["proof_id"] + "/")
               for k in ("claim_id", "formal_declaration_id", "run_id")):
            raise MathsAIAPIError("request identities belong to different proofs")
        return request

    def restore_prepared(self, request):
        """Register durable request identity before reconciling uncertain delivery."""
        request = self.prepare(request)
        previous = self._requests.get(request["run_id"])
        if previous is not None and without_nulls(previous) != without_nulls(request):
            raise MathsAIAPIError("run_id already belongs to a different request")
        self._requests[request["run_id"]] = request

    def _prepare(self, request):
        request = self.prepare(request)
        cap = self.formal_capabilities()
        if 2 not in cap["protocol_versions"] or cap.get("frontier_resume") is not False:
            raise MathsAIAPIError("unsupported provider lifecycle contract")
        default = cap.get("default") or {}
        deployment_id = request.get("deployment_id") or default.get("deployment_id")
        deployment = cap["deployments"].get(deployment_id)
        if not deployment or not deployment.get("available"):
            raise MathsAIAPIError("selected deployment is unavailable")
        backend = request.get("backend")
        if backend is None:
            profile = default.get("profile") if deployment_id == default.get("deployment_id") else None
            if profile is None and len(deployment["profiles"]) == 1:
                profile = next(iter(deployment["profiles"]))
            backend = {"name": deployment["backend"], "profile": profile}
        for key, expected in (("backend", backend["name"]),
                              ("environment_id", request["environment_id"]),
                              ("environment_hash", request["environment_hash"])):
            if deployment.get(key) != expected:
                raise MathsAIAPIError(f"deployment {key} mismatch")
        if request["input"]["kind"] not in deployment["inputs"]:
            raise MathsAIAPIError("deployment does not support the selected input kind")
        if backend["profile"] not in deployment["profiles"]:
            raise MathsAIAPIError("deployment does not support the selected profile")
        if backend["name"] == "accepted-gnn" and request["input"].get("universe_levels"):
            raise MathsAIAPIError("accepted-gnn does not support universe-polymorphic goals")
        return request, deployment

    def _check_snapshot(self, snapshot, request, *, admission=False, deployment=None):
        _check("formal-admission" if admission else "formal-run", snapshot)
        if snapshot.get("protocol_version") != 2:
            raise MathsAIAPIError("provider did not return protocol_version 2")
        for key, expected in (("run_id", request["run_id"]),
                              ("request_digest", content_hash(without_nulls(request))),
                              ("input_kind", request["input"]["kind"])):
            if snapshot.get(key) != expected:
                raise MathsAIAPIError(f"run {key} mismatch")
        resolved = snapshot["resolved"]
        for assertion in (request.get("model_bundle"), request.get("corpus_manifest")):
            if assertion and any(value is not None and resolved["model"].get(key) != value for key, value in assertion.items()):
                raise MathsAIAPIError("provider changed asserted model or corpus")
        requested_backend = request.get("backend") or {}
        for key, expected in (("deployment_id", request.get("deployment_id")),
                              ("backend", requested_backend.get("name")),
                              ("profile", requested_backend.get("profile")),
                              ("proposal_mode", requested_backend.get("proposal_mode")),
                              ("seed", requested_backend.get("seed")),
                              ("environment_id", request["environment_id"]),
                              ("environment_hash", request["environment_hash"])):
            if expected is not None and resolved.get(key) != expected:
                raise MathsAIAPIError(f"resolved {key} mismatch")
        if deployment and resolved["profile_config_hash"] != deployment["profile_config_hashes"].get(resolved["profile"]):
            raise MathsAIAPIError("resolved profile configuration mismatch")
        previous = self._snapshots.get(request["run_id"])
        budget = snapshot["budget"]
        if budget["requested_max_steps"] != request["budget"]["max_steps"]:
            raise MathsAIAPIError("requested tactic allowance mismatch")
        admitted, used, remaining = (budget[k] for k in ("admitted_max_steps", "consumed_steps", "remaining_steps"))
        if not (0 <= used <= admitted <= budget["requested_max_steps"] and remaining == admitted - used):
            raise MathsAIAPIError("invalid provider tactic accounting")
        if budget["admitted_wall_clock_ms"] <= 0:
            raise MathsAIAPIError("invalid provider wall clock allowance")
        requested_wall = request["budget"].get("wall_clock_ms")
        if budget["requested_wall_clock_ms"] != requested_wall or (
            requested_wall is not None and budget["admitted_wall_clock_ms"] > requested_wall
        ):
            raise MathsAIAPIError("provider changed the requested wall-clock allowance")
        if deployment:
            profile = deployment["profiles"][resolved["profile"]]
            expected_steps = min(request["budget"]["max_steps"], profile["max_steps"])
            expected_wall = min(request["budget"].get("wall_clock_ms") or profile["default_wall_clock_ms"], profile["wall_clock_ms"])
            if admitted != expected_steps or budget["admitted_wall_clock_ms"] != expected_wall:
                raise MathsAIAPIError("provider did not acknowledge the deployment budget ceilings")
        if previous:
            if resolved != previous["resolved"]:
                raise MathsAIAPIError("run deployment changed after admission")
            for key in ("admitted_max_steps", "admitted_wall_clock_ms", "verification_max_steps"):
                if budget[key] != previous["budget"][key]:
                    raise MathsAIAPIError("run allowance changed after admission")
            if used < previous["budget"]["consumed_steps"] or snapshot["attempts"] < previous["attempts"]:
                raise MathsAIAPIError("run accounting moved backwards")
            if not previous["budget"]["consumed_steps_exact"] and budget["consumed_steps_exact"]:
                raise MathsAIAPIError("unknown run costs cannot become exact")
            for key in ("input_artifact_hash", "source_artifact_hash", "source_origin"):
                if previous.get(key) and previous[key] != snapshot.get(key):
                    raise MathsAIAPIError(f"run {key} changed after admission")
        if snapshot.get("source_artifact_hash") and request["input"]["kind"] == "source":
            if snapshot["source_artifact_hash"] != request["input"]["artifact_hash"]:
                raise MathsAIAPIError("source input was replaced")
        if snapshot.get("source_origin") and snapshot["source_origin"] != {
                "source": "caller-artifact", "goal": "generated-from-text"}[request["input"]["kind"]]:
            raise MathsAIAPIError("source origin does not match the input kind")

    @staticmethod
    def _json_artifact(raw):
        try:
            body = _json(raw)
            if not isinstance(body, dict) or canonical_json(body) != raw:
                raise ValueError("expected a canonical JSON object")
            return body
        except (ValueError, UnicodeDecodeError) as exc:
            raise MathsAIAPIError(f"invalid canonical JSON artifact: {exc}") from exc

    def _evidence(self, result, request, *, local_only=False):
        fetch = self._local_bytes if local_only else self._fetch_artifact
        fetched = {a["sha256"]: fetch(a["sha256"]) for a in result["artifacts"]}
        def load(digest):
            if not digest:
                raise MathsAIAPIError("proof is missing its evidence address")
            if digest not in fetched:
                fetched[digest] = fetch(digest)
            return fetched[digest]
        if result.get("input_artifact_hash"):
            manifest = self._json_artifact(load(result["input_artifact_hash"]))
            for key, expected in (("format", "maths-ai-formal-input-v1"),
                                  ("input", request["input"]), ("kind", request["input"]["kind"]),
                                  ("environment_id", request["environment_id"]),
                                  ("environment_hash", request["environment_hash"]),
                                  ("source_artifact_hash", result["source_artifact_hash"]),
                                  ("source_origin", result["source_origin"])):
                if manifest.get(key) != expected:
                    raise MathsAIAPIError(f"admitted-input artifact {key} mismatch")
            load(result["source_artifact_hash"])
        if result.get("certificate"):
            cert = self._json_artifact(load(result["certificate"]["artifact_hash"]))
            expected_format = "maths-ai-mcts-certificate-v1" if result["backend"]["name"] == "mcts-actor-critic" else "maths-ai-accepted-certificate-v1"
            for key, expected in (("format", expected_format), ("run_id", request["run_id"]),
                                  ("attempt", result["attempts"]), ("backend", result["backend"]["name"]),
                                  ("input_artifact_hash", result["input_artifact_hash"]),
                                  ("source_artifact_hash", result["source_artifact_hash"]),
                                  ("environment_hash", request["environment_hash"])):
                if cert.get(key) != expected:
                    raise MathsAIAPIError(f"certificate artifact {key} mismatch")

    @staticmethod
    def _scope_graph(result, request, attempt):
        # Provider ids include their run and attempt. Validate that binding
        # before mapping them into persistent-ATP's flat fs/ta id grammar.
        scope = f"{request['run_id']}/attempt-{attempt}"
        def ids(records, field, prefix):
            original = [r[field] for r in records]
            if len(set(original)) != len(original):
                raise MathsAIAPIError(f"duplicate provider {field}")
            if any(not i.startswith(request["proof_id"] + "/") for i in original):
                raise MathsAIAPIError("provider graph crosses proof scopes")
            pattern = re.escape(scope) + rf"/{prefix}-[1-9][0-9]*"
            if any(re.fullmatch(pattern, i) is None for i in original):
                raise MathsAIAPIError("provider graph ids do not match the run and attempt")
            return {i: f"{request['proof_id']}/{prefix}-{int(content_hash([request['run_id'], attempt, i])[7:], 16) or 1}"
                    for i in original}
        states = ids(result["states"], "state_id", "fs")
        tactics = ids(result["tactic_edges"], "tactic_id", "ta")
        try:
            result["root_state_id"] = states[result["root_state_id"]]
            for state in result["states"]:
                state["state_id"] = states[state["state_id"]]
            for edge in result["tactic_edges"]:
                edge["tactic_id"] = tactics[edge["tactic_id"]]
                edge["source_state_id"] = states[edge["source_state_id"]]
                edge["produced_goal_ids"] = [states[i] for i in edge["produced_goal_ids"]]
            for obstruction in result["obstructions"]:
                if "formal_state_ids" in obstruction:
                    obstruction["formal_state_ids"] = [states[i] for i in obstruction["formal_state_ids"]]
        except KeyError as exc:
            raise MathsAIAPIError(f"provider graph references an absent state: {exc}") from exc

    def _translate(self, snapshot, request, *, admission=False, deployment=None, local_only=False):
        self._check_snapshot(snapshot, request, admission=admission, deployment=deployment)
        provider_result = snapshot.get("result")
        result = {"run_id": request["run_id"], "proof_id": request["proof_id"],
                  "declaration_id": request["formal_declaration_id"],
                  "environment_hash": request["environment_hash"], "disposition": "searching",
                  "states": [], "tactic_edges": [], "artifacts": []}
        if not admission and snapshot["status"] not in self._ACTIVE:
            if provider_result is None:
                raise MathsAIAPIError("terminal run is missing its result")
            _check("formal-result", provider_result)
            bindings = {"run_id": request["run_id"], "proof_id": request["proof_id"],
                        "declaration_id": request["formal_declaration_id"],
                        "environment_hash": request["environment_hash"],
                        "request_digest": snapshot["request_digest"], "attempts": snapshot["attempts"],
                        "input_kind": snapshot["input_kind"],
                        "input_artifact_hash": snapshot["input_artifact_hash"],
                        "source_artifact_hash": snapshot["source_artifact_hash"],
                        "source_origin": snapshot["source_origin"], "disposition": snapshot["disposition"],
                        "coordinator_binding": {k: request[k] for k in ("claim_id", "base_revision", "lease_id", "fencing_token")}}
            if any(provider_result.get(k) != v for k, v in bindings.items()):
                raise MathsAIAPIError("result does not match its run and coordinator binding")
            backend = provider_result["backend"]
            if backend != {"name": snapshot["resolved"]["backend"], **{k: snapshot["resolved"][k] for k in ("deployment_id", "profile", "profile_config_hash")}}:
                raise MathsAIAPIError("result backend mismatch")
            if provider_result["termination"]["reason"] != snapshot["termination"]:
                raise MathsAIAPIError("result termination mismatch")
            for k in ("disposition", "root_state_id", "states", "tactic_edges", "obstructions", "artifacts"):
                result[k] = deepcopy(provider_result[k])
            if provider_result.get("certificate"):
                result["certificate"] = deepcopy(provider_result["certificate"])
                if (result["certificate"]["status"] != "replay-pending"
                        or result["certificate"]["producer_run_id"] != request["run_id"]):
                    raise MathsAIAPIError("search cannot supply an accepted replay verdict")
            cost = provider_result["budget_used"]
            if cost["tactic_steps"] != snapshot["budget"]["consumed_steps"]:
                raise MathsAIAPIError("result cost mismatch")
            if cost["tactic_steps_exact"] != snapshot["budget"]["consumed_steps_exact"]:
                raise MathsAIAPIError("result cost exactness mismatch")
            if provider_result["budget"] != {k: snapshot["budget"][k] for k in (
                    "admitted_max_steps", "admitted_wall_clock_ms", "remaining_steps")}:
                raise MathsAIAPIError("result allowance mismatch")
            result["budget_used"] = {"tactic_steps": cost["tactic_steps"]}
            if cost.get("attempt", {}).get("wall_clock_ms") is not None:
                result["budget_used"]["wall_clock_ms"] = cost["attempt"]["wall_clock_ms"]
            self._scope_graph(result, request, snapshot["attempts"])
            errors = validate_canonical("formal-search-result", result)
            findings = validate_formal_search_result(result, pinned_environment_hash=request["environment_hash"])
            if errors or findings:
                raise MathsAIAPIError(f"invalid or unsound result: {errors or findings}")
            self._evidence(provider_result, request, local_only=local_only)
            cert = result.get("certificate")
            if cert:
                replay = self._replays.get((request["run_id"], cert["artifact_hash"]))
                if replay:
                    cert["status"] = {"verified": "replay-accepted", "rejected": "replay-rejected", "inconclusive": "replay-pending"}[replay["status"]]
                    cert["replay_result"] = deepcopy(replay)
        # Keep admission/accounting available without claiming a saved search frontier.
        result["provider"] = {k: deepcopy(v) for k, v in snapshot.items() if k not in {"result", "replays"}}
        result["provider"]["frontier_resume"] = False
        # Replay history grows after search finishes. Attestations have their
        # own artifacts; they must not change the immutable candidate binding.
        binding_snapshot = deepcopy(snapshot)
        if "replays" in binding_snapshot:
            binding_snapshot["replays"] = []
        binding = self._put(canonical_json({"format": "persistent-atp-formal-v2-binding-v1",
                                           "request": request, "snapshot": binding_snapshot}))
        if result.get("certificate"):
            result["certificate"]["binding_artifact_hash"] = binding
        self._requests[request["run_id"]] = deepcopy(request)
        self._snapshots[request["run_id"]] = deepcopy(snapshot)
        self._latest[request["run_id"]] = deepcopy(result)
        return result

    def formal_search_start(self, request):
        request, deployment = self._prepare(request)
        run_id = request["run_id"]
        if run_id in self._requests and without_nulls(self._requests[run_id]) != without_nulls(request):
            raise MathsAIAPIError("run_id already belongs to a different request")
        if request["input"]["kind"] == "source":
            self.upload_artifact(request["input"]["artifact_hash"])
        try:
            admission = self._call("POST", "/formal-search/start", request,
                                   expected_statuses=frozenset({200, 202}))
        except MathsAIAPIError as exc:
            if exc.status_code is not None:
                raise
            # A lost start reply is uncertain. Adopt only the run bound to this digest.
            snapshot = self._call("GET", "/formal-search/" + quote(run_id, safe="/"))
            return self._translate(snapshot, request, deployment=deployment)
        result = self._translate(admission, request, admission=True, deployment=deployment)
        if admission["status"] not in self._ACTIVE:
            return self.formal_search_status(run_id)
        return result

    def restore_run(self, request):
        """Reattach after client restart using the coordinator's original request."""
        request = deepcopy(dict(request))
        _check("formal-search-request", request)
        snapshot = self._call("GET", "/formal-search/" + quote(request["run_id"], safe="/"))
        return self._translate(snapshot, request)

    def _known(self, run_id):
        if run_id not in self._requests:
            raise MathsAIAPIError("unknown run; use restore_run(original_request) after restart")
        return self._requests[run_id]

    def formal_search_status(self, run_id):
        request = self._known(run_id)
        return self._translate(self._call("GET", "/formal-search/" + quote(run_id, safe="/")), request)

    def formal_search_resume(self, run_id, budget=None):
        request = self._known(run_id)
        if budget is not None and dict(budget) != request["budget"]:
            raise MathsAIAPIError("retry cannot replace or replenish the bound budget")
        snapshot = self._call("GET", "/formal-search/" + quote(run_id, safe="/"))
        result = self._translate(snapshot, request)
        retryable = not (snapshot.get("disposition") in {"invalid-request", "environment-error"}
                         and snapshot.get("input_artifact_hash") is None)
        if retryable and snapshot["status"] in {"finished", "interrupted"} and snapshot["budget"]["remaining_steps"] > 0 and not (snapshot.get("result") or {}).get("certificate"):
            snapshot = self._call("POST", "/formal-search/" + quote(run_id, safe="/") + "/retry",
                                  {}, expected_statuses=frozenset({202}))
            return self._translate(snapshot, request)
        return result

    def formal_search_cancel(self, run_id):
        request = self._known(run_id)
        snapshot = self._call("POST", "/formal-search/" + quote(run_id, safe="/") + "/cancel",
                              {}, expected_statuses=frozenset({202}))
        return self._translate(snapshot, request)

    def formal_replay(self, certificate, environment_hash, *, max_steps=None, wall_clock_ms=None):
        # The binding artifact makes replay independent of this client's old memory.
        if not certificate.get("binding_artifact_hash"):
            raise MathsAIAPIError("v2 replay requires the certificate's durable binding_artifact_hash")
        binding = self._json_artifact(self._local_bytes(certificate["binding_artifact_hash"]))
        if binding.get("format") != "persistent-atp-formal-v2-binding-v1":
            raise MathsAIAPIError("unsupported local certificate binding")
        request, snapshot = binding["request"], binding["snapshot"]
        stored = (snapshot.get("result") or {}).get("certificate") or {}
        if (certificate.get("artifact_hash") != stored.get("artifact_hash")
                or certificate.get("producer_run_id") != request["run_id"]
                or environment_hash != request["environment_hash"]):
            raise MathsAIAPIError("certificate does not match its durable run binding")
        self._check_snapshot(snapshot, request)
        # Check durable local bytes before replay; provider rechecks its own copies.
        for digest in (stored["artifact_hash"], snapshot["input_artifact_hash"], snapshot["source_artifact_hash"]):
            self._local_bytes(digest)
        budget = dict(self.replay_budget)
        if max_steps is not None:
            budget["max_steps"] = max_steps
        if wall_clock_ms is not None:
            budget["wall_clock_ms"] = wall_clock_ms
        payload = {"protocol_version": 2, "run_id": request["run_id"],
                   "certificate_hash": stored["artifact_hash"], "environment_hash": environment_hash,
                   "input_artifact_hash": snapshot["input_artifact_hash"],
                   "source_artifact_hash": snapshot["source_artifact_hash"], "budget": budget}
        _check("formal-replay-request", payload)
        body = _check("formal-replay-attestation", self._call("POST", "/formal-replay", payload))
        for key in ("run_id", "certificate_hash", "input_artifact_hash", "source_artifact_hash"):
            if body[key] != payload[key]:
                raise MathsAIAPIError(f"replay attestation {key} mismatch")
        if body["backend"] != snapshot["resolved"]["backend"] or body["source_origin"] != snapshot["source_origin"]:
            raise MathsAIAPIError("replay backend or source origin mismatch")
        if body["status"] != "inconclusive" and body["environment_hash"] != environment_hash:
            raise MathsAIAPIError("replay environment mismatch")
        attestation_hash = self._put(canonical_json(body))
        verdict = {"status": body["status"], "environment_hash": body["environment_hash"],
                   "attestation_artifact_hash": attestation_hash}
        if body.get("reason"):
            verdict["rejection_reason"] = body["reason"]
        self._replays[(request["run_id"], stored["artifact_hash"])] = deepcopy(verdict)
        result = deepcopy(self._latest.get(request["run_id"]))
        if result and result.get("certificate", {}).get("artifact_hash") == stored["artifact_hash"]:
            result["certificate"]["status"] = {"verified": "replay-accepted", "rejected": "replay-rejected", "inconclusive": "replay-pending"}[body["status"]]
            result["certificate"]["replay_result"] = verdict
            self._latest[request["run_id"]] = result
        return verdict

    def run_snapshot(self, run_id):
        """Validated provider view for leased admission; the stable API stays canonical."""
        return self.run_evidence(run_id)[0]

    def run_evidence(self, run_id):
        canonical = self.formal_search_status(run_id)
        return deepcopy(self._snapshots[run_id]), canonical

    def cached_evidence(self, run_id):
        """Return already validated evidence without network I/O."""
        return deepcopy(self._snapshots[run_id]), deepcopy(self._latest[run_id])

    def request_for(self, run_id):
        return deepcopy(self._known(run_id))

    def binding(self, run_id):
        snapshot = deepcopy(self._snapshots[run_id])
        if "replays" in snapshot:
            snapshot["replays"] = []
        return {"format": "persistent-atp-formal-v2-binding-v1",
                "request": self.request_for(run_id), "snapshot": snapshot}

    def restore(self, binding, *, refresh=True):
        if binding.get("format") != "persistent-atp-formal-v2-binding-v1":
            raise MathsAIAPIError("unsupported durable run binding")
        request = self.prepare(binding["request"])
        admission = "result" not in binding["snapshot"]
        self._check_snapshot(binding["snapshot"], request, admission=admission)
        self.restore_prepared(request)
        if not refresh:
            return self._translate(binding["snapshot"], request, admission=admission, local_only=True)
        return self.restore_run(binding["request"])

    def result(self, run_id):
        """Return settled canonical evidence, or None while the provider is active."""
        result = self.formal_search_status(run_id)
        return None if self._snapshots[run_id]["status"] in self._ACTIVE else result

    def formal_search_retry(self, run_id):
        request = self._known(run_id)
        snapshot = self._call("POST", "/formal-search/" + quote(run_id, safe="/") + "/retry",
                              {}, expected_statuses=frozenset({202}))
        return self._translate(snapshot, request)

    def replay_attestation(self, certificate, environment_hash, *, max_steps, wall_clock_ms):
        """Full stored native receipt for the independently leased replayer."""
        verdict = self.formal_replay(certificate, environment_hash, max_steps=max_steps, wall_clock_ms=wall_clock_ms)
        return self._json_artifact(self._local_bytes(verdict["attestation_artifact_hash"]))


def maths_ai_http_atp(base_url: str, *, artifacts: ArtifactRepository,
                      token: str | None = None, timeout: float = 30.0,
                      replay_budget=None) -> MathsAIHTTPFormalATP:
    return MathsAIHTTPFormalATP(base_url, artifacts=artifacts, token=token,
                               timeout=timeout, replay_budget=replay_budget)
