"""The formal ATP boundary and its backends.

``FormalATPAdapter`` is the stable seam between the proof-store and any Lean
backend (Section: Formal ATP Boundary). The proof-store never sees Pantograph,
processes, or Lean errors -- only these five calls and schema-shaped results.

Two implementations live behind the seam:

* ``FakeFormalATP`` (:mod:`mathproof.formal_atp`) -- the deterministic Phase 0
  reference: no real Lean dependency, able to emit every documented run
  disposition (6.10) on demand from its script:

      proved-pending-replay, budget-exhausted, stagnated, counterexample,
      invalid-request, environment-error, internal-error

* ``MathsAIFormalATP`` (:mod:`mathproof.maths_ai_atp`) -- the real backend,
  driving the ``maths_ai`` hybrid GNN/PLN reasoner over Pantograph.

Both share the request-field contract (:func:`missing_request_fields`) and the
payload builders (:func:`build_state`, :func:`build_tactic_edge`). The fake's
results are built by :func:`build_result`, which derives all identifiers and
hashes canonically from the request, so identical input always yields an
identical payload.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Iterable, Mapping

from commit_gate.canon import content_hash
from commit_gate.vocab import RunDisposition

__all__ = [
    "FormalATPAdapter",
    "FakeFormalATP",
    "build_result",
    "build_state",
    "build_tactic_edge",
    "missing_request_fields",
    "EMITTABLE_DISPOSITIONS",
    "stub_replay",
]

REQUEST_REQUIRED_FIELDS = (
    "proof_id",
    "claim_id",
    "formal_declaration_id",
    "run_id",
    "base_revision",
    "lease_id",
    "fencing_token",
    "lean_source_artifact",
    "environment_id",
    "environment_hash",
)

EMITTABLE_DISPOSITIONS = frozenset(
    {
        RunDisposition.PROVED_PENDING_REPLAY.value,
        RunDisposition.BUDGET_EXHAUSTED.value,
        RunDisposition.STAGNATED.value,
        RunDisposition.COUNTEREXAMPLE.value,
        RunDisposition.INVALID_REQUEST.value,
        RunDisposition.ENVIRONMENT_ERROR.value,
        RunDisposition.INTERNAL_ERROR.value,
    }
)
"""Every documented terminal disposition the adapter must be able to emit."""


class FormalATPAdapter:
    """Protocol for a Lean-backed search service.

    Subclassing is *not* required; any object with these methods satisfies the
    boundary. Kept as a plain base class (not typing.Protocol) so the fake,
    the maths-ai backend, and future adapters share one place that documents
    call semantics.
    """

    def formal_search_start(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError

    def formal_search_resume(
        self, run_id: str, budget: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def formal_search_status(self, run_id: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def formal_search_cancel(self, run_id: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def formal_replay(
        self, certificate: Mapping[str, Any], environment_hash: str
    ) -> Mapping[str, Any]:
        raise NotImplementedError


def missing_request_fields(request: Mapping[str, Any]) -> list[str]:
    """Required request fields absent from ``request``, in contract order."""
    return [f for f in REQUEST_REQUIRED_FIELDS if f not in request]


def _digest(*parts: Any) -> str:
    return content_hash(list(parts))


def build_state(proof_id: str, serial: int, goal_text: str, env_hash: str, **extra):
    exact = _digest("exact", goal_text, env_hash)
    semantic = _digest("semantic", goal_text)
    state = {
        "state_id": f"{proof_id}/fs-{serial}",
        "kind": extra.pop("kind", "or"),
        "goal_text": goal_text,
        "exact_state_hash": exact,
        "semantic_signature": semantic,
        "status": extra.pop("status", "open"),
    }
    if extra.get("annotations"):
        state["annotations"] = extra.pop("annotations")
    assert not extra, f"unused state fields {extra}"
    return state


def build_tactic_edge(
    proof_id: str,
    serial: int,
    source_state_id: str,
    executor_result: str,
    subgoal_count: int,
    produced_goal_ids: list[str],
    **extra,
):
    edge = {
        "tactic_id": f"{proof_id}/ta-{serial}",
        "source_state_id": source_state_id,
        "executor_result": executor_result,
        "subgoal_count": subgoal_count,
        "produced_goal_ids": produced_goal_ids,
    }
    edge.update(extra)
    return edge


def build_result(disposition: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Synthesize a schema-shaped result for `disposition` from `request`.

    Deterministic: all hashes are content digests of the request's identity.
    This is the single place Phase 0 fixture shapes come from.
    """
    if disposition not in EMITTABLE_DISPOSITIONS:
        raise ValueError(f"fake cannot emit disposition {disposition!r}")

    run_id = request.get("run_id", "unscoped/fr-0")
    proof_id = request.get("proof_id", "unscoped")
    env_hash = request.get("environment_hash") or _digest("env-missing")

    result: dict[str, Any] = {
        "run_id": run_id,
        "proof_id": proof_id,
        "environment_hash": env_hash,
        "disposition": disposition,
    }
    if request.get("formal_declaration_id"):
        result["declaration_id"] = request["formal_declaration_id"]

    if disposition == RunDisposition.INVALID_REQUEST.value:
        result["states"] = []
        result["tactic_edges"] = []
        result["artifacts"] = [
            {"sha256": _digest("invalid-request", sorted(request)), "role": "rejection-reason"}
        ]
        return result

    if disposition in (
        RunDisposition.ENVIRONMENT_ERROR.value,
        RunDisposition.INTERNAL_ERROR.value,
    ):
        # Infrastructure failure: no states were searched, nothing closed.
        result["states"] = []
        result["tactic_edges"] = []
        role = "environment-diagnostic" if "environment" in disposition else "crash-log"
        result["artifacts"] = [{"sha256": _digest(disposition, run_id), "role": role}]
        return result

    goal_text = request.get("goal_text", f"goal({run_id})")

    if disposition == RunDisposition.PROVED_PENDING_REPLAY.value:
        root = build_state(proof_id, 1, goal_text, env_hash)
        closer = build_tactic_edge(
            proof_id, 1, root["state_id"], "lean-accepted", 0, [],
            tactic_label="exact",
            tactic_args=[_digest("candidate-lemma").split(":")[1][:12]],
        )
        result.update(
            root_state_id=root["state_id"],
            states=[root],
            tactic_edges=[closer],
            checkpoint={"epoch_ms": 0, "frontier_state_ids": []},
            certificate={
                "artifact_hash": _digest("certificate", run_id),
                "status": "candidate",
                "producer_run_id": run_id,
            },
            obstructions=[],
            artifacts=[
                {"sha256": _digest("certificate", run_id), "role": "certificate"}
            ],
            budget_used={"tactic_steps": 1, "wall_clock_ms": 100},
        )
        return result

    if disposition == RunDisposition.COUNTEREXAMPLE.value:
        root = build_state(proof_id, 1, goal_text, env_hash)
        witness_artifact = _digest("witness", run_id)
        result.update(
            root_state_id=root["state_id"],
            states=[{**root, "status": "failed"}],
            tactic_edges=[],
            obstructions=[
                {
                    "obstruction_id": f"{proof_id}/obs-1",
                    "kind": "likely-false",
                    "formal_run_id": run_id,
                    "formal_state_ids": [root["state_id"]],
                    "minimal_state_artifact": witness_artifact,
                    "diagnostic_artifacts": [witness_artifact],
                    "evidence": [
                        {"kind": "random-sampling", "artifact_hash": witness_artifact}
                    ],
                    "suggested_escalation": "counterexample-search",
                    "environment_hash": env_hash,
                }
            ],
            artifacts=[{"sha256": witness_artifact, "role": "counterexample-witness"}],
            budget_used={"tactic_steps": 5, "wall_clock_ms": 500},
        )
        return result

    if disposition == RunDisposition.STAGNATED.value:
        root = build_state(proof_id, 1, goal_text, env_hash)
        child = build_state(proof_id, 2, goal_text + "' (after rw)", env_hash)
        dead = build_tactic_edge(
            proof_id, 1, root["state_id"], "lean-rejected", 0, [],
            tactic_label="simp",
            diagnostic_artifact=_digest("diagnostic", run_id),
            annotations={"failure_family": "apply-failure-no-matching-conclusion"},
        )
        obstruction = {
            "obstruction_id": f"{proof_id}/obs-1",
            "kind": "missing-lemma",
            "formal_run_id": run_id,
            "formal_state_ids": [child["state_id"]],
            "minimal_state_artifact": _digest("minimal-state", child["state_id"]),
            "diagnostic_artifacts": [_digest("diagnostic", run_id)],
            "premises_considered": ["Nat.add_comm"],
            "tactic_families_considered": ["simp", "rw", "apply"],
            "evidence": [{"kind": "heuristic-optimizer"}],
            "suggested_escalation": "bridge-lemma",
            "environment_hash": env_hash,
        }
        result.update(
            root_state_id=root["state_id"],
            states=[root, child],
            tactic_edges=[dead],
            checkpoint={"epoch_ms": 0, "frontier_state_ids": [child["state_id"]]},
            obstructions=[obstruction],
            artifacts=[],
            budget_used={"tactic_steps": 64, "wall_clock_ms": 4000},
        )
        return result

    # budget-exhausted: live frontier with an unproven multi-child expansion.
    root = build_state(proof_id, 1, goal_text, env_hash)
    left = build_state(proof_id, 2, goal_text + " [left]", env_hash)
    right = build_state(proof_id, 3, goal_text + " [right]", env_hash)
    expand = build_tactic_edge(
        proof_id, 1, root["state_id"], "lean-accepted", 2,
        [left["state_id"], right["state_id"]],
        tactic_label="constructor",
        annotations={"gnn_tactic_prior": 0.7},
    )
    result.update(
        root_state_id=root["state_id"],
        states=[root, left, right],
        tactic_edges=[expand],
        checkpoint={"epoch_ms": 0, "frontier_state_ids": [left["state_id"], right["state_id"]]},
        obstructions=[],
        artifacts=[],
        budget_used={"tactic_steps": 128, "wall_clock_ms": 9000},
    )
    return result


ReplayFn = Callable[[Mapping[str, Any], str], Mapping[str, Any]]


def stub_replay(outcome: str) -> ReplayFn:
    """A replay function with a fixed verdict: 'verified' or 'rejected'.

    The contract under test (C3) is that a certificate cannot become valid
    without going through this call; the verdict itself is Phase 1 scope.
    """
    if outcome not in ("verified", "rejected"):
        raise ValueError(f"stub replay verdict must be 'verified' or 'rejected', got {outcome!r}")

    def replay(certificate: Mapping[str, Any], environment_hash: str) -> Mapping[str, Any]:
        payload = {"status": outcome, "environment_hash": environment_hash}
        if outcome == "rejected":
            payload["rejection_reason"] = "stub-replay-rejection"
        return payload

    return replay


class FakeFormalATP(FormalATPAdapter):
    """Scripted, deterministic stand-in for a real ATP service.

    ``plan`` maps ``run_id`` to a sequence of steps. Each step is either a
    disposition name (the fake synthesizes the result) or a complete result
    payload used verbatim (for hand-authored traces). ``formal_search_start``
    returns the first step; each ``formal_search_resume`` returns the next;
    ``formal_search_status`` re-reads the latest without consuming.

    ``replay_fn`` defaults to accepting everything; pass ``stub_replay(...)``
    for fixed pass/fail behaviour.
    """

    def __init__(
        self,
        plan: Mapping[str, Iterable[str | Mapping[str, Any]]] | None = None,
        replay_fn: ReplayFn | None = None,
    ):
        self._scripts = {run: deque(script) for run, script in (plan or {}).items()}
        self._latest: dict[str, Mapping[str, Any]] = {}
        self._cancelled: set[str] = set()
        self._replay = replay_fn or stub_replay("verified")

    # -- search lifecycle -------------------------------------------------

    def formal_search_start(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        missing = missing_request_fields(request)
        if missing:
            result = build_result(RunDisposition.INVALID_REQUEST.value, request)
            result["artifacts"][0]["note"] = f"missing fields: {missing}"
            return result

        run_id = request["run_id"]
        script = self._scripts.get(run_id)
        if not script:
            result = build_result(RunDisposition.INVALID_REQUEST.value, request)
            result["artifacts"][0]["note"] = f"no script for run {run_id!r}"
            return result

        return self._consume(run_id, request)

    def formal_search_resume(
        self, run_id: str, budget: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        if run_id in self._cancelled:
            return {"run_id": run_id, "disposition": "cancelled"}
        script = self._scripts.get(run_id)
        if not script:
            return build_result(RunDisposition.INVALID_REQUEST.value, {"run_id": run_id})
        return self._consume(run_id, self._latest.get(run_id, {"run_id": run_id}))

    def formal_search_status(self, run_id: str) -> Mapping[str, Any]:
        latest = self._latest.get(run_id)
        if latest is None:
            return build_result(RunDisposition.INVALID_REQUEST.value, {"run_id": run_id})
        return latest

    def formal_search_cancel(self, run_id: str) -> Mapping[str, Any]:
        self._cancelled.add(run_id)
        self._scripts.get(run_id, deque()).clear()
        return {"run_id": run_id, "disposition": "cancelled"}

    # -- independent replay ------------------------------------------------

    def formal_replay(
        self, certificate: Mapping[str, Any], environment_hash: str
    ) -> Mapping[str, Any]:
        return self._replay(certificate, environment_hash)

    # -- internals -----------------------------------------------------------

    def _consume(self, run_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        step = self._scripts[run_id].popleft()
        result = dict(step) if isinstance(step, Mapping) else build_result(step, request)
        self._latest[run_id] = result
        return result
