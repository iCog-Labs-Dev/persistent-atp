"""Resolve scheduler-selected graph objects into compiler task identities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from commit_gate.state import belongs_to_proof
from shared.vocab import (
    DeclarationStatus,
    FormalStateStatus,
    ResearchMoveStatus,
    ResearchStateStatus,
    RunDisposition,
)

from .errors import ContextValidationError, FormalRunPreparationRequired
from .snapshot import ProofSnapshot

__all__ = [
    "ResolvedFormalTask",
    "ResolvedObstructionTask",
    "ResolvedResearchTask",
    "resolve_formal_task",
    "resolve_obstruction_task",
    "resolve_research_task",
]

_SELECTABLE_MOVES = frozenset(
    {
        ResearchMoveStatus.QUEUED.value,
        ResearchMoveStatus.OPEN.value,
        ResearchMoveStatus.LEASED.value,
    }
)
_DEAD_STATES = frozenset(
    {
        ResearchStateStatus.SUPERSEDED.value,
        ResearchStateStatus.REFUTED.value,
        ResearchStateStatus.STALE.value,
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedResearchTask:
    """The committed graph identities needed to compile one research move."""

    kernel_id: str
    active_state_id: str
    selected_move_id: str
    required_claim_ids: tuple[str, ...]
    seed_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedFormalTask:
    """Committed graph identities needed to resume one formal run."""

    kernel_id: str
    claim_id: str
    declaration_id: str
    run_id: str
    root_state_id: str
    environment_id: str
    premise_ids: tuple[str, ...]
    seed_ids: tuple[str, ...]
    is_resume: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedObstructionTask:
    """Committed identities needed to compile one formal obstruction."""

    kernel_id: str
    obstruction_id: str
    formal_run_id: str
    environment_id: str
    environment_hash: str
    failed_state_ids: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    seed_ids: tuple[str, ...]


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


_FORMAL_DECLARATION_STATUSES = frozenset(
    {DeclarationStatus.ALIGNED.value, DeclarationStatus.SEARCHING.value}
)
_FORMAL_ROOT_STATUSES = frozenset(
    {
        FormalStateStatus.OPEN.value,
        FormalStateStatus.EXPANDED.value,
        FormalStateStatus.REOPENED.value,
    }
)


def resolve_formal_task(
    snapshot: ProofSnapshot,
    selected_move_id: str,
    *,
    kernel_id: str | None = None,
    premise_ids: tuple[str, ...] = (),
) -> ResolvedFormalTask:
    """Resolve a scheduler-selected existing ``FormalRun``.

    Aligned declarations are scheduler candidates too, but they do not yet
    identify the run, root state, or execution environment required by the
    formal compiler. That case is reported explicitly so a coordinator can
    run its preparation step before requesting context.
    """
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    _require_scoped_id(snapshot.proof_id, "selected_move_id", selected_move_id)
    view = snapshot.view
    selected = view.node(selected_move_id)
    if selected is None:
        raise ContextValidationError(
            f"selected formal work {selected_move_id!r} is not committed"
        )
    if selected.label == "FormalDeclaration":
        if selected.fields.get("status") not in _FORMAL_DECLARATION_STATUSES:
            raise ContextValidationError(
                f"formal declaration {selected_move_id!r} is not schedulable"
            )
        raise FormalRunPreparationRequired(selected_move_id)
    if selected.label != "FormalRun":
        raise ContextValidationError(
            f"selected formal work {selected_move_id!r} is not a FormalRun "
            "or FormalDeclaration"
        )
    if selected.fields.get("status") != RunDisposition.SEARCHING.value:
        raise ContextValidationError(
            f"formal run {selected_move_id!r} is not searching"
        )

    declaration_id = _unique_target(
        snapshot, selected_move_id, "SEARCHES", "FormalDeclaration"
    )
    declaration = view.node(declaration_id)
    if declaration.fields.get("status") not in _FORMAL_DECLARATION_STATUSES:
        raise ContextValidationError(
            f"formal declaration {declaration_id!r} is not eligible for search"
        )

    root_state_id = _unique_target(
        snapshot, selected_move_id, "HAS_ROOT", "FormalState"
    )
    root = view.node(root_state_id)
    if root.fields.get("status") not in _FORMAL_ROOT_STATUSES:
        raise ContextValidationError(
            f"formal root state {root_state_id!r} is not searchable"
        )

    environment_id = _unique_target(
        snapshot, selected_move_id, "RAN_UNDER", "Environment"
    )
    pin_edges = view.edges_from(declaration_id, "PINNED_ENVIRONMENT")
    if pin_edges and (
        len(pin_edges) != 1 or pin_edges[0].dst_id != environment_id
    ):
        raise ContextValidationError(
            f"formal run {selected_move_id!r} environment disagrees with the "
            f"pin on declaration {declaration_id!r}"
        )

    declared_claim = declaration.fields.get("claim_id")
    if (
        kernel_id is not None
        and declared_claim is not None
        and kernel_id != declared_claim
    ):
        raise ContextValidationError(
            "configured formal kernel disagrees with the declaration claim_id"
        )
    claim_id = kernel_id or declared_claim
    _require_scoped_id(snapshot.proof_id, "formal kernel", claim_id)
    claim = view.node(claim_id)
    if claim is None or claim.label != "Claim":
        raise ContextValidationError(
            f"formal kernel {claim_id!r} is not a committed Claim"
        )

    if not isinstance(premise_ids, tuple):
        raise ContextValidationError("premise_ids must be a tuple")
    unique_premises = tuple(dict.fromkeys(premise_ids))
    for premise_id in unique_premises:
        _require_scoped_id(snapshot.proof_id, "formal premise", premise_id)
        premise = view.node(premise_id)
        if premise is None or premise.label != "FormalDeclaration":
            raise ContextValidationError(
                f"formal premise {premise_id!r} is not a committed "
                "FormalDeclaration"
            )

    seeds = tuple(dict.fromkeys((selected_move_id, *unique_premises)))
    return ResolvedFormalTask(
        kernel_id=claim_id,
        claim_id=claim_id,
        declaration_id=declaration_id,
        run_id=selected_move_id,
        root_state_id=root_state_id,
        environment_id=environment_id,
        premise_ids=unique_premises,
        seed_ids=seeds,
    )


def resolve_obstruction_task(
    snapshot: ProofSnapshot,
    selected_move_id: str,
    *,
    eligible_dispositions: frozenset[str],
    kernel_id: str | None = None,
) -> ResolvedObstructionTask:
    """Resolve a scheduler-selected obstruction from committed failure data."""
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    if not isinstance(eligible_dispositions, frozenset) or not all(
        isinstance(item, str) and item.strip() for item in eligible_dispositions
    ):
        raise ContextValidationError(
            "eligible_dispositions must be a frozenset of non-empty strings"
        )
    _require_scoped_id(snapshot.proof_id, "selected_move_id", selected_move_id)
    view = snapshot.view
    obstruction = view.node(selected_move_id)
    if obstruction is None or obstruction.label != "Obstruction":
        raise ContextValidationError(
            f"selected obstruction {selected_move_id!r} is not committed"
        )

    raised_by = view.edges_to(selected_move_id, "RAISED_OBSTRUCTION")
    if len(raised_by) != 1:
        raise ContextValidationError(
            f"obstruction {selected_move_id!r} must have exactly one "
            "RAISED_OBSTRUCTION source"
        )
    formal_run_id = raised_by[0].src_id
    _require_scoped_id(snapshot.proof_id, "formal_run_id", formal_run_id)
    run = view.node(formal_run_id)
    if run is None or run.label != "FormalRun":
        raise ContextValidationError(
            f"obstruction source {formal_run_id!r} is not a committed FormalRun"
        )
    disposition = run.fields.get("status") or run.fields.get("disposition")
    if disposition not in eligible_dispositions:
        raise ContextValidationError(
            f"formal run {formal_run_id!r} disposition {disposition!r} is not "
            "eligible for obstruction compilation"
        )
    recorded_run = obstruction.fields.get("formal_run_id")
    if recorded_run is not None and recorded_run != formal_run_id:
        raise ContextValidationError(
            "obstruction formal_run_id disagrees with RAISED_OBSTRUCTION"
        )

    state_edges = view.edges_from(selected_move_id, "AT_STATE")
    if not state_edges:
        raise ContextValidationError(
            f"obstruction {selected_move_id!r} must identify at least one "
            "FormalState"
        )
    failed_state_ids = tuple(
        sorted(dict.fromkeys(edge.dst_id for edge in state_edges))
    )
    for state_id in failed_state_ids:
        _require_scoped_id(snapshot.proof_id, "failed state", state_id)
        state = view.node(state_id)
        if state is None or state.label != "FormalState":
            raise ContextValidationError(
                f"obstruction state {state_id!r} is not a committed FormalState"
            )
    recorded_states = obstruction.fields.get("formal_state_ids")
    if recorded_states is not None:
        if not isinstance(recorded_states, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in recorded_states
        ):
            raise ContextValidationError(
                "obstruction formal_state_ids must be a list of non-empty strings"
            )
        if len(set(recorded_states)) != len(recorded_states) or set(
            recorded_states
        ) != set(failed_state_ids):
            raise ContextValidationError(
                "obstruction formal_state_ids disagree with AT_STATE relationships"
            )

    kernel_edges = view.edges_to(selected_move_id, "RESOLVES")
    if len(kernel_edges) != 1:
        raise ContextValidationError(
            f"obstruction {selected_move_id!r} must have exactly one RESOLVES Claim"
        )
    related_kernel = kernel_edges[0].src_id
    if kernel_id is not None and kernel_id != related_kernel:
        raise ContextValidationError(
            "configured obstruction kernel disagrees with the RESOLVES Claim"
        )
    resolved_kernel = kernel_id or related_kernel
    _require_scoped_id(snapshot.proof_id, "obstruction kernel", resolved_kernel)
    kernel = view.node(resolved_kernel)
    if kernel is None or kernel.label != "Claim":
        raise ContextValidationError(
            f"obstruction kernel {resolved_kernel!r} is not a committed Claim"
        )

    environment_id = _unique_target(
        snapshot, formal_run_id, "RAN_UNDER", "Environment"
    )
    environment = view.node(environment_id)
    environment_hash = environment.fields.get("environment_hash")
    if (
        not isinstance(environment_hash, str)
        or _SHA256_RE.fullmatch(environment_hash) is None
    ):
        raise ContextValidationError(
            f"environment {environment_id!r} has no valid environment_hash"
        )
    for owner, value in (
        ("formal run", run.fields.get("environment_hash")),
        ("obstruction", obstruction.fields.get("environment_hash")),
    ):
        if value is not None and value != environment_hash:
            raise ContextValidationError(
                f"{owner} environment_hash disagrees with {environment_id!r}"
            )

    attempt_ids = tuple(
        sorted(
            dict.fromkeys(
                edge.src_id
                for state_id in failed_state_ids
                for edge in view.edges_to(state_id, "ON_STATE")
            )
        )
    )
    for attempt_id in attempt_ids:
        _require_scoped_id(snapshot.proof_id, "failure attempt", attempt_id)
        attempt = view.node(attempt_id)
        if attempt is None or attempt.label != "Attempt":
            raise ContextValidationError(
                f"failure attempt {attempt_id!r} is not a committed Attempt"
            )

    seeds = tuple(
        dict.fromkeys(
            (
                selected_move_id,
                formal_run_id,
                *failed_state_ids,
                *attempt_ids,
            )
        )
    )
    return ResolvedObstructionTask(
        kernel_id=resolved_kernel,
        obstruction_id=selected_move_id,
        formal_run_id=formal_run_id,
        environment_id=environment_id,
        environment_hash=environment_hash,
        failed_state_ids=failed_state_ids,
        attempt_ids=attempt_ids,
        seed_ids=seeds,
    )


def resolve_research_task(
    snapshot: ProofSnapshot,
    selected_move_id: str,
    *,
    kernel_id: str | None = None,
) -> ResolvedResearchTask:
    """Resolve and validate a scheduler-selected ``ResearchMove``.

    The ``PROPOSES`` relationship is the canonical owner of a move.  The
    theorem kernel is either supplied explicitly, targeted by ``MOVE_TARGETS``,
    or (as a fallback) the proof's unique ``HAS_TARGET`` claim.
    """
    if not isinstance(snapshot, ProofSnapshot):
        raise ContextValidationError("snapshot must be a ProofSnapshot")
    _require_scoped_id(snapshot.proof_id, "selected_move_id", selected_move_id)

    view = snapshot.view
    move = view.node(selected_move_id)
    if move is None or move.label != "ResearchMove":
        raise ContextValidationError(
            f"selected move {selected_move_id!r} is not a committed ResearchMove"
        )
    status = move.fields.get("status")
    if status not in _SELECTABLE_MOVES:
        raise ContextValidationError(
            f"selected move {selected_move_id!r} has non-selectable status {status!r}"
        )

    parent_edges = view.edges_to(selected_move_id, "PROPOSES")
    if len(parent_edges) != 1:
        raise ContextValidationError(
            f"selected move {selected_move_id!r} must have exactly one PROPOSES parent"
        )
    active_state_id = parent_edges[0].src_id
    _require_scoped_id(snapshot.proof_id, "active_state_id", active_state_id)
    state = view.node(active_state_id)
    if state is None or state.label != "ResearchState":
        raise ContextValidationError(
            f"PROPOSES parent {active_state_id!r} is not a committed ResearchState"
        )
    state_status = state.fields.get("status")
    if state_status in _DEAD_STATES:
        raise ContextValidationError(
            f"active state {active_state_id!r} has dead status {state_status!r}"
        )
    if state_status != ResearchStateStatus.OPEN.value:
        raise ContextValidationError(
            f"active state {active_state_id!r} has invalid status {state_status!r}"
        )

    declared_state = move.fields.get("state_id")
    if declared_state is not None:
        if not isinstance(declared_state, str) or not declared_state.strip():
            raise ContextValidationError("ResearchMove state_id must be a non-empty string")
        normalized_state = (
            declared_state
            if "/" in declared_state
            else f"{snapshot.proof_id}/{declared_state}"
        )
        if normalized_state != active_state_id:
            raise ContextValidationError(
                f"ResearchMove state_id {declared_state!r} disagrees with "
                f"PROPOSES parent {active_state_id!r}"
            )

    required_claim_ids = _required_claim_ids(
        snapshot.proof_id, move.fields.get("required_claim_ids", ()), view
    )
    resolved_kernel = _resolve_kernel_id(
        snapshot, selected_move_id, explicit_kernel_id=kernel_id
    )
    seeds = tuple(
        dict.fromkeys((active_state_id, selected_move_id, *required_claim_ids))
    )
    return ResolvedResearchTask(
        kernel_id=resolved_kernel,
        active_state_id=active_state_id,
        selected_move_id=selected_move_id,
        required_claim_ids=required_claim_ids,
        seed_ids=seeds,
    )


def _resolve_kernel_id(
    snapshot: ProofSnapshot,
    selected_move_id: str,
    *,
    explicit_kernel_id: str | None,
) -> str:
    view = snapshot.view
    if explicit_kernel_id is not None:
        candidates = (explicit_kernel_id,)
    else:
        move_targets = tuple(
            edge.dst_id for edge in view.edges_from(selected_move_id, "MOVE_TARGETS")
        )
        if move_targets:
            candidates = move_targets
        else:
            candidates = tuple(
                edge.dst_id
                for proof in view.list_nodes(snapshot.proof_id, "Proof")
                for edge in view.edges_from(proof.node_id, "HAS_TARGET")
            )
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ContextValidationError(
            "research task must resolve exactly one theorem kernel Claim"
        )
    kernel_id = unique[0]
    _require_scoped_id(snapshot.proof_id, "kernel_id", kernel_id)
    kernel = view.node(kernel_id)
    if kernel is None or kernel.label != "Claim":
        raise ContextValidationError(
            f"kernel {kernel_id!r} is not a committed Claim"
        )
    return kernel_id


def _required_claim_ids(proof_id: str, raw: object, view) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(claim_id, str) and claim_id.strip() for claim_id in raw
    ):
        raise ContextValidationError(
            "ResearchMove required_claim_ids must be a list of non-empty strings"
        )
    required = tuple(dict.fromkeys(raw))
    for claim_id in required:
        _require_scoped_id(proof_id, "required claim", claim_id)
        claim = view.node(claim_id)
        if claim is None or claim.label != "Claim":
            raise ContextValidationError(
                f"required claim {claim_id!r} is not a committed Claim"
            )
    return required


def _unique_target(
    snapshot: ProofSnapshot,
    source_id: str,
    relation: str,
    target_label: str,
) -> str:
    edges = snapshot.view.edges_from(source_id, relation)
    if len(edges) != 1:
        raise ContextValidationError(
            f"{source_id!r} must have exactly one {relation} target"
        )
    target_id = edges[0].dst_id
    _require_scoped_id(snapshot.proof_id, f"{relation} target", target_id)
    target = snapshot.view.node(target_id)
    if target is None or target.label != target_label:
        raise ContextValidationError(
            f"{relation} target {target_id!r} is not a committed {target_label}"
        )
    return target_id


def _require_scoped_id(proof_id: str, name: str, identity: object) -> None:
    if not isinstance(identity, str) or not identity.strip():
        raise ContextValidationError(f"{name} must be a non-empty string")
    if not belongs_to_proof(identity, proof_id):
        raise ContextValidationError(
            f"{name} {identity!r} does not belong to proof {proof_id!r}"
        )
