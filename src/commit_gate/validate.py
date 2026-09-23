"""Validators for a proposed change.

Most checks are decidable from the proposal alone: they read no committed
state, run before any lock is taken, and need no graph backend. Checks that do
need committed state take a `ReadView` and are skipped when none is supplied.

Each validator yields one `Rejection` per violation; `validate_proposal` runs
all of them and returns every finding, so a worker gets a complete diagnosis
rather than the first failure.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterator, Mapping

from .ops import UNSET, AddEdge, Op, RemoveEdge, SetField, UpsertNode
from .proposal import Proposal
from .reasons import Reason, Rejection
from .state import ReadView
from .transitions import IMMUTABLE_FIELDS, STATUS_TRANSITIONS
from .vocab import (
    TERMINAL_EXECUTOR_FAILURES,
    AlignmentLifecycle,
    AlignmentVerdict,
    AttemptStatus,
    CertificateStatus,
    ClaimStatus,
    DeclarationStatus,
    ExecutorResult,
    FormalStateStatus,
    ObstructionKind,
    ReplayStatus,
    RunDisposition,
    TacticStatus,
    WorkerClass,
)

__all__ = [
    "validate_proposal",
    "check_concurrency_tokens",
    "check_vocabulary",
    "check_namespace",
    "check_subgoal_conservation",
    "check_executor_result",
    "check_annotation_separation",
    "check_references",
    "check_prior_values",
    "check_status_transitions",
    "check_immutability",
    "check_stagnation_obstruction",
    "check_replay_evidence",
    "check_self_certification",
    "check_alignment_gate",

]

ENUM_FIELDS: dict[tuple[str, str], type] = {
    ("Claim", "status"): ClaimStatus,
    ("FormalState", "status"): FormalStateStatus,
    ("FormalDeclaration", "status"): DeclarationStatus,
    ("Certificate", "status"): CertificateStatus,
    ("Alignment", "lifecycle"): AlignmentLifecycle,
    ("Alignment", "verdict"): AlignmentVerdict,
    ("FormalRun", "status"): RunDisposition,
    ("TacticApplication", "status"): TacticStatus,
    ("TacticApplication", "executor_result"): ExecutorResult,
    ("LeanReplay", "status"): ReplayStatus,
    ("Obstruction", "kind"): ObstructionKind,
    ("Attempt", "status"): AttemptStatus,
    ("Attempt", "worker_class"): WorkerClass,
}
"""Fields whose values must come from a closed vocabulary."""

CLOSED_STATE_VALUES = frozenset(
    {FormalStateStatus.FORMALLY_CLOSED.value, FormalStateStatus.LEAN_VERIFIED.value}
)
TERMINAL_FAILURE_VALUES = frozenset(m.value for m in TERMINAL_EXECUTOR_FAILURES)

UNSCOPED_LABELS = frozenset({"Artifact"})
"""Labels that are content-addressed and therefore carry no proof scope."""

EDGE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "HAS_TACTIC": ("FormalState", "TacticApplication"),
    "FORMAL_REQUIRES": ("TacticApplication", "FormalState"),
    "CLOSES_STATE": ("TacticApplication", "FormalState"),
    "HAS_ROOT": ("FormalRun", "FormalState"),
    "HAS_CHECKPOINT": ("FormalRun", "FormalCheckpoint"),
    "CHECKPOINT_FRONTIER": ("FormalCheckpoint", "FormalState"),
    "RAN_UNDER": ("FormalRun", "Environment"),
    "SEARCHES": ("FormalRun", "FormalDeclaration"),
    "PRODUCED_CERTIFICATE": ("FormalRun", "Certificate"),
    "CERTIFIES": ("Certificate", "FormalDeclaration"),
    "CERTIFICATE_ENVIRONMENT": ("Certificate", "Environment"),
    "REPLAYED_BY": ("Certificate", "LeanReplay"),
    "REPLAY_ENVIRONMENT": ("LeanReplay", "Environment"),
    "PINNED_ENVIRONMENT": ("FormalDeclaration", "Environment"),
    "ALIGNS_CLAIM": ("Alignment", "Claim"),
    "ALIGNS_DECLARATION": ("Alignment", "FormalDeclaration"),
    "PROVED_BY": ("Claim", "Certificate"),
    "DEPENDS_ON": ("Claim", "Claim"),
    "PROMOTED_TO": ("SpeculativeHypothesis", "Claim"),
    "RAISED_OBSTRUCTION": ("FormalRun", "Obstruction"),
    "AT_STATE": ("Obstruction", "FormalState"),
    "RESOLVES": ("Claim", "Obstruction"),
    "HAS_TARGET": ("Proof", "Claim"),
}
"""Required endpoint labels per relationship type.

Relationship types absent from this table are not endpoint-checked, the same
way `ENUM_FIELDS` skips fields with no closed vocabulary. `DEPENDS_ON` being
`Claim -> Claim` is what stops a speculative hypothesis being used as a
dependency without waiting for an audit query to notice.
"""


def validate_proposal(proposal: Proposal, view: ReadView | None = None) -> list[Rejection]:
    """Run every validator and collect all violations.
    
    If `view` is provided, state-dependent validators are also run.
    """
    findings: list[Rejection] = []
    findings.extend(check_concurrency_tokens(proposal))
    findings.extend(check_vocabulary(proposal))
    findings.extend(check_namespace(proposal))
    findings.extend(check_subgoal_conservation(proposal))
    findings.extend(check_executor_result(proposal))
    findings.extend(check_annotation_separation(proposal))

    if view is not None:
        findings.extend(check_references(proposal, view))
        findings.extend(check_prior_values(proposal, view))
        findings.extend(check_status_transitions(proposal, view))
        findings.extend(check_immutability(proposal, view))
        findings.extend(check_stagnation_obstruction(proposal, view))
        findings.extend(check_replay_evidence(proposal, view)) 
        findings.extend(check_self_certification(proposal, view)) 
        findings.extend(check_alignment_gate(proposal, view))

    return findings


def check_concurrency_tokens(proposal: Proposal) -> Iterator[Rejection]:
    """The proposal carries the tokens the journal needs to check it.

    `base_revision` is required of everyone: without it there is nothing to
    compare the head against, and stale work commits silently. A status-class
    op additionally needs the lease, because it changes what committed state
    means — two holders writing the same status must not both win.
    """
    if proposal.base_revision is None:
        yield Rejection(
            Reason.MISSING_CONCURRENCY_TOKEN,
            "proposal names no base_revision; it cannot be checked against the journal",
        )

    if proposal.lease_id is not None and proposal.fencing_token is not None:
        return
    for index, op in enumerate(proposal.ops):
        if op.op_class == "status":
            missing = ", ".join(
                name
                for name, value in (
                    ("lease_id", proposal.lease_id),
                    ("fencing_token", proposal.fencing_token),
                )
                if value is None
            )
            yield Rejection(
                Reason.MISSING_CONCURRENCY_TOKEN,
                f"status-class op requires the write lease; missing {missing}",
                index,
            )
            return


def check_vocabulary(proposal: Proposal) -> Iterator[Rejection]:
    """Every enum-valued field carries a literal from its closed vocabulary."""
    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode):
            for name, value in op.fields.items():
                yield from _check_literal(index, op.label, name, value)
        elif isinstance(op, SetField):
            yield from _check_literal(index, op.label, op.field, op.value)


def _check_literal(index: int, label: str, name: str, value: Any) -> Iterator[Rejection]:
    vocabulary = ENUM_FIELDS.get((label, name))
    if vocabulary is None:
        return
    try:
        vocabulary(value)
    except ValueError:
        legal = ", ".join(sorted(member.value for member in vocabulary))
        yield Rejection(
            Reason.UNKNOWN_STATUS_VALUE,
            f"{label}.{name} = {value!r}; legal values: {legal}",
            index,
        )


def check_namespace(proposal: Proposal) -> Iterator[Rejection]:
    """Every identity is scoped to the proposal's proof."""
    prefix = f"{proposal.proof_id}/"
    for index, op in enumerate(proposal.ops):
        for role, identity in _identities(op):
            if _is_content_addressed(identity) or not isinstance(identity, str):
                continue
            if not identity.startswith(prefix):
                yield Rejection(
                    Reason.NAMESPACE_MISMATCH,
                    f"{role} {identity!r} is not scoped to {prefix!r}",
                    index,
                )


def _identities(op: Op) -> Iterator[tuple[str, Any]]:
    if isinstance(op, UpsertNode):
        if op.label not in UNSCOPED_LABELS:
            yield "node id", op.node_id
    elif isinstance(op, SetField):
        if op.label not in UNSCOPED_LABELS:
            yield "node id", op.node_id
    elif isinstance(op, AddEdge):
        yield "edge source", op.src_id
        yield "edge target", op.dst_id
        yield "edge id", op.edge_id
    elif isinstance(op, RemoveEdge):
        yield "edge id", op.edge_id


def _is_content_addressed(identity: Any) -> bool:
    return isinstance(identity, str) and identity.startswith("sha256:")


def check_subgoal_conservation(proposal: Proposal) -> Iterator[Rejection]:
    """Invariant 2: every goal Lean produced becomes a required child.

    A tactic's arity is fixed when it is created, so the tactic node and all of
    its `FORMAL_REQUIRES` edges must arrive in the same proposal. An edge whose
    source tactic is absent would change the arity of an already-validated
    tactic, and removal of such an edge would drop an obligation outright.
    """
    tactics: dict[str, tuple[int, Any]] = {}
    children: dict[str, list[tuple[int, str, Any]]] = defaultdict(list)

    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode) and op.label == "TacticApplication":
            tactics[op.node_id] = (index, op.fields)
        elif isinstance(op, AddEdge) and op.rel_type == "FORMAL_REQUIRES":
            child_index = dict(op.fields or {}).get("child_index")
            children[op.src_id].append((index, op.dst_id, child_index))
        elif isinstance(op, RemoveEdge) and op.rel_type == "FORMAL_REQUIRES":
            yield Rejection(
                Reason.FORMAL_REQUIRES_REMOVAL,
                f"edge {op.edge_id!r} carries a formal obligation and cannot be removed",
                index,
            )

    for tactic_id, entries in children.items():
        if tactic_id not in tactics:
            yield Rejection(
                Reason.ORPHAN_SUBGOAL_EDGE,
                f"tactic {tactic_id!r} is not created in this proposal, "
                "so its arity cannot be verified",
                entries[0][0],
            )

    for tactic_id, (index, fields) in tactics.items():
        entries = children.get(tactic_id, [])
        declared = dict(fields).get("subgoal_count")

        if declared is None:
            yield Rejection(
                Reason.MISSING_REQUIRED_FIELD,
                f"tactic {tactic_id!r} declares no subgoal_count",
                index,
            )
        elif declared != len(entries):
            yield Rejection(
                Reason.SUBGOAL_COUNT_MISMATCH,
                f"tactic {tactic_id!r} declares {declared} subgoal(s) "
                f"but the proposal carries {len(entries)} required child edge(s)",
                index,
            )

        targets = [target for _, target, _ in entries]
        for duplicate in sorted({t for t in targets if targets.count(t) > 1}):
            yield Rejection(
                Reason.SUBGOAL_DUPLICATED,
                f"tactic {tactic_id!r} requires {duplicate!r} more than once",
                index,
            )

        positions = sorted(pos for _, _, pos in entries if pos is not None)
        if positions != list(range(len(entries))):
            yield Rejection(
                Reason.SUBGOAL_INDEX_INVALID,
                f"tactic {tactic_id!r} child_index values {positions} "
                f"are not exactly 0..{len(entries) - 1}",
                index,
            )


def check_executor_result(proposal: Proposal) -> Iterator[Rejection]:
    """11.3: only a Lean-accepted zero-goal transition closes a leaf.

    A timeout, missing backend, parse failure, crash, or empty model output is
    an infrastructure failure. It cannot close a state, and it cannot be
    recorded as a dead edge carrying a mathematical diagnostic.
    """
    tactics: dict[str, tuple[int, Any]] = {}
    closures: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode) and op.label == "TacticApplication":
            tactics[op.node_id] = (index, op.fields)
        elif isinstance(op, AddEdge) and op.rel_type == "CLOSES_STATE":
            closures[op.src_id].append((index, op.dst_id))

    for tactic_id, (index, raw) in tactics.items():
        fields = dict(raw)
        result = fields.get("executor_result")
        diagnostic = fields.get("diagnostic_artifact")

        if result is None:
            yield Rejection(
                Reason.MISSING_REQUIRED_FIELD,
                f"tactic {tactic_id!r} declares no executor_result",
                index,
            )
            continue

        if result in TERMINAL_FAILURE_VALUES:
            if fields.get("status") == TacticStatus.CLOSED.value:
                yield Rejection(
                    Reason.EXECUTOR_FAILURE_AS_SUCCESS,
                    f"tactic {tactic_id!r} reported {result!r} but is marked closed",
                    index,
                )
            if diagnostic is not None:
                yield Rejection(
                    Reason.FAILURE_WITH_MATHEMATICAL_DIAGNOSTIC,
                    f"tactic {tactic_id!r} reported infrastructure failure {result!r} "
                    "and cannot carry a mathematical diagnostic",
                    index,
                )
        elif result == ExecutorResult.LEAN_REJECTED.value and diagnostic is None:
            yield Rejection(
                Reason.DEAD_EDGE_MISSING_DIAGNOSTIC,
                f"tactic {tactic_id!r} was rejected by Lean without a diagnostic",
                index,
            )

    for tactic_id, entries in closures.items():
        if tactic_id not in tactics:
            yield Rejection(
                Reason.ORPHAN_CLOSURE_EDGE,
                f"closure claims tactic {tactic_id!r}, which this proposal does not create",
                entries[0][0],
            )
            continue

        fields = dict(tactics[tactic_id][1])
        result = fields.get("executor_result")
        declared = fields.get("subgoal_count")

        for index, state_id in entries:
            if result != ExecutorResult.LEAN_ACCEPTED.value:
                yield Rejection(
                    Reason.CLOSURE_WITHOUT_LEAN_ACCEPTED,
                    f"tactic {tactic_id!r} reported {result!r} and cannot close {state_id!r}",
                    index,
                )
            if declared != 0:
                yield Rejection(
                    Reason.CLOSURE_WITHOUT_ZERO_GOALS,
                    f"tactic {tactic_id!r} left {declared} subgoal(s) "
                    f"and cannot close {state_id!r}",
                    index,
                )


def check_annotation_separation(proposal: Proposal) -> Iterator[Rejection]:
    """Invariant 3: a heuristic score never justifies a status change.

    Two rules. Any non-annotation write states what it expected to overwrite,
    so a stale worker cannot silently clobber a status. And a proposal that
    closes a state while carrying scores for that same state, with no
    Lean-accepted closure edge, is a score being used as a proof.
    """
    closed_targets: dict[str, int] = {}
    annotated: set[str] = set()
    evidence: set[str] = set()

    for index, op in enumerate(proposal.ops):
        if isinstance(op, SetField):
            if op.prior is UNSET and op.op_class != "annotation":
                yield Rejection(
                    Reason.MISSING_PRIOR_VALUE,
                    f"{op.op_class} write to {op.label}.{op.field} on {op.node_id!r} "
                    "states no expected prior value",
                    index,
                )
            if op.op_class == "annotation":
                annotated.add(op.node_id)
            elif (
                op.label == "FormalState"
                and op.field == "status"
                and op.value in CLOSED_STATE_VALUES
            ):
                closed_targets.setdefault(op.node_id, index)
        elif isinstance(op, UpsertNode):
            if op.label == "FormalState" and any(
                _is_annotation(name) for name in op.fields
            ):
                annotated.add(op.node_id)
        elif isinstance(op, AddEdge) and op.rel_type == "CLOSES_STATE":
            evidence.add(op.dst_id)

    for node_id, index in closed_targets.items():
        if node_id in annotated and node_id not in evidence:
            yield Rejection(
                Reason.HEURISTIC_CLOSURE_ATTEMPT,
                f"state {node_id!r} is closed alongside score updates "
                "with no Lean-accepted closure edge",
                index,
            )


def _is_annotation(name: str) -> bool:
    from .vocab import ANNOTATION_FIELDS

    return name in ANNOTATION_FIELDS


def check_references(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """Node targets must exist, edge endpoints must match type definitions, and
    a removal must name an edge that is really there under that rel type."""
    created_nodes = {op.node_id: op.label for op in proposal.ops if isinstance(op, UpsertNode)}
    added_edges = {op.edge_id: op.rel_type for op in proposal.ops if isinstance(op, AddEdge)}
    removed_edges: set[str] = set()

    def get_label(node_id: str) -> str | None:
        if node_id in created_nodes:
            return created_nodes[node_id]
        record = view.node(node_id)
        return record.label if record else None

    def get_rel(edge_id: str) -> str | None:
        if edge_id in added_edges:
            return added_edges[edge_id]
        record = view.edge(edge_id)
        return record.rel_type if record else None

    for index, op in enumerate(proposal.ops):
        if isinstance(op, SetField):
            if get_label(op.node_id) is None:
                yield Rejection(
                    Reason.UNKNOWN_NODE,
                    f"SetField targets unknown node {op.node_id!r}",
                    index,
                )
            elif get_label(op.node_id) != op.label:
                yield Rejection(
                    Reason.NODE_ALREADY_EXISTS_WITH_LABEL,
                    f"SetField label {op.label!r} does not match node {op.node_id!r}",
                    index,
                )
        elif isinstance(op, AddEdge):
            src_label = get_label(op.src_id)
            if src_label is None:
                yield Rejection(Reason.UNKNOWN_NODE, f"source {op.src_id!r} unknown", index)
            
            dst_label = get_label(op.dst_id)
            if dst_label is None and not _is_content_addressed(op.dst_id):
                yield Rejection(Reason.UNKNOWN_NODE, f"target {op.dst_id!r} unknown", index)

            expected = EDGE_ENDPOINTS.get(op.rel_type)
            if expected is not None:
                exp_src, exp_dst = expected
                if src_label is not None and src_label != exp_src:
                    yield Rejection(
                        Reason.EDGE_ENDPOINT_TYPE_INVALID,
                        f"{op.rel_type} source {op.src_id!r} is {src_label}, expected {exp_src}",
                        index,
                    )
                if dst_label is not None and dst_label != exp_dst:
                    yield Rejection(
                        Reason.EDGE_ENDPOINT_TYPE_INVALID,
                        f"{op.rel_type} target {op.dst_id!r} is {dst_label}, expected {exp_dst}",
                        index,
                    )
        elif isinstance(op, UpsertNode):
            record = view.node(op.node_id)
            if record is None:
                continue  # genuine create — nothing to check against

            for field, proposed in op.fields.items():
                committed = record.fields.get(field, UNSET)
                if committed is UNSET:
                    yield Rejection(
                        Reason.UPSERT_FIELD_CONFLICT,
                        f"UpsertNode on {op.node_id!r} field {field!r}: "
                        f"committed=<absent>, proposed={proposed!r}",
                        index,
                    )
                elif committed != proposed:
                    yield Rejection(
                        Reason.UPSERT_FIELD_CONFLICT,
                        f"UpsertNode on {op.node_id!r} field {field!r}: "
                        f"committed={committed!r}, proposed={proposed!r}",
                        index,
                    )

        elif isinstance(op, RemoveEdge):
            # MemoryView drops an unknown removal silently and MORK reports OK
            # whether or not its exact-byte match found anything, so neither
            # backend can raise this later. It has to be caught here or not at
            # all — and an uncaught one leaves the edge live in MORK while the
            # journal says it is gone.
            if op.edge_id in removed_edges:
                yield Rejection(
                    Reason.UNKNOWN_EDGE,
                    f"edge {op.edge_id!r} is already removed earlier in this proposal",
                    index,
                )
                continue
            removed_edges.add(op.edge_id)

            actual_rel = get_rel(op.edge_id)
            if actual_rel is None:
                yield Rejection(
                    Reason.UNKNOWN_EDGE,
                    f"RemoveEdge targets unknown edge {op.edge_id!r}",
                    index,
                )
            elif actual_rel != op.rel_type:
                # The removal would be projected as `(... op.rel_type ...)`,
                # which matches no atom, so the real edge would survive.
                yield Rejection(
                    Reason.UNKNOWN_EDGE,
                    f"RemoveEdge names rel {op.rel_type!r} but edge "
                    f"{op.edge_id!r} is {actual_rel!r}",
                    index,
                )


def check_prior_values(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A non-annotation SetField's prior must match the committed state exactly."""
    for index, op in enumerate(proposal.ops):
        if not isinstance(op, SetField) or op.prior is UNSET:
            continue
            
        record = view.node(op.node_id)
        if record is None:
            continue  # Caught by check_references
            
        current = record.fields.get(op.field)
        if current != op.prior:
            yield Rejection(
                Reason.PRIOR_VALUE_MISMATCH,
                f"{op.field} on {op.node_id!r} is {current!r}, but proposal expected {op.prior!r}",
                index,
            )


def check_status_transitions(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """Status changes must be valid according to the transitions table."""
    for index, op in enumerate(proposal.ops):
        if not isinstance(op, SetField):
            continue
            
        table = STATUS_TRANSITIONS.get((op.label, op.field))
        if table is None:
            continue
            
        record = view.node(op.node_id)
        if record is None:
            continue
            
        current = record.fields.get(op.field)
        if current is None:
            continue  # Schema enforcement issue, not a transition issue
            
        allowed = table.get(current, frozenset())
        if op.value not in allowed:
            yield Rejection(
                Reason.ILLEGAL_STATUS_TRANSITION,
                f"cannot transition {op.label}.{op.field} from {current!r} to {op.value!r}",
                index,
            )


def check_immutability(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """Immutable fields can be set on creation, but never modified via SetField."""
    for index, op in enumerate(proposal.ops):
        if isinstance(op, SetField):
            immutable_for_label = IMMUTABLE_FIELDS.get(op.label, frozenset())
            if op.field in immutable_for_label:
                yield Rejection(
                    Reason.IMMUTABLE_FIELD_OVERWRITE,
                    f"{op.field} on {op.label} is immutable and cannot be updated",
                    index,
                )


def check_stagnation_obstruction(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A run cannot be stagnated without logging an obstruction."""
    for index, op in enumerate(proposal.ops):
        if (
            isinstance(op, SetField) 
            and op.label == "FormalRun" 
            and op.field == "status" 
            and op.value == RunDisposition.STAGNATED.value
        ):
            # Did this proposal include the edge?
            has_edge_in_proposal = any(
                isinstance(other, AddEdge) 
                and other.rel_type == "RAISED_OBSTRUCTION" 
                and other.src_id == op.node_id
                for other in proposal.ops
            )
            if has_edge_in_proposal:
                continue
                
            # Does the graph already have the edge?
            if view.node(op.node_id) is not None:
                if len(view.edges_from(op.node_id, "RAISED_OBSTRUCTION")) > 0:
                    continue
                    
            yield Rejection(
                Reason.STAGNATION_WITHOUT_OBSTRUCTION,
                f"FormalRun {op.node_id!r} marked stagnated without a RAISED_OBSTRUCTION edge",
                index,
            )

# -- Proof soundness gates ---

def check_replay_evidence(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A claim cannot become lean-verified on a self-report.

    Promotion requires an independently checkable replay: the claim's
    certificate must have been replayed, the replay must have passed, and
    the replay must not have silently accepted a `sorry`.
    """
    for index, op in enumerate(proposal.ops):
        if not (
            isinstance(op, SetField)
            and op.label == "Claim"
            and op.field == "status"
            and op.value == ClaimStatus.LEAN_VERIFIED.value
        ):
            continue

        if not _has_verified_sorry_free_replay(op.node_id, proposal, view):
            yield Rejection(
                Reason.PROMOTION_WITHOUT_REPLAY,
                f"Claim {op.node_id!r} promoted to lean-verified without "
                "a verified, sorry-free replay",
                index,
            )


def check_self_certification(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A replay actor cannot be the same actor who produced the certificate.
 
    A `LeanReplay` is only independent evidence if someone other than the
    certificate's own producer ran it. 
    The trigger is the `REPLAYED_BY` edge
    itself, not the `LeanReplay` upsert: a replay can be created in one
    proposal and linked to its certificate in a later one, and the gate
    must still catch a self-certified pairing at the point the link is
    actually made.
    """
    for index, op in enumerate(proposal.ops):
        if not (isinstance(op, AddEdge) and op.rel_type == "REPLAYED_BY"):
            continue
 
        certificate_id = op.src_id
        replay_id = op.dst_id
        replay_actor = _node_fields(replay_id, proposal, view).get("actor")
        certificate_actor = _node_fields(certificate_id, proposal, view).get("actor")
        if (
            replay_actor is not None
            and certificate_actor is not None
            and replay_actor == certificate_actor
        ):
            yield Rejection(
                Reason.SELF_CERTIFICATION,
                f"LeanReplay {replay_id!r} actor {replay_actor!r} matches "
                f"producing Certificate {certificate_id!r}; self-certified "
                "replay is rejected",
                index,
            )


def check_alignment_gate(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A claim cannot become lean-verified without a reviewed, aligned check.

    An independently reviewed `Alignment` record confirms the formal
    declaration actually says what the informal claim says.
    """
    for index, op in enumerate(proposal.ops):
        if not (
            isinstance(op, SetField)
            and op.label == "Claim"
            and op.field == "status"
            and op.value == ClaimStatus.LEAN_VERIFIED.value
        ):
            continue

        if not _has_reviewed_aligned_alignment(op.node_id, proposal, view):
            yield Rejection(
                Reason.PROMOTION_WITHOUT_ALIGNMENT,
                f"Claim {op.node_id!r} promoted to lean-verified without "
                "a reviewed, aligned Alignment record",
                index,
            )


 
def _node_fields(node_id: str, proposal: Proposal, view: ReadView) -> Mapping[str, Any]:
    """Fields for a node, preferring committed state over the proposal.
 
    `UpsertNode` creates a node or confirms an existing one -- it is never a
    mutation. `apply_ops` drops a re-upsert of an already-committed node, so
    a proposal cannot lie about a node's fields by re-upserting it with
    different values. Only a genuinely new node (absent from `view`) may
    take its fields from this proposal's own `UpsertNode`.
    """
    record = view.node(node_id)
    if record is not None:
        return record.fields
    for op in proposal.ops:
        if isinstance(op, UpsertNode) and op.node_id == node_id:
            return op.fields
    return {}


def _edge_targets(
    src_id: str, rel_type: str, proposal: Proposal, view: ReadView
) -> list[str]:
    """Destination ids of `rel_type` edges leaving `src_id` -- proposal + committed."""
    targets = [
        op.dst_id
        for op in proposal.ops
        if isinstance(op, AddEdge) and op.rel_type == rel_type and op.src_id == src_id
    ]
    targets.extend(e.dst_id for e in view.edges_from(src_id, rel_type))
    return targets


def _edge_sources(
    dst_id: str, rel_type: str, proposal: Proposal, view: ReadView
) -> list[str]:
    """Source ids of `rel_type` edges entering `dst_id` -- proposal + committed."""
    sources = [
        op.src_id
        for op in proposal.ops
        if isinstance(op, AddEdge) and op.rel_type == rel_type and op.dst_id == dst_id
    ]
    sources.extend(e.src_id for e in view.edges_to(dst_id, rel_type))
    return sources


def _has_verified_sorry_free_replay(
    claim_id: str, proposal: Proposal, view: ReadView
) -> bool:
    for certificate_id in _edge_targets(claim_id, "PROVED_BY", proposal, view):
        certificate_actor = _node_fields(certificate_id, proposal, view).get("actor")
        for replay_id in _edge_targets(certificate_id, "REPLAYED_BY", proposal, view):
            fields = _node_fields(replay_id, proposal, view)
            replay_actor = fields.get("actor")
            if (
                fields.get("status") == ReplayStatus.VERIFIED.value
                and fields.get("sorry_detected") is False
                and replay_actor is not None
                and certificate_actor is not None
                and replay_actor != certificate_actor
            ):
                return True
    return False


def _has_reviewed_aligned_alignment(
    claim_id: str, proposal: Proposal, view: ReadView
) -> bool:
    for alignment_id in _edge_sources(claim_id, "ALIGNS_CLAIM", proposal, view):
        fields = _node_fields(alignment_id, proposal, view)
        if (
            fields.get("lifecycle") == AlignmentLifecycle.REVIEWED.value
            and fields.get("verdict") == AlignmentVerdict.ALIGNED.value
        ):
            return True
    return False
