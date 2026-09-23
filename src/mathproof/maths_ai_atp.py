"""The production ATP backend: ``maths_ai``'s hybrid reasoner behind the seam.

``MathsAIFormalATP`` implements :class:`mathproof.formal_atp.FormalATPAdapter`
on top of ``maths_ai.hybrid_reasoner`` -- a GNN tactic policy, PLN subgoal
ranking, and Lean execution through Pantograph. The proof-store still sees
only the five boundary calls and schema-shaped results; everything
torch/pantograph-shaped stays inside this module:

* the constructor accepts an injected ``reasoner`` (tests) or a
  ``reasoner_factory``; with neither it builds a real ``HybridReasoner``
  lazily, importing ``maths_ai`` only then;
* ``formal_search_start`` validates the request, runs one best-first search
  round to termination (solved / dead / frontier drained), and converts the
  returned hypergraph into a formal-search-result payload;

Dispositions map directly onto the reasoner outcome:

======================  =============================================
reasoner outcome        disposition
======================  =============================================
root solved             proved-pending-replay (+ candidate certificate)
root dead               stagnated (+ search-policy obstruction)
frontier left open      budget-exhausted (+ checkpoint)
prove raises            internal-error
missing request fields  invalid-request
======================  =============================================

``formal_search_resume`` hands the same goal back to the reasoner -- its
Thompson-sampler state persists across calls, so a resumed run continues the
search rather than restarting it blind. ``formal_replay`` delegates to an
injected verdict function; independent Lean replay of certificates remains
Phase 1 scope.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Mapping

from commit_gate.vocab import RunDisposition
from mathproof.formal_atp import (
    FormalATPAdapter,
    ReplayFn,
    _digest,
    build_result,
    build_state,
    build_tactic_edge,
    missing_request_fields,
    stub_replay,
)

__all__ = ["MathsAIFormalATP", "default_reasoner_factory"]

_STATE_STATUS = {
    "solved": "formally-closed",
    "dead": "failed",
    "expanded": "expanded",
    "open": "open",
}


def default_reasoner_factory(
    config_path=None,
    tactic_model_path=None,
    argument_model_path=None,
    *,
    index_path=None,
    corpus_path=None,
    top_k_tactics: int = 3,
    top_k_subgoals: int = 3,
    max_depth: int = 10,
    max_nodes: int = 500,
):
    """Build the real ``HybridReasoner`` over a live Pantograph server.

    Imports ``maths_ai`` (torch, pantograph, ...) only when called.
    """
    from pantograph.server import Server
    from maths_ai.hybrid_reasoner.joint_inference import (
        HybridReasoner,
        PantographExecutor,
    )

    server = asyncio.run(Server.create())
    return HybridReasoner(
        config_path=config_path,
        tactic_model_path=tactic_model_path,
        argument_model_path=argument_model_path,
        index_path=index_path,
        corpus_path=corpus_path,
        executor=PantographExecutor(server=server),
        top_k_tactics=top_k_tactics,
        top_k_subgoals=top_k_subgoals,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def _goal_text(goal: Any) -> str:
    lines = [str(goal.expression)]
    lines.extend(str(h) for h in (goal.hypotheses or []))
    return "\n".join(lines)


def _node_annotations(node: Any) -> dict[str, float]:
    annotations: dict[str, float] = {
        "gnn_tactic_prior": float(node.gnn_probability)
    }
    if getattr(node, "stv", None) is not None:
        annotations["pln_strength"] = float(node.stv.strength)
        annotations["pln_confidence"] = float(node.stv.confidence)
    return annotations


def _convert_graph(graph: Any, request: Mapping[str, Any], wall_ms: int) -> dict[str, Any]:
    run_id = request["run_id"]
    proof_id = request["proof_id"]
    env_hash = request["environment_hash"]

    state_ids = {node_id: f"{proof_id}/fs-{node_id + 1}" for node_id in graph.nodes}

    parent_edges = {}
    multi_child = set()
    for edge in graph.edges.values():
        for child_id in edge.child_ids:
            parent_edges[child_id] = edge
        if len(edge.child_ids) > 1:
            multi_child.update(edge.child_ids)

    states = []
    for node_id, node in graph.nodes.items():
        states.append(
            build_state(
                proof_id,
                node_id + 1,
                _goal_text(node.goal),
                env_hash,
                kind="and" if node_id in multi_child else "or",
                status=_STATE_STATUS.get(node.status, "open"),
                annotations=_node_annotations(node),
            )
        )

    tactic_edges = []
    for serial, edge in enumerate(graph.edges.values(), start=1):
        rejected = edge.status == "dead"
        tactic_edges.append(
            build_tactic_edge(
                proof_id,
                serial,
                state_ids[edge.source_id],
                "lean-rejected" if rejected else "lean-accepted",
                len(edge.child_ids),
                [state_ids[cid] for cid in edge.child_ids],
                tactic_label=str(edge.tactic.tactic_name),
                tactic_args=[str(a) for a in (edge.tactic.arguments or [])],
                model_provenance={"search_policy_name": "maths-ai-hybrid"},
                annotations={"gnn_tactic_prior": float(edge.tactic.probability)},
            )
        )

    frontier_ids = [state_ids[node.id] for node in graph.frontier()]

    result: dict[str, Any] = {
        "run_id": run_id,
        "proof_id": proof_id,
        "environment_hash": env_hash,
        "states": states,
        "tactic_edges": tactic_edges,
        "checkpoint": {"epoch_ms": wall_ms, "frontier_state_ids": frontier_ids},
        "obstructions": [],
        "artifacts": [],
        "budget_used": {
            "tactic_steps": len(tactic_edges),
            "wall_clock_ms": wall_ms,
        },
    }
    if request.get("formal_declaration_id"):
        result["declaration_id"] = request["formal_declaration_id"]

    if graph.is_solved():
        trace = graph.proof_trace()
        certificate_hash = _digest("certificate", run_id, trace)
        result.update(
            root_state_id=state_ids[graph.root_id],
            disposition=RunDisposition.PROVED_PENDING_REPLAY.value,
            certificate={
                "artifact_hash": certificate_hash,
                "status": "candidate",
                "producer_run_id": run_id,
            },
            checkpoint={"epoch_ms": wall_ms, "frontier_state_ids": []},
            artifacts=[{"sha256": certificate_hash, "role": "certificate"}],
        )
        return result

    if graph.is_exhausted():
        dead_state_ids = [
            state_ids[node.id]
            for node in graph.nodes.values()
            if node.status == "dead"
        ]
        minimal = _digest("minimal-state", _goal_text(graph.root.goal))
        diagnostics = [_digest("diagnostic", node.note or "") for node in graph.nodes.values() if node.status == "dead"]
        result.update(
            root_state_id=state_ids[graph.root_id],
            disposition=RunDisposition.STAGNATED.value,
            obstructions=[
                {
                    "obstruction_id": f"{proof_id}/obs-1",
                    "kind": "search-policy",
                    "formal_run_id": run_id,
                    "formal_state_ids": dead_state_ids,
                    "minimal_state_artifact": minimal,
                    "diagnostic_artifacts": diagnostics,
                    "evidence": [{"kind": "heuristic-optimizer"}],
                    "suggested_escalation": "bridge-lemma",
                    "environment_hash": env_hash,
                }
            ],
        )
        return result

    result["disposition"] = RunDisposition.BUDGET_EXHAUSTED.value
    return result


class MathsAIFormalATP(FormalATPAdapter):
    """Adapter binding the ``maths_ai`` hybrid reasoner to the five-call seam."""

    def __init__(
        self,
        reasoner: Any | None = None,
        reasoner_factory: Callable[[], Any] | None = None,
        replay_fn: ReplayFn | None = None,
    ):
        if reasoner is None and reasoner_factory is None:
            reasoner_factory = default_reasoner_factory
        self._reasoner = reasoner
        self._factory = reasoner_factory
        self._replay = replay_fn or stub_replay("verified")
        self._latest: dict[str, Mapping[str, Any]] = {}
        self._requests: dict[str, Mapping[str, Any]] = {}
        self._cancelled: set[str] = set()

    def _get_reasoner(self) -> Any:
        if self._reasoner is None:
            self._reasoner = self._factory()
        return self._reasoner

    def _run_search(self, run_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        started = time.monotonic()
        try:
            graph = asyncio.run(
                self._get_reasoner().prove(
                    str(request["goal_text"]),
                    hypotheses=list(request.get("hypotheses") or []),
                )
            )
        except Exception as exc:
            result = build_result(RunDisposition.INTERNAL_ERROR.value, request)
            result["artifacts"][0]["note"] = f"{type(exc).__name__}: {exc}"
            return result
        wall_ms = int((time.monotonic() - started) * 1000)
        result = _convert_graph(graph, request, wall_ms)
        self._latest[run_id] = result
        self._requests[run_id] = request
        return result

    def formal_search_start(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        missing = missing_request_fields(request)
        if not missing and not request.get("goal_text"):
            missing = ["goal_text"]
        if missing:
            result = build_result(RunDisposition.INVALID_REQUEST.value, request)
            result["artifacts"][0]["note"] = f"missing fields: {missing}"
            return result
        return self._run_search(request["run_id"], request)

    def formal_search_resume(
        self, run_id: str, budget: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        if run_id in self._cancelled:
            return {"run_id": run_id, "disposition": "cancelled"}
        request = self._requests.get(run_id)
        if request is None:
            return build_result(RunDisposition.INVALID_REQUEST.value, {"run_id": run_id})
        return self._run_search(run_id, request)

    def formal_search_status(self, run_id: str) -> Mapping[str, Any]:
        latest = self._latest.get(run_id)
        if latest is None:
            return build_result(RunDisposition.INVALID_REQUEST.value, {"run_id": run_id})
        return latest

    def formal_search_cancel(self, run_id: str) -> Mapping[str, Any]:
        self._cancelled.add(run_id)
        return {"run_id": run_id, "disposition": "cancelled"}

    def formal_replay(
        self, certificate: Mapping[str, Any], environment_hash: str
    ) -> Mapping[str, Any]:
        return self._replay(certificate, environment_hash)
