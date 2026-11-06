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
      queued ResearchMove, or a structured argument makes its exact target
      provisional for independent criticism.
    """
    worker_class = lease.worker_class
    if worker_class == "formalizer":
        ops = formalizer_result_to_ops(result, lease, view)
    elif worker_class == "experiment":
        ops = statement_check_result_to_ops(result, lease, view)
    elif worker_class == "formal-atp":
        ops = formal_result_to_ops(result, view)
    elif worker_class == "replayer":
        ops = replay_result_to_ops(result, lease, view)
    elif worker_class == "critic":
        ops = critic_result_to_ops(
            {**result, "actor": lease.actor},
            claim_id=lease.selected_move_id,
            attempt_id=attempt_id or f"{lease.proof_id}/at-{_next_serial(view, 'at', lease.proof_id)}",
            view=view,
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
    closed_states = {
        edge["source_state_id"]
        for edge in result.get("tactic_edges", [])
        if not edge.get("produced_goal_ids")
        and edge.get("executor_result") == "lean-accepted"
    }

    for state in result.get("states", []):
        fields = {
            key: value
            for key, value in state.items()
            if key not in ("state_id",) and value is not None
        }
        if state["state_id"] in closed_states:
            fields["status"] = "formally-closed"
        existing = view.node(state["state_id"])
        if existing is None:
            ops.append(UpsertNode("FormalState", state["state_id"], fields))
        else:
            _advance_status(ops, "FormalState", existing, fields)

    run_id = result.get("run_id")
    disposition = result.get("disposition")
    if run_id is not None:
        existing_run = view.node(run_id)
        run_fields = {
            "actor": result.get("actor", "formal-atp"),
            "status": disposition,
            "environment_hash": result.get("environment_hash"),
            "test_mode": bool(result.get("test_mode", False)),
        }
        run_fields = {key: value for key, value in run_fields.items() if value is not None}
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
        environment_id = result.get("environment_id")
        if environment_id and not view.edges_from(run_id, "RAN_UNDER"):
            ops.append(AddEdge("RAN_UNDER", run_id, environment_id, f"{run_id}-environment"))

        declaration = view.node(declaration_id) if declaration_id else None
        if declaration is not None and declaration.fields.get("status") == "aligned":
            ops.append(SetField(
                "FormalDeclaration", declaration_id, "status", "searching", prior="aligned"
            ))
            declaration_status = "searching"
        else:
            declaration_status = declaration.fields.get("status") if declaration else None
        if result.get("certificate") and declaration_status == "searching":
            ops.append(SetField(
                "FormalDeclaration", declaration_id, "status",
                "certificate-produced", prior="searching",
            ))

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
                    "environment_hash": result["environment_hash"],
                    "status": certificate.get("status", "candidate"),
                    "test_mode": bool(certificate.get("test_mode", False)),
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
        environment_id = result.get("environment_id")
        if environment_id:
            ops.append(AddEdge(
                "CERTIFICATE_ENVIRONMENT", certificate_id, environment_id,
                f"{certificate_id}-environment",
            ))
        claim_id = result.get("claim_id")
        if claim_id:
            ops.append(AddEdge(
                "PROVED_BY", claim_id, certificate_id, f"{claim_id}-proved-by-{certificate_id.rsplit('/', 1)[-1]}",
            ))

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


def replay_result_to_ops(
    result: Mapping[str, Any], lease, view: ReadView
) -> list[Any]:
    """Record one replay verdict and advance its certificate atomically."""
    certificate = view.node(lease.selected_move_id)
    if certificate is None or certificate.label != "Certificate":
        raise ValueError("replayer needs a committed certificate")
    if certificate.fields.get("status") != "candidate":
        raise ValueError("replayer only accepts candidate certificates")
    environment_edges = view.edges_from(certificate.node_id, "CERTIFICATE_ENVIRONMENT")
    if len(environment_edges) != 1:
        raise ValueError("certificate has no unique environment binding")
    environment_id = environment_edges[0].dst_id
    environment = view.node(environment_id)
    if environment is None:
        raise ValueError("certificate environment is missing")
    environment_hash = environment.fields.get("environment_hash")
    if result.get("environment_hash") != environment_hash:
        raise ValueError("replay result does not match the pinned environment")
    verdict = result.get("status")
    if verdict not in {"verified", "rejected"}:
        raise ValueError("replay result must be verified or rejected")

    replay_id = f"{lease.proof_id}/lr-{_next_serial(view, 'lr', lease.proof_id)}"
    replay_fields = {
        "actor": lease.actor,
        "status": verdict,
        "sorry_detected": result.get("sorry_detected", False),
        "environment_hash": environment_hash,
        "certificate_id": certificate.node_id,
        "artifact_hash": certificate.fields["artifact_hash"],
        "simulated": bool(result.get("simulated", False)),
        "test_mode": bool(result.get("test_mode", False)),
    }
    if result.get("rejection_reason"):
        replay_fields["rejection_reason"] = result["rejection_reason"]
    ops = [
        UpsertNode("LeanReplay", replay_id, replay_fields),
        AddEdge("REPLAYED_BY", certificate.node_id, replay_id, f"{certificate.node_id}-replayed-{replay_id.rsplit('/', 1)[-1]}"),
        AddEdge("REPLAY_ENVIRONMENT", replay_id, environment_id, f"{replay_id}-environment"),
        SetField("Certificate", certificate.node_id, "status", "replay-pending", prior="candidate"),
    ]
    if not result.get("simulated", False) or result.get("test_mode") is True:
        final_status = "replay-accepted" if verdict == "verified" else "replay-rejected"
        ops.append(SetField(
            "Certificate", certificate.node_id, "status",
            final_status, prior="replay-pending",
        ))
    return ops


def critic_result_to_ops(
    result: Mapping[str, Any], *, claim_id: str, attempt_id: str, view: ReadView
) -> list[Any]:
    """Record the review; promote supported claims only after alignment."""
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
    aligned = any(
        (alignment := view.node(edge.src_id)) is not None
        and alignment.label == "Alignment"
        and alignment.fields.get("lifecycle") == "reviewed"
        and alignment.fields.get("verdict") == "aligned"
        for edge in view.edges_to(claim_id, "ALIGNS_CLAIM")
    )
    if verdict == AttemptStatus.CRITIC_ACCEPTED.value or (
        verdict == AttemptStatus.SUPPORTED.value and aligned
    ):
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
    parent_state = view.node(parent_state_id)
    if parent_state is None or parent_state.label != "ResearchState" or not parent_state_id.startswith(f"{proof_id}/"):
        raise ValueError("research result names no state in this proof")
    depth = parent_state.fields.get("branch_depth", 0)
    if type(depth) is not int or depth < 0 or depth > 2:
        raise ValueError("research state has invalid branch depth")
    leased = view.node(leased_move_id) if leased_move_id else None
    proposed = result.get("moves")
    if proposed is None:
        proposed = [{"move_id": result.get("move_id"), "detail": result.get("detail", "")}]
    ops: list[Any] = []
    candidate = result.get("candidate_argument")
    if candidate is not None:
        if proposed:
            raise ValueError("research argument cannot also propose new moves")
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "claim_id", "argument", "prerequisite_claim_ids"
        }:
            raise ValueError("research argument has the wrong fields")
        claim_id = candidate["claim_id"]
        claim = view.node(claim_id) if isinstance(claim_id, str) else None
        targets = set(result.get("target_claim_ids", ()))
        leased_targets = {
            edge.dst_id for edge in view.edges_from(leased.node_id, "MOVE_TARGETS")
        } if leased is not None and leased.label == "ResearchMove" else set()
        if (
            claim is None
            or claim.label != "Claim"
            or not claim_id.startswith(f"{proof_id}/")
            or claim_id not in targets
            or leased_targets != {claim_id}
            or claim.fields.get("status") != "conjectural"
        ):
            raise ValueError("research argument does not name its conjectural leased target")
        argument = candidate["argument"]
        prerequisites = candidate["prerequisite_claim_ids"]
        if not isinstance(argument, str) or not argument.strip():
            raise ValueError("research argument must be nonempty")
        if not isinstance(prerequisites, list):
            raise ValueError("research argument prerequisites must be a list")
        dependencies = []
        for dependency_id in prerequisites:
            dependency = view.node(dependency_id) if isinstance(dependency_id, str) else None
            if (
                dependency is None
                or dependency.label != "Claim"
                or not dependency_id.startswith(f"{proof_id}/")
                or dependency_id == claim_id
            ):
                raise ValueError("research argument cites an unknown or circular prerequisite claim")
            if dependency_id not in dependencies:
                dependencies.append(dependency_id)
        ops.extend((
            SetField("Claim", claim_id, "argument", argument.strip(), prior=claim.fields.get("argument")),
            SetField("Claim", claim_id, "argument_source_move_id", leased_move_id,
                     prior=claim.fields.get("argument_source_move_id")),
            SetField("Claim", claim_id, "argument_context_digest", result.get("context_digest"),
                     prior=claim.fields.get("argument_context_digest")),
            SetField("Claim", claim_id, "status", "provisional", prior="conjectural"),
        ))
        existing_dependencies = {
            edge.dst_id for edge in view.edges_from(claim_id, "DEPENDS_ON")
        }
        for index, dependency_id in enumerate(dependencies):
            if dependency_id not in existing_dependencies:
                ops.append(AddEdge(
                    "DEPENDS_ON", claim_id, dependency_id,
                    f"{claim_id}-research-dependency-{index}",
                ))
    serial = _next_serial(view, "rm", proof_id)
    claim_serial = _next_serial(view, "c", proof_id)
    state_serial = _next_serial(view, "rs", proof_id)
    shared_subgoals: dict[str, str] = {}
    for entry in proposed:
        move_id = entry.get("move_id") or f"{proof_id}/rm-{serial}"
        serial += 1
        subgoals = entry.get("subgoals", [])
        if not isinstance(subgoals, list) or len(subgoals) > 3:
            raise ValueError("research alternative needs at most three subgoals")
        if subgoals and (depth >= 2 or entry.get("action") is not None):
            raise ValueError("subgoals exceed branch depth or conflict with an action")
        fields = {key: value for key, value in entry.items() if key not in ("move_id", "status", "state_id", "subgoals")}
        fields.setdefault("detail", "")
        ops.append(UpsertNode("ResearchMove", move_id, {
            "status": "closed" if subgoals else "queued",
            "state_id": parent_state_id, **fields,
        }))
        ops.append(AddEdge("PROPOSES", parent_state_id, move_id, f"{move_id}-proposed"))
        for index, claim_id in enumerate(result.get("target_claim_ids", ())):
            ops.append(AddEdge("MOVE_TARGETS", move_id, claim_id, f"{move_id}-target-{index}"))
        for index, subgoal in enumerate(subgoals):
            if not isinstance(subgoal, Mapping) or set(subgoal) != {"statement", "why_needed"}:
                raise ValueError("subgoal needs a statement and reason")
            statement, why = subgoal["statement"], subgoal["why_needed"]
            if not isinstance(statement, str) or not statement.strip() or not isinstance(why, str) or not why.strip():
                raise ValueError("subgoal statement and reason must be nonempty")
            key = " ".join(statement.split()).casefold()
            child_move_id = shared_subgoals.get(key)
            if child_move_id is None:
                claim_id = f"{proof_id}/c-{claim_serial}"
                state_id = f"{proof_id}/rs-{state_serial}"
                child_move_id = f"{proof_id}/rm-{serial}"
                claim_serial += 1
                state_serial += 1
                serial += 1
                ops.extend((
                    UpsertNode("Claim", claim_id, {
                        "status": "conjectural", "claim_text": statement.strip(),
                    }),
                    UpsertNode("ResearchState", state_id, {
                        "status": "open", "target_claim_id": claim_id,
                        "branch_depth": depth + 1,
                    }),
                    UpsertNode("ResearchMove", child_move_id, {
                        "status": "queued", "state_id": state_id,
                        "detail": f"Investigate subgoal: {statement.strip()}",
                    }),
                    AddEdge("PROPOSES", state_id, child_move_id, f"{child_move_id}-proposed"),
                    AddEdge("MOVE_TARGETS", child_move_id, claim_id, f"{child_move_id}-target-0"),
                ))
                shared_subgoals[key] = child_move_id
            ops.append(AddEdge(
                "REQUIRES_SUBGOAL", move_id, child_move_id,
                f"{move_id}-subgoal-{index}", {"why_needed": why.strip()},
            ))

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
