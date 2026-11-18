"""HTTP adapter for Mathematics-ai-atp's persistent API.

The remote service owns asynchronous execution and durable run snapshots.  This
module owns the translation between that provider-specific API and
``FormalATPAdapter``'s canonical request/result contract.

The provider exposes ``retry`` rather than checkpoint ``resume``. A retry
restarts only an interrupted, failed, or cancelled run with its original
request. This adapter therefore refuses budget changes and never describes a
retry as continuation from a saved search frontier.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from commit_gate.vocab import NON_KERNEL_TACTICS, RunDisposition
from mathproof.formal_atp import (
    FormalATPAdapter,
    _digest,
    build_result,
    missing_request_fields,
)
from mathproof.maths_ai_atp import _convert_graph
from mathproof.schemas import validate as validate_schema
from mathproof.soundness import validate_formal_search_result

__all__ = ["MathsAIAPIError", "MathsAIHTTPFormalATP", "maths_ai_http_atp"]


class MathsAIAPIError(RuntimeError):
    """A transport failure or invalid Mathematics-ai-atp response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _empty_result(
    disposition: str,
    request: Mapping[str, Any],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": request.get("run_id", "unscoped/fr-0"),
        "proof_id": request.get("proof_id", "unscoped"),
        "environment_hash": request.get("environment_hash") or _digest("env-missing"),
        "disposition": disposition,
        "states": [],
        "tactic_edges": [],
        "artifacts": [],
    }
    if request.get("formal_declaration_id"):
        result["declaration_id"] = request["formal_declaration_id"]
    if note:
        result["artifacts"] = [
            {
                "sha256": _digest("maths-ai-provider", disposition, note),
                "role": "provider-diagnostic",
                "note": note,
            }
        ]
    return result


def _remote_graph(summary: Mapping[str, Any], request: Mapping[str, Any]) -> Any:
    """Validate a provider hypergraph summary and expose the local graph API."""
    raw_nodes = summary.get("nodes")
    raw_edges = summary.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise MathsAIAPIError("formal-search result lacks a hypergraph summary")

    nodes: dict[int, Any] = {}
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise MathsAIAPIError("formal-search summary contains a non-object node")
        node_id = raw.get("id")
        expression = raw.get("expression")
        status = raw.get("status")
        prior = raw.get("gnn_probability")
        if type(node_id) is not int or node_id < 0 or node_id in nodes:
            raise MathsAIAPIError("formal-search summary has an invalid node identity")
        if not isinstance(expression, str) or not expression.strip():
            raise MathsAIAPIError("formal-search summary has an empty node expression")
        if status not in {"open", "expanded", "solved", "dead"}:
            raise MathsAIAPIError("formal-search summary has an invalid node status")
        if not _finite_number(prior):
            raise MathsAIAPIError("formal-search summary has an invalid GNN prior")
        raw_stv = raw.get("stv")
        stv = None
        if raw_stv is not None:
            if (
                not isinstance(raw_stv, Mapping)
                or not _finite_number(raw_stv.get("strength"))
                or not _finite_number(raw_stv.get("confidence"))
            ):
                raise MathsAIAPIError("formal-search summary has an invalid PLN score")
            stv = SimpleNamespace(
                strength=float(raw_stv["strength"]),
                confidence=float(raw_stv["confidence"]),
            )
        nodes[node_id] = SimpleNamespace(
            id=node_id,
            goal=SimpleNamespace(expression=expression, hypotheses=[]),
            status=status,
            gnn_probability=float(prior),
            stv=stv,
            note=raw.get("note") if isinstance(raw.get("note"), str) else None,
        )

    root_id = summary.get("root_id")
    if type(root_id) is not int or root_id not in nodes:
        raise MathsAIAPIError("formal-search summary has no valid root")
    if nodes[root_id].goal.expression != request["goal_text"]:
        raise MathsAIAPIError("formal-search root does not match the requested goal")
    nodes[root_id].goal.hypotheses = list(request.get("hypotheses") or [])

    edges: dict[int, Any] = {}
    for raw in raw_edges:
        if not isinstance(raw, Mapping):
            raise MathsAIAPIError("formal-search summary contains a non-object edge")
        edge_id = raw.get("id")
        source_id = raw.get("source_id")
        child_ids = raw.get("child_ids")
        tactic = raw.get("tactic")
        arguments = raw.get("arguments")
        probability = raw.get("probability")
        status = raw.get("status")
        if type(edge_id) is not int or edge_id < 0 or edge_id in edges:
            raise MathsAIAPIError("formal-search summary has an invalid edge identity")
        if type(source_id) is not int or source_id not in nodes:
            raise MathsAIAPIError("formal-search edge has an unknown source")
        if (
            not isinstance(child_ids, list)
            or any(type(child) is not int or child not in nodes for child in child_ids)
            or len(set(child_ids)) != len(child_ids)
        ):
            raise MathsAIAPIError("formal-search edge has invalid children")
        if not isinstance(tactic, str) or not tactic.strip():
            raise MathsAIAPIError("formal-search edge has an empty tactic")
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise MathsAIAPIError("formal-search edge has invalid tactic arguments")
        if not _finite_number(probability) or status not in {"pending", "solved", "dead"}:
            raise MathsAIAPIError("formal-search edge has invalid probability or status")

        # A dead edge with children represents an accepted transition whose
        # descendants failed.  A zero-child dead edge represents rejection.
        conversion_status = "pending" if status == "dead" and child_ids else status
        edges[edge_id] = SimpleNamespace(
            id=edge_id,
            source_id=source_id,
            child_ids=list(child_ids),
            status=conversion_status,
            tactic=SimpleNamespace(
                tactic_name=tactic,
                arguments=list(arguments),
                probability=float(probability),
            ),
        )

    solved = summary.get("solved")
    exhausted = summary.get("exhausted")
    if type(solved) is not bool or type(exhausted) is not bool or (solved and exhausted):
        raise MathsAIAPIError("formal-search summary has contradictory termination flags")
    if solved != (nodes[root_id].status == "solved"):
        raise MathsAIAPIError("formal-search solved flag contradicts the root status")
    if exhausted != (nodes[root_id].status == "dead"):
        raise MathsAIAPIError("formal-search exhausted flag contradicts the root status")

    return SimpleNamespace(
        nodes=nodes,
        edges=edges,
        root_id=root_id,
        root=nodes[root_id],
        is_solved=lambda: solved,
        is_exhausted=lambda: exhausted,
        frontier=lambda: [node for node in nodes.values() if node.status == "open"],
    )


def _trace_matches_graph(graph: Any, node_id: int, trace: Any, visiting: frozenset[int]) -> bool:
    """Return whether a provider trace is witnessed by solved kernel edges."""
    if node_id in visiting or node_id not in graph.nodes or not isinstance(trace, Mapping):
        return False
    if trace.get("goal") != graph.nodes[node_id].goal.expression:
        return False
    tactic = trace.get("tactic")
    arguments = trace.get("arguments", [])
    children = trace.get("subgoals", [])
    if (
        not isinstance(tactic, str)
        or tactic in NON_KERNEL_TACTICS
        or not isinstance(arguments, list)
        or not all(isinstance(arg, str) for arg in arguments)
        or not isinstance(children, list)
    ):
        return False
    path = visiting | {node_id}
    for edge in graph.edges.values():
        if (
            edge.source_id == node_id
            and edge.status == "solved"
            and edge.tactic.tactic_name == tactic
            and edge.tactic.arguments == arguments
            and len(edge.child_ids) == len(children)
            and all(
                _trace_matches_graph(graph, child_id, child_trace, path)
                for child_id, child_trace in zip(edge.child_ids, children)
            )
        ):
            return True
    return False


class MathsAIHTTPFormalATP(FormalATPAdapter):
    """Translate persistent-atp calls to Mathematics-ai-atp's HTTP API."""

    _ACTIVE = frozenset({"queued", "running", "cancelling"})
    _RETRYABLE = frozenset({"interrupted", "failed", "cancelled"})
    _TERMINAL = frozenset({"found", "verified", "unsolved", "exhausted"})

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) URL without query or fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._requests: dict[str, dict[str, Any]] = {}
        self._envelopes: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._proof_traces: dict[str, dict[str, Any]] = {}
        self._replays: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._admitted: set[str] = set()

    def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        expected_statuses: frozenset[int] = frozenset({200}),
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = json.load(response)
        except HTTPError as exc:
            detail = str(exc)
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
                detail = str(error_body.get("detail", error_body))
            except (OSError, UnicodeDecodeError, ValueError, AttributeError):
                pass
            raise MathsAIAPIError(
                f"Mathematics-ai-atp {method} {path} failed: {detail}",
                status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise MathsAIAPIError(
                f"Mathematics-ai-atp {method} {path} failed: {exc}"
            ) from exc
        if status not in expected_statuses:
            raise MathsAIAPIError(
                f"Mathematics-ai-atp {method} {path} returned HTTP {status}",
                status_code=status,
            )
        if not isinstance(body, dict):
            raise MathsAIAPIError("Mathematics-ai-atp response must be a JSON object")
        return body

    @staticmethod
    def _provider_envelope(request: Mapping[str, Any]) -> dict[str, Any]:
        budget = request.get("budget") or {}
        provider_budget: dict[str, Any] = {}
        if budget.get("wall_clock_ms") is not None:
            provider_budget["timeout"] = budget["wall_clock_ms"] / 1000
        return {
            "run_id": request["run_id"],
            "problem": {
                "goal": request["goal_text"],
                "hypotheses": list(request.get("hypotheses") or []),
            },
            "environment": {
                "project": request["environment_id"],
                "variables": {
                    "persistent_atp_environment_hash": request["environment_hash"],
                    "persistent_atp_lean_source_artifact": request["lean_source_artifact"],
                },
            },
            "budget": provider_budget,
        }

    def _check_snapshot(self, snapshot: Mapping[str, Any], run_id: str) -> str:
        if snapshot.get("run_id") != run_id:
            raise MathsAIAPIError("formal-search snapshot returned a mismatched run_id")
        envelope = self._envelopes[run_id]
        if snapshot.get("request") != envelope:
            raise MathsAIAPIError("formal-search snapshot did not echo the bound request")
        if snapshot.get("environment") != envelope["environment"]:
            raise MathsAIAPIError("formal-search snapshot changed the pinned environment")
        if snapshot.get("budget") != envelope["budget"]:
            raise MathsAIAPIError("formal-search snapshot changed the requested budget")
        status = snapshot.get("status")
        if status not in self._ACTIVE | self._RETRYABLE | self._TERMINAL:
            raise MathsAIAPIError(f"formal-search snapshot has unknown status {status!r}")
        return str(status)

    @staticmethod
    def _validate_result(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        schema_errors = validate_schema("formal-search-result", result)
        soundness = (
            validate_formal_search_result(
                result,
                pinned_environment_hash=request["environment_hash"],
            )
            if not schema_errors
            else ()
        )
        identity_errors = [
            key
            for key, expected in (
                ("run_id", request["run_id"]),
                ("proof_id", request["proof_id"]),
                ("declaration_id", request["formal_declaration_id"]),
                ("environment_hash", request["environment_hash"]),
            )
            if result.get(key) != expected
        ]
        if schema_errors or soundness or identity_errors:
            diagnostic = {
                "schema": schema_errors,
                "soundness": [finding.to_dict() for finding in soundness],
                "identity": identity_errors,
            }
            raise MathsAIAPIError(
                "translated formal-search result failed validation: "
                + json.dumps(diagnostic, sort_keys=True)
            )

    def _translate(self, snapshot: Mapping[str, Any], run_id: str) -> dict[str, Any]:
        request = self._requests[run_id]
        try:
            status = self._check_snapshot(snapshot, run_id)
            if status in self._ACTIVE:
                result = _empty_result(RunDisposition.SEARCHING.value, request)
            elif status == "cancelled":
                result = _empty_result(RunDisposition.CANCELLED.value, request)
            elif status == "interrupted":
                result = build_result(RunDisposition.ENVIRONMENT_ERROR.value, request)
                result["artifacts"][0]["note"] = "remote run was interrupted"
            elif status == "failed":
                result = build_result(RunDisposition.INTERNAL_ERROR.value, request)
                result["artifacts"][0]["note"] = str(snapshot.get("error") or "remote run failed")
            else:
                provider_result = snapshot.get("result")
                if not isinstance(provider_result, Mapping):
                    raise MathsAIAPIError(f"terminal run {status!r} has no result")
                summary = provider_result.get("summary")
                if not isinstance(summary, Mapping):
                    raise MathsAIAPIError("terminal run has no hypergraph summary")
                if (
                    provider_result.get("solved") != summary.get("solved")
                    or provider_result.get("exhausted") != summary.get("exhausted")
                ):
                    raise MathsAIAPIError("formal-search result contradicts its summary")
                graph = _remote_graph(summary, request)
                proof_trace = provider_result.get("proof_trace")
                if status in {"found", "verified"}:
                    if not graph.is_solved() or not _trace_matches_graph(
                        graph, graph.root_id, proof_trace, frozenset()
                    ):
                        raise MathsAIAPIError("found proof trace is not witnessed by the hypergraph")
                    self._proof_traces[run_id] = dict(proof_trace)
                elif graph.is_solved():
                    raise MathsAIAPIError(f"run status {status!r} hides a solved graph")
                if status == "exhausted" and not graph.is_exhausted():
                    raise MathsAIAPIError("exhausted run has a non-exhausted graph")
                if status == "unsolved" and graph.is_exhausted():
                    raise MathsAIAPIError("unsolved run has an exhausted graph")

                result = _convert_graph(graph, request, 0)
                if graph.is_solved():
                    certificate_hash = _digest("certificate", run_id, proof_trace)
                    result["certificate"]["artifact_hash"] = certificate_hash
                    result["artifacts"] = [
                        {"sha256": certificate_hash, "role": "certificate"}
                    ]
                    replay = self._replays.get((run_id, certificate_hash, request["environment_hash"]))
                    if replay is not None:
                        result["certificate"]["status"] = (
                            "replay-accepted"
                            if replay["status"] == "verified"
                            else "replay-rejected"
                        )
                        result["certificate"]["replay_result"] = dict(replay)

            self._validate_result(result, request)
        except MathsAIAPIError as exc:
            result = build_result(RunDisposition.INTERNAL_ERROR.value, request)
            result["artifacts"][0]["note"] = str(exc)
        self._latest[run_id] = result
        return result

    @staticmethod
    def _invalid_request(request: Mapping[str, Any], note: str) -> dict[str, Any]:
        result = build_result(RunDisposition.INVALID_REQUEST.value, request)
        result["artifacts"][0]["note"] = note
        return result

    def formal_search_start(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        missing = missing_request_fields(request)
        schema_errors = validate_schema("formal-search-request", request) if not missing else []
        if missing or schema_errors:
            return self._invalid_request(
                request,
                f"missing fields: {missing}; schema errors: {schema_errors}",
            )
        run_id = str(request["run_id"])
        if run_id in self._requests:
            if self._requests[run_id] != dict(request):
                return self._invalid_request(request, f"run {run_id!r} has a different request")
            if run_id in self._admitted:
                return self._invalid_request(request, f"run {run_id!r} already started")
            # The previous POST may have reached the server before its reply
            # was lost. Never send another POST while admission is uncertain.
            return self._reconcile_start(run_id)
        self._requests[run_id] = dict(request)
        self._envelopes[run_id] = self._provider_envelope(request)
        try:
            snapshot = self._call(
                "POST", "/formal-search/start", self._envelopes[run_id],
                expected_statuses=frozenset({202}),
            )
        except MathsAIAPIError as exc:
            # A 409 may mean a previous attempt was admitted. A transport
            # failure is ambiguous as well; reconcile both by identity.
            try:
                return self._reconcile_start(run_id)
            except MathsAIAPIError as lookup_error:
                if lookup_error.status_code == 404:
                    self._requests.pop(run_id, None)
                    self._envelopes.pop(run_id, None)
                    raise exc
                raise
        self._check_snapshot(snapshot, run_id)
        self._admitted.add(run_id)
        return self._translate(snapshot, run_id)

    def _reconcile_start(self, run_id: str) -> Mapping[str, Any]:
        snapshot = self._call("GET", "/formal-search/" + quote(run_id, safe="/"))
        self._check_snapshot(snapshot, run_id)
        self._admitted.add(run_id)
        return self._translate(snapshot, run_id)

    def formal_search_status(self, run_id: str) -> Mapping[str, Any]:
        if run_id not in self._requests:
            return self._invalid_request({"run_id": run_id}, f"unknown run {run_id!r}")
        path = "/formal-search/" + quote(run_id, safe="/")
        return self._translate(self._call("GET", path), run_id)

    def formal_search_resume(
        self, run_id: str, budget: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        request = self._requests.get(run_id)
        if request is None:
            return self._invalid_request({"run_id": run_id}, f"unknown run {run_id!r}")
        original_budget = request.get("budget") or {}
        if budget is not None and dict(budget) != original_budget:
            return self._invalid_request(
                request,
                "Mathematics-ai-atp retry restarts the original request and cannot change budget",
            )
        path = "/formal-search/" + quote(run_id, safe="/")
        snapshot = self._call("GET", path)
        try:
            status = self._check_snapshot(snapshot, run_id)
        except MathsAIAPIError:
            return self._translate(snapshot, run_id)
        if status in self._RETRYABLE:
            snapshot = self._call(
                "POST",
                path + "/retry",
                {},
                expected_statuses=frozenset({202}),
            )
        return self._translate(snapshot, run_id)

    def formal_search_cancel(self, run_id: str) -> Mapping[str, Any]:
        if run_id not in self._requests:
            return self._invalid_request({"run_id": run_id}, f"unknown run {run_id!r}")
        path = "/formal-search/" + quote(run_id, safe="/") + "/cancel"
        return self._translate(self._call("POST", path, {}), run_id)

    def formal_replay(
        self, certificate: Mapping[str, Any], environment_hash: str
    ) -> Mapping[str, Any]:
        run_id = certificate.get("producer_run_id")
        if not isinstance(run_id, str) or run_id not in self._requests:
            raise MathsAIAPIError("certificate does not identify a known producer run")
        request = self._requests[run_id]
        if environment_hash != request["environment_hash"]:
            raise MathsAIAPIError("replay environment does not match the search environment")
        latest_certificate = (self._latest.get(run_id) or {}).get("certificate") or {}
        if certificate.get("artifact_hash") != latest_certificate.get("artifact_hash"):
            raise MathsAIAPIError("certificate artifact does not match the stored proof trace")
        proof_trace = self._proof_traces.get(run_id)
        if proof_trace is None:
            raise MathsAIAPIError("producer run has no replayable proof trace")
        payload = {
            "run_id": run_id,
            "problem": {
                "goal": request["goal_text"],
                "hypotheses": list(request.get("hypotheses") or []),
            },
            "proof_trace": proof_trace,
        }
        try:
            body = self._call("POST", "/formal-replay", payload)
        except MathsAIAPIError as exc:
            # Mathematics-ai-atp maps a Lean tactic rejection (ValueError) to
            # HTTP 400. The request was already locally validated and bound to
            # the stored run, so this is an independent negative verdict.
            if exc.status_code != 400:
                raise
            verdict = {
                "status": "rejected",
                "environment_hash": environment_hash,
                "rejection_reason": str(exc),
            }
        else:
            if body.get("verified") is True and body.get("status") == "verified":
                if (body.get("run_id") != run_id
                    or body.get("certificate_hash") != latest_certificate["artifact_hash"]
                    or body.get("environment_hash") != environment_hash):
                    raise MathsAIAPIError(
                        "replay verification unavailable: provider did not attest "
                        "the run, certificate, and actual environment"
                    )
                verdict = {"status": "verified", "environment_hash": environment_hash}
            elif body.get("verified") is False and body.get("status") == "rejected":
                verdict = {
                    "status": "rejected",
                    "environment_hash": environment_hash,
                    "rejection_reason": str(body.get("error") or "Lean rejected the proof"),
                }
            else:
                raise MathsAIAPIError("formal-replay returned an invalid verdict")
        key = (run_id, latest_certificate["artifact_hash"], environment_hash)
        self._replays[key] = verdict
        if run_id in self._latest and "certificate" in self._latest[run_id]:
            updated = dict(self._latest[run_id])
            updated_certificate = dict(updated["certificate"])
            updated_certificate["status"] = (
                "replay-accepted" if verdict["status"] == "verified" else "replay-rejected"
            )
            updated_certificate["replay_result"] = dict(verdict)
            updated["certificate"] = updated_certificate
            self._validate_result(updated, request)
            self._latest[run_id] = updated
        return verdict


def maths_ai_http_atp(
    base_url: str,
    *,
    token: str | None = None,
    timeout: float = 30.0,
) -> MathsAIHTTPFormalATP:
    """Build a Mathematics-ai-atp HTTP adapter."""
    return MathsAIHTTPFormalATP(base_url, token=token, timeout=timeout)
