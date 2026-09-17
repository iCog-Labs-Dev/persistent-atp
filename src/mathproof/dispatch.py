"""Dispatch: turn a lease into worker output, and output into ops (Section 6).

The dispatcher is a seam: `ScriptedDispatcher` serves tests and the Phase 3
harness; real LLM/Hyperon workers implement the same `run(lease, context)`
contract later. The converters here are the missing glue between a
formal-search-result payload (or a critic/explorer verdict) and an inert
`Proposal` the commit gate will accept.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView
from commit_gate.vocab import AttemptStatus

__all__ = ["Dispatcher", "ScriptedDispatcher", "result_to_proposal"]


class Dispatcher:
    """A worker-class router: run one leased move to a structured result."""

    def run(self, lease, context: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError


class ScriptedDispatcher(Dispatcher):
    """Maps worker_class -> handler(lease, context) for tests and staging."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[Any, Mapping[str, Any]], Mapping[str, Any]]],
    ):
        self._handlers = dict(handlers)

    def run(self, lease, context: Mapping[str, Any]) -> Mapping[str, Any]:
        handler = self._handlers.get(lease.worker_class)
        if handler is None:
            raise NotImplementedError(
                f"no dispatcher handler for worker class {lease.worker_class!r}"
            )
        return handler(lease, context)


# -- result -> proposal --------------------------------------------------------


def result_to_proposal(
    result: Mapping[str, Any],
    lease,
    view: ReadView,
    *,
    attempt_id: str | None = None,
) -> Proposal:
    """Convert any dispatched worker's result into an inert `Proposal`.

    Dispatch by the leased worker class:

    * ``formal-atp`` -- a formal-search-result payload becomes state/tactic/
      checkpoint/certificate/obstruction ops.
    * ``critic``     -- a verdict becomes the critic's Attempt plus its
      REVIEWS_CLAIM edge (and the claim promotion when favorable).
    * ``llm-research``/``hyperon`` -- a proposed research move becomes a
      queued ResearchMove under its parent state.
    """
    worker_class = lease.worker_class
    if worker_class == "formal-atp":
        ops = formal_result_to_ops(result, view)
    elif worker_class == "critic":
        ops = critic_result_to_ops(
            result,
            claim_id=lease.selected_move_id,
            attempt_id=attempt_id or f"{lease.proof_id}/at-{_next_serial(view, 'at')}",
        )
    else:
        ops = research_result_to_ops(
            result,
            proof_id=lease.proof_id,
            view=view,
        )
    return Proposal(
        proof_id=lease.proof_id,
        actor=result.get("actor", lease.worker_class),
        worker_class=worker_class,
        ops=tuple(ops),
        base_revision=lease.base_revision,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
    )


def formal_result_to_ops(
    result: Mapping[str, Any], view: ReadView
) -> list[Any]:
    """Ops committing one formal-search-result payload.

    Creates what is absent, annotates what exists: states, tactic edges with
    their required children, checkpoints, terminal dispositions, candidate
    certificates and obstructions. Every op is idempotent, so replaying a
    result already committed leaves committed state unchanged.
    """
    ops: list[Any] = []

    for state in result.get("states", []):
        fields = {
            key: value
            for key, value in state.items()
            if key not in ("state_id",) and value is not None
        }
        existing = view.node(state["state_id"])
        if existing is None:
            ops.append(UpsertNode("FormalState", state["state_id"], fields))
        else:
            _advance_status(ops, "FormalState", existing, fields)

    run_id = result.get("run_id")
    disposition = result.get("disposition")
    if run_id is not None:
        existing_run = view.node(run_id)
        run_fields = {"actor": result.get("actor", "formal-atp"), "status": disposition}
        if existing_run is None:
            ops.append(UpsertNode("FormalRun", run_id, run_fields))
        elif disposition:
            _advance_status(ops, "FormalRun", existing_run, {"status": disposition})
        root_id = result.get("root_state_id")
        if root_id is not None and existing_run is None:
            ops.append(
                AddEdge("HAS_ROOT", run_id, root_id, f"{run_id}-root")
            )
        searched = {edge.dst_id for edge in view.edges_from(run_id, "SEARCHES")}
        declaration_id = result.get("declaration_id")
        if declaration_id and declaration_id not in searched:
            ops.append(
                AddEdge("SEARCHES", run_id, declaration_id, f"{run_id}-searches")
            )

    for edge in result.get("tactic_edges", []):
        tactic_id = edge["tactic_id"]
        if view.node(tactic_id) is None:
            fields = {
                key: value
                for key, value in edge.items()
                if key
                not in (
                    "tactic_id",
                    "source_state_id",
                    "produced_goal_ids",
                    "tactic_args",
                    "model_provenance",
                )
                and value is not None
            }
            fields.setdefault("subgoal_count", len(edge.get("produced_goal_ids", [])))
            if edge.get("tactic_label") is not None:
                fields["tactic_label"] = edge["tactic_label"]
            if edge.get("tactic_args") is not None:
                # Arguments are immutable provenance; keep them addressable.
                fields["tactic_family"] = str(edge["tactic_args"])
            ops.append(UpsertNode("TacticApplication", tactic_id, fields))
            ops.append(
                AddEdge(
                    "HAS_TACTIC", edge["source_state_id"], tactic_id, f"{tactic_id}-at"
                )
            )
            produced = edge.get("produced_goal_ids", [])
            for child_index, goal_id in enumerate(produced):
                ops.append(
                    AddEdge(
                        "FORMAL_REQUIRES",
                        tactic_id,
                        goal_id,
                        f"{tactic_id}-req-{child_index}",
                        {"child_index": child_index},
                    )
                )
            if not produced and edge.get("executor_result") == "lean-accepted":
                ops.append(
                    AddEdge(
                        "CLOSES_STATE",
                        tactic_id,
                        edge["source_state_id"],
                        f"{tactic_id}-closes",
                    )
                )

    checkpoint = result.get("checkpoint")
    if checkpoint and checkpoint.get("frontier_state_ids"):
        serial = _next_serial(view, "fc")
        checkpoint_id = f"{result['proof_id']}/fc-{serial}"
        ops.append(
            UpsertNode(
                "FormalCheckpoint",
                checkpoint_id,
                {
                    "epoch_ms": checkpoint.get("epoch_ms", 0),
                    "actor": result.get("actor", "formal-atp"),
                },
            )
        )
        ops.append(
            AddEdge("HAS_CHECKPOINT", run_id, checkpoint_id, f"{checkpoint_id}-in")
        )
        for position, state_id in enumerate(checkpoint["frontier_state_ids"]):
            ops.append(
                AddEdge(
                    "CHECKPOINT_FRONTIER",
                    checkpoint_id,
                    state_id,
                    f"{checkpoint_id}-{position}",
                )
            )

    certificate = result.get("certificate")
    if certificate:
        certificate_id = f"{result['proof_id']}/cert-{_next_serial(view, 'cert')}"
        ops.append(
            UpsertNode(
                "Certificate",
                certificate_id,
                {
                    "actor": result.get("actor", "formal-atp"),
                    "producer_run_id": result["run_id"],
                    "artifact_hash": certificate["artifact_hash"],
                    "status": certificate.get("status", "candidate"),
                },
            )
        )
        ops.append(
            AddEdge(
                "PRODUCED_CERTIFICATE",
                result["run_id"],
                certificate_id,
                f"{certificate_id}-produced",
            )
        )
        declaration_id = result.get("declaration_id")
        if declaration_id:
            ops.append(
                AddEdge(
                    "CERTIFIES",
                    certificate_id,
                    declaration_id,
                    f"{certificate_id}-certifies",
                )
            )

    for obstruction in result.get("obstructions", []):
        obstruction_id = obstruction["obstruction_id"]
        if view.node(obstruction_id) is not None:
            continue
        ops.append(
            UpsertNode(
                "Obstruction",
                obstruction_id,
                {
                    "kind": obstruction["kind"],
                    "description": obstruction.get("description", ""),
                    "actor": result.get("actor", "formal-atp"),
                },
            )
        )
        ops.append(
            AddEdge(
                "RAISED_OBSTRUCTION",
                obstruction["formal_run_id"],
                obstruction_id,
                f"{obstruction_id}-raised",
            )
        )
        for position, state_id in enumerate(obstruction.get("formal_state_ids", [])):
            ops.append(
                AddEdge(
                    "AT_STATE",
                    obstruction_id,
                    state_id,
                    f"{obstruction_id}-state-{position}",
                )
            )

    return ops


def critic_result_to_ops(
    result: Mapping[str, Any], *, claim_id: str, attempt_id: str
) -> list[Any]:
    """A critic verdict: the Attempt, its review edge, maybe the promotion."""
    verdict = result["verdict"]
    ops: list[Any] = [
        UpsertNode(
            "Attempt",
            attempt_id,
            {
                "actor": result.get("actor", "critic"),
                "worker_class": "critic",
                "status": verdict,
            },
        ),
        AddEdge(
            "REVIEWS_CLAIM",
            attempt_id,
            claim_id,
            result.get("review_edge_id", f"{attempt_id}-reviews"),
        ),
    ]
    if verdict in (AttemptStatus.SUPPORTED.value, AttemptStatus.CRITIC_ACCEPTED.value):
        ops.append(
            SetField("Claim", claim_id, "status", "critic-accepted", prior="provisional")
        )
    return ops


def research_result_to_ops(
    result: Mapping[str, Any], *, proof_id: str, view: ReadView
) -> list[Any]:
    """An explorer outcome: a queued ResearchMove under a parent state."""
    parent_state_id = result["parent_state_id"]
    serial = _next_serial(view, "rm")
    move_id = result.get("move_id") or f"{proof_id}/rm-{serial}"
    detail = result.get("detail", "")
    return [
        UpsertNode(
            "ResearchMove",
            move_id,
            {"status": "queued", "detail": detail},
        ),
        AddEdge("PROPOSES", parent_state_id, move_id, f"{move_id}-proposed"),
    ]


# -- helpers --------------------------------------------------------------------


def _advance_status(ops: list[Any], label: str, record, fields: Mapping[str, Any]) -> None:
    """Queue a compare-and-set status write only where it moves forward."""
    new_status = fields.get("status")
    current = record.fields.get("status")
    if new_status and new_status != current:
        ops.append(SetField(label, record.node_id, "status", new_status, prior=current))


def _next_serial(view: ReadView, prefix: str) -> int:
    """One past the highest committed ``<prefix>-<serial>`` local id."""
    import re

    pattern = re.compile(r"^[^/]+/" + re.escape(prefix) + r"-([1-9][0-9]*)$")
    highest = 0
    for node_id in getattr(view, "nodes", {}):
        match = pattern.match(node_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1
