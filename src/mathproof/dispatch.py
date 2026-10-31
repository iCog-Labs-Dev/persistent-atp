"""Dispatch: turn a lease into worker output, and output into ops (Section 6).

The dispatcher maps worker classes to `run(lease, context)` handlers. The
converters turn worker results into inert proposals for the commit gate.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from commit_gate.ops import AddEdge, SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.state import ReadView
from commit_gate.vocab import AttemptStatus
from .actions import STATEMENT_CHECK

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
      REVIEWS_CLAIM edge; only critic-accepted promotes a claim.
    * ``experiment`` -- a Lean statement check becomes an Experiment and
      closes its requesting action.
    * ``formalizer`` -- a Lean proposition becomes a draft FormalDeclaration.
    * ``llm-research``/``hyperon`` -- a proposed research move becomes a
      queued ResearchMove under its parent state.
    """
    worker_class = lease.worker_class
    if worker_class == "formalizer":
        ops = formalizer_result_to_ops(result, lease, view)
    elif worker_class == "experiment":
        ops = statement_check_result_to_ops(result, lease, view)
    elif worker_class == "formal-atp":
        ops = formal_result_to_ops(result, view)
    elif worker_class == "critic":
        ops = critic_result_to_ops(
            {**result, "actor": lease.actor},
            claim_id=lease.selected_move_id,
            attempt_id=attempt_id or f"{lease.proof_id}/at-{_next_serial(view, 'at', lease.proof_id)}",
        )
    else:
        ops = research_result_to_ops(
            result,
            proof_id=lease.proof_id,
            view=view,
            leased_move_id=lease.selected_move_id,
        )
    # The gate binds a leased proposal to its holder and move, so both come
    # from the lease the scheduler issued, never from what the worker reports.
    return Proposal(
        proof_id=lease.proof_id,
        actor=lease.actor,
        worker_class=worker_class,
        ops=tuple(ops),
        base_revision=lease.base_revision,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        selected_move_id=lease.selected_move_id,
    )


def formalizer_result_to_ops(result: Mapping[str, Any], lease, view: ReadView) -> list[Any]:
    """Commit only a draft statement linked to the leased claim."""
    declaration_id = f"{lease.proof_id}/fd-{_next_serial(view, 'fd', lease.proof_id)}"
    fields = {
        "status": "draft",
        "lean_name": result["lean_name"],
        "lean_type": result["lean_type"],
        "statement_source": result["statement_source"],
        "imports": result["imports"],
        "namespace": result["namespace"],
        "reasoning": result["reasoning"],
        "prompt_version": result["prompt_version"],
        "context_digest": result["context_digest"],
        "model_output": result["model_output"],
    }
    return [
        UpsertNode("FormalDeclaration", declaration_id, fields),
        AddEdge("DRAFTS_CLAIM", declaration_id, lease.selected_move_id, f"{declaration_id}-drafts"),
    ]


def statement_check_result_to_ops(result: Mapping[str, Any], lease, view: ReadView) -> list[Any]:
    """Record Lean's diagnostic and retire the action that requested it."""
    owner = view.node(lease.selected_move_id)
    if owner is None or owner.label not in {"ResearchMove", "Attempt"}:
        raise ValueError("statement check has no committed action owner")
    action = owner.fields.get("action" if owner.label == "ResearchMove" else "follow_up")
    if not isinstance(action, Mapping) or action.get("kind") != STATEMENT_CHECK:
        raise ValueError("statement check owner has no supported action")
    declaration_id = action.get("declaration_id")
    declaration = view.node(declaration_id)
    if declaration is None or declaration.label != "FormalDeclaration":
        raise ValueError("statement check declaration is missing")
    targets = {edge.dst_id for edge in view.edges_from(owner.node_id,
               "MOVE_TARGETS" if owner.label == "ResearchMove" else "REVIEWS_CLAIM")}
    claims = {edge.dst_id for edge in view.edges_from(declaration_id, "DRAFTS_CLAIM")}
    linked = sorted(targets & claims)
    if len(linked) != 1:
        raise ValueError("statement check does not target one claim")
    claim_id = linked[0]
    experiment_id = f"{lease.proof_id}/ex-{_next_serial(view, 'ex', lease.proof_id)}"
    fields = {
        "actor": lease.actor,
        "kind": STATEMENT_CHECK,
        "success": result["success"],
        "diagnostics": result["diagnostics"],
        "source_hash": result["source_hash"],
        "context_digest": result["context_digest"],
        "owner_id": owner.node_id,
        "declaration_id": declaration_id,
    }
    ops: list[Any] = [
        UpsertNode("Experiment", experiment_id, fields),
        AddEdge("CHECKS_CLAIM", experiment_id, claim_id, f"{experiment_id}-claim"),
        AddEdge("CHECKS_DECLARATION", experiment_id, declaration_id, f"{experiment_id}-declaration"),
        SetField(owner.label, owner.node_id, "action_result_id", experiment_id,
                 prior=owner.fields.get("action_result_id")),
    ]
    if owner.label == "ResearchMove":
        _advance_status(ops, owner.label, owner, {"status": "closed"})
    return ops


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
        serial = _next_serial(view, "fc", result["proof_id"])
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
        certificate_id = f"{result['proof_id']}/cert-{_next_serial(view, 'cert', result['proof_id'])}"
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
    """Record the review; only an explicit critic-accepted verdict promotes."""
    verdict = result["verdict"]
    fields = {
        "actor": result.get("actor", "critic"),
        "worker_class": "critic",
        "status": verdict,
    }
    for name in (
        "reasoning", "earliest_gap", "repair", "evidence_ids", "context_digest",
        "reported_verdict", "prompt_version", "model_output", "follow_up", "input_digest"
    ):
        if result.get(name) is not None:
            fields[name] = result[name]
    ops: list[Any] = [
        UpsertNode("Attempt", attempt_id, fields),
        AddEdge(
            "REVIEWS_CLAIM",
            attempt_id,
            claim_id,
            result.get("review_edge_id", f"{attempt_id}-reviews"),
        ),
    ]
    if verdict == AttemptStatus.CRITIC_ACCEPTED.value:
        ops.append(
            SetField("Claim", claim_id, "status", "critic-accepted", prior="provisional")
        )
    return ops


def research_result_to_ops(
    result: Mapping[str, Any], *, proof_id: str, view: ReadView,
    leased_move_id: str | None = None,
) -> list[Any]:
    """Queue proposed moves and retire a worked move through the gate."""
    parent_state_id = result["parent_state_id"]
    proposed = result.get("moves")
    if proposed is None:
        proposed = [{"move_id": result.get("move_id"), "detail": result.get("detail", "")}]
    serial = _next_serial(view, "rm", proof_id)
    ops: list[Any] = []
    for entry in proposed:
        move_id = entry.get("move_id") or f"{proof_id}/rm-{serial}"
        serial += 1
        fields = {key: value for key, value in entry.items() if key not in ("move_id", "status", "state_id")}
        fields.setdefault("detail", "")
        ops.append(UpsertNode("ResearchMove", move_id, {"status": "queued", "state_id": parent_state_id, **fields}))
        ops.append(AddEdge("PROPOSES", parent_state_id, move_id, f"{move_id}-proposed"))
        for index, claim_id in enumerate(result.get("target_claim_ids", ())):
            ops.append(AddEdge("MOVE_TARGETS", move_id, claim_id, f"{move_id}-target-{index}"))

    leased = view.node(leased_move_id) if leased_move_id else None
    if leased is not None and leased.label == "ResearchMove":
        if result.get("move_status"):
            _advance_status(ops, "ResearchMove", leased, {"status": result["move_status"]})
        for name in ("reasoning", "context_digest", "prompt_version", "model_output"):
            if result.get(name) is not None:
                ops.append(SetField(
                    "ResearchMove", leased.node_id, name, result[name],
                    prior=leased.fields.get(name),
                ))
    return ops


# -- helpers --------------------------------------------------------------------


def _advance_status(ops: list[Any], label: str, record, fields: Mapping[str, Any]) -> None:
    """Queue a compare-and-set status write only where it moves forward."""
    new_status = fields.get("status")
    current = record.fields.get("status")
    if new_status and new_status != current:
        ops.append(SetField(label, record.node_id, "status", new_status, prior=current))


def _next_serial(view: ReadView, prefix: str, proof_id: str) -> int:
    """One past the highest committed ``<prefix>-<serial>`` local id."""
    import re

    pattern = re.compile(
        r"^" + re.escape(proof_id) + r"/" + re.escape(prefix) + r"-([1-9][0-9]*)$"
    )
    highest = 0
    for record in view.list_nodes(proof_id):
        match = pattern.match(record.node_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1
