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
    NON_KERNEL_TACTICS,
    ObstructionKind,
    ReplayStatus,
    ResearchMoveStatus,
    ResearchStateStatus,
    RunDisposition,
    TacticStatus,
    WorkerClass,
)

__all__ = [
    "validate_proposal",
    "check_concurrency_tokens",
    "check_vocabulary",
    "check_namespace",
    "check_worker_authority",
    "check_subgoal_conservation",
    "check_executor_result",
    "check_annotation_separation",
    "check_references",
    "check_prior_values",
    "check_status_transitions",
    "check_immutability",
    "check_stagnation_obstruction",
    "check_critic_gating",
    "check_claim_replay_evidence",
    "check_claim_alignment",
    "check_environment_binding",
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
    ("ResearchState", "status"): ResearchStateStatus,
    ("ResearchMove", "status"): ResearchMoveStatus,
}
"""Fields whose values must come from a closed vocabulary."""

CLOSED_STATE_VALUES = frozenset(
    {FormalStateStatus.FORMALLY_CLOSED.value, FormalStateStatus.LEAN_VERIFIED.value}
)
TERMINAL_FAILURE_VALUES = frozenset(m.value for m in TERMINAL_EXECUTOR_FAILURES)

CLAIM_PROMOTION_TARGETS = frozenset(
    {
        ClaimStatus.CRITIC_ACCEPTED.value,
        ClaimStatus.FORMALLY_CLOSED.value,
        ClaimStatus.LEAN_VERIFIED.value,
    }
)
"""Claim statuses that assert the claim is established.

Reaching any of them is a promotion: it publishes the claim as usable
knowledge, so reviewed, aligned statement agreement is required first.
"""

FAVORABLE_CRITIC_VERDICTS = frozenset(
    {AttemptStatus.SUPPORTED.value, AttemptStatus.CRITIC_ACCEPTED.value}
)
"""Critic attempt outcomes that can back a `provisional -> critic-accepted`
promotion. A pending or refuted critique is not a verdict."""

TRUSTED_WORKER_CLASSES = frozenset(
    {
        WorkerClass.COORDINATOR.value,
        WorkerClass.MAINTENANCE.value,
        WorkerClass.HUMAN.value,
    }
)
"""Actors that may write any label: they operate the store, not one proof."""

WORKER_CLASS_AUTHORITY: dict[str, frozenset[str]] = {
    WorkerClass.FORMAL_ATP.value: frozenset(
        {
            "FormalState",
            "TacticApplication",
            "FormalRun",
            "FormalCheckpoint",
            "Certificate",
            "Obstruction",
            "Environment",
        }
    ),
    WorkerClass.REPLAYER.value: frozenset({"LeanReplay", "Certificate"}),
    WorkerClass.LLM_RESEARCH.value: frozenset(
        {"Claim", "SpeculativeHypothesis", "ResearchState", "ResearchMove"}
    ),
    WorkerClass.CRITIC.value: frozenset({"Attempt", "Critique", "Claim"}),
    WorkerClass.ALIGNMENT_REVIEWER.value: frozenset({"Alignment", "Artifact", "Claim"}),
    WorkerClass.HYPERON.value: frozenset(
        {"Claim", "SpeculativeHypothesis", "ResearchState", "ResearchMove"}
    ),
    WorkerClass.EXPERIMENT.value: frozenset({"Experiment"}),
}
"""The atom labels each worker class may create or overwrite.

Issuing multi-class leases is unsafe without this: an explorer must not close
formal states, and a critic must not invent declarations. A worker_class
outside this table (and outside `TRUSTED_WORKER_CLASSES`) is unmanaged -- the
scheduler never issues it a lease, so its proposals are not policed here.
"""

UNIVERSAL_WORKER_AUTHORITY = frozenset({"Attempt"})
"""Provenance every worker journals about its own work: any schedulable
class may create the Attempt that closes its result."""


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
    "PROPOSES": ("ResearchState", "ResearchMove"),
    "MOVE_TARGETS": ("ResearchMove", "Claim"),
    "REVIEWS_CLAIM": ("Attempt", "Claim"),
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
    findings.extend(check_worker_authority(proposal))
    findings.extend(check_subgoal_conservation(proposal))
    findings.extend(check_executor_result(proposal))
    findings.extend(check_annotation_separation(proposal))

    if view is not None:
        findings.extend(check_references(proposal, view))
        findings.extend(check_prior_values(proposal, view))
        findings.extend(check_status_transitions(proposal, view))
        findings.extend(check_immutability(proposal, view))
        findings.extend(check_stagnation_obstruction(proposal, view))
        findings.extend(check_critic_gating(proposal, view))
        findings.extend(check_claim_replay_evidence(proposal, view))
        findings.extend(check_claim_alignment(proposal, view))
        findings.extend(check_environment_binding(proposal, view))

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


def check_worker_authority(proposal: Proposal) -> Iterator[Rejection]:
    """A worker class writes only the atom types it has authority over.

    Edge ops are not checked here -- their endpoints are label-checked
    elsewhere, and creating an edge between two existing nodes changes no
    atom's type.
    """
    worker_class = proposal.worker_class
    authority = WORKER_CLASS_AUTHORITY.get(worker_class)
    if authority is None or worker_class in TRUSTED_WORKER_CLASSES:
        return
    authority = authority | UNIVERSAL_WORKER_AUTHORITY

    for index, op in enumerate(proposal.ops):
        if isinstance(op, (UpsertNode, SetField)) and op.label not in authority:
            yield Rejection(
                Reason.WORKER_CLASS_OUT_OF_AUTHORITY,
                f"worker class {worker_class!r} has no authority over "
                f"{op.label} nodes (authority: {sorted(authority)})",
                index,
            )


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

        if str(fields.get("tactic_label", "")) in NON_KERNEL_TACTICS and (
            result == ExecutorResult.LEAN_ACCEPTED.value
            or fields.get("status") == TacticStatus.CLOSED.value
            or tactic_id in closures
        ):
            # C6: kernel evidence cannot be claimed by relabelling. The
            # executor_result field is exactly what an untrusted producer
            # controls, so the tactic label decides on its own.
            yield Rejection(
                Reason.NON_KERNEL_CLOSURE,
                f"tactic {tactic_id!r} carries non-kernel label "
                f"{fields.get('tactic_label')!r} and cannot claim kernel "
                "acceptance or close a state",
                index,
            )

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


def check_critic_gating(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """`provisional -> critic-accepted` requires critic-verdict evidence.

    The promotion must carry, in the same proposal or already committed, a
    `REVIEWS_CLAIM` edge from an `Attempt` whose worker_class is `critic` and
    whose status is a favorable verdict. A pending or refuted critique cannot
    promote; the scheduler's frontier filters on the promoted status, so a
    claim could otherwise reach it on its own say-so.
    """
    promotions: list[tuple[int, str]] = [
        (index, op.node_id)
        for index, op in enumerate(proposal.ops)
        if isinstance(op, SetField)
        and op.label == "Claim"
        and op.field == "status"
        and op.value == ClaimStatus.CRITIC_ACCEPTED.value
    ]
    if not promotions:
        return

    proposed_attempts: dict[str, tuple[str | None, str | None]] = {}
    proposed_reviews: dict[str, set[str]] = defaultdict(set)
    for op in proposal.ops:
        if isinstance(op, UpsertNode) and op.label == "Attempt":
            fields = dict(op.fields or {})
            proposed_attempts[op.node_id] = (
                fields.get("worker_class"),
                fields.get("status"),
            )
        elif isinstance(op, AddEdge) and op.rel_type == "REVIEWS_CLAIM":
            proposed_reviews[op.dst_id].add(op.src_id)

    for index, claim_id in promotions:
        verdicts: list[tuple[str | None, str | None]] = []
        # Verdicts proposed alongside the promotion...
        for attempt_id in proposed_reviews.get(claim_id, ()):
            if attempt_id in proposed_attempts:
                verdicts.append(proposed_attempts[attempt_id])
        # ...and verdicts already committed against this claim.
        for edge in view.edges_to(claim_id, "REVIEWS_CLAIM"):
            record = view.node(edge.src_id)
            if record is not None and record.label == "Attempt":
                fields = dict(record.fields)
                verdicts.append(
                    (fields.get("worker_class"), fields.get("status"))
                )

        if any(
            worker_class == WorkerClass.CRITIC.value
            and status in FAVORABLE_CRITIC_VERDICTS
            for worker_class, status in verdicts
        ):
            continue

        yield Rejection(
            Reason.CRITIC_VERDICT_REQUIRED,
            f"Claim {claim_id!r} cannot be promoted to critic-accepted "
            "without a favorable critic verdict (a REVIEWS_CLAIM edge from "
            "an accepted critic Attempt)",
            index,
        )


def _created_fields(proposal: Proposal) -> dict[str, Any]:
    """Fields of every node this proposal creates, by id."""
    return {
        op.node_id: dict(op.fields) for op in proposal.ops if isinstance(op, UpsertNode)
    }


def _proposed_edges(proposal: Proposal, rel_type: str) -> list[tuple[str, str]]:
    """(src, dst) of every `rel_type` edge this proposal adds."""
    return [
        (op.src_id, op.dst_id)
        for op in proposal.ops
        if isinstance(op, AddEdge) and op.rel_type == rel_type
    ]


def _fields_of(
    node_id: str, created: dict[str, Any], view: ReadView
) -> Mapping[str, Any] | None:
    """A node's fields whether it is created here or already committed."""
    if node_id in created:
        return created[node_id]
    record = view.node(node_id)
    return record.fields if record is not None else None


def check_claim_replay_evidence(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A claim reaches lean-verified only over an independent verified replay.

    The promotion must be backed by a full evidence chain — PROVED_BY to a
    certificate whose REPLAYED_BY edge names a LeanReplay reporting
    status=verified with sorry_detected=false. A replay that does not state
    the flag is not evidence. A replay run by the certificate's producer or
    by this proposal's actor is self-certification: it never counts as
    evidence, and is reported on its own so the worker sees why.

    The finding itself refuses the proposal, so a self-certified replay
    poisons every promotion that can still see it — even one carrying other
    valid replays. Remediation is a retraction of the offending REPLAYED_BY
    edge in its own committed event; the chain then walks clean.
    """
    created = _created_fields(proposal)
    proved = _proposed_edges(proposal, "PROVED_BY")
    replayed = _proposed_edges(proposal, "REPLAYED_BY")

    for index, op in enumerate(proposal.ops):
        if not (
            isinstance(op, SetField)
            and op.label == "Claim"
            and op.field == "status"
            and op.value == ClaimStatus.LEAN_VERIFIED.value
        ):
            continue

        claim_id = op.node_id
        cert_ids = [edge.dst_id for edge in view.edges_from(claim_id, "PROVED_BY")]
        cert_ids += [dst for src, dst in proved if src == claim_id]

        verified = False
        seen_replays: set[str] = set()
        for cert_id in cert_ids:
            cert_fields = _fields_of(cert_id, created, view)
            producer_actor = cert_fields.get("actor") if cert_fields else None

            replay_ids = [
                edge.dst_id for edge in view.edges_from(cert_id, "REPLAYED_BY")
            ]
            replay_ids += [dst for src, dst in replayed if src == cert_id]
            for replay_id in replay_ids:
                if replay_id in seen_replays:
                    continue
                seen_replays.add(replay_id)

                fields = _fields_of(replay_id, created, view)
                if fields is None:
                    continue
                actor = fields.get("actor")
                if actor is not None and actor in (producer_actor, proposal.actor):
                    yield Rejection(
                        Reason.SELF_CERTIFICATION,
                        f"replay {replay_id!r} was run by {actor!r}, who also "
                        f"produced the certificate or submits this proposal",
                        index,
                    )
                    continue
                if (
                    fields.get("status") == ReplayStatus.VERIFIED.value
                    and fields.get("sorry_detected") is False
                ):
                    verified = True

        if not verified:
            yield Rejection(
                Reason.PROMOTION_WITHOUT_REPLAY,
                f"claim {claim_id!r} has no independent replay with "
                "status=verified and sorry_detected=false",
                index,
            )


def check_claim_alignment(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """An upward claim promotion requires reviewed, aligned statement agreement.

    critic-accepted, formally-closed and lean-verified all publish the claim
    as established, so an ALIGNS_CLAIM edge must reach an Alignment whose
    lifecycle is reviewed and whose verdict is aligned. Draft, unreviewed,
    superseded or disagreeing alignments do not qualify.
    """
    created = _created_fields(proposal)
    aligns = _proposed_edges(proposal, "ALIGNS_CLAIM")

    for index, op in enumerate(proposal.ops):
        if not (
            isinstance(op, SetField)
            and op.label == "Claim"
            and op.field == "status"
            and op.value in CLAIM_PROMOTION_TARGETS
        ):
            continue

        claim_id = op.node_id
        alignment_ids = [
            edge.src_id for edge in view.edges_to(claim_id, "ALIGNS_CLAIM")
        ]
        alignment_ids += [src for src, dst in aligns if dst == claim_id]

        aligned = any(
            (fields := _fields_of(alignment_id, created, view)) is not None
            and fields.get("lifecycle") == AlignmentLifecycle.REVIEWED.value
            and fields.get("verdict") == AlignmentVerdict.ALIGNED.value
            for alignment_id in set(alignment_ids)
        )
        if not aligned:
            yield Rejection(
                Reason.PROMOTION_WITHOUT_ALIGNMENT,
                f"claim {claim_id!r} promotes to {op.value!r} without an "
                "alignment that is reviewed and aligned",
                index,
            )


CERTIFICATE_VALID_STATUSES = frozenset(
    {CertificateStatus.CANDIDATE.value, CertificateStatus.REPLAY_ACCEPTED.value}
)
"""Certificate statuses that assert the artifact is usable evidence.

Committing a certificate under one of them binds it to the declaration's
pinned toolchain; under any other environment hash it may only enter the
graph as `stale`.
"""


def check_environment_binding(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A certificate is only valid under its declaration's pinned environment.

    C4: after a mathlib bump, a re-run produces a different
    environment_hash. That second certificate is not silently accepted as
    fresh evidence -- it may only be committed as `stale`. The binding is
    read through the run that produced the certificate (PRODUCED_CERTIFICATE,
    then SEARCHES to the declaration, then PINNED_ENVIRONMENT to an
    Environment); where no pin exists there is nothing to enforce and the
    check stays silent.
    """
    created = _created_fields(proposal)
    produced = _proposed_edges(proposal, "PRODUCED_CERTIFICATE")
    searches = _proposed_edges(proposal, "SEARCHES")
    pinned = _proposed_edges(proposal, "PINNED_ENVIRONMENT")

    def committed_env_hash(env_id: str) -> Any:
        fields = _fields_of(env_id, created, view)
        return fields.get("environment_hash") if fields else None

    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode) and op.label == "Certificate":
            cert_id, status = op.node_id, op.fields.get("status")
            env_hash = op.fields.get("environment_hash")
        elif (
            isinstance(op, SetField)
            and op.label == "Certificate"
            and op.field == "status"
        ):
            cert_id = op.node_id
            record = view.node(cert_id)
            if record is None:
                continue
            status = op.value
            env_hash = record.fields.get("environment_hash")
        else:
            continue

        if status not in CERTIFICATE_VALID_STATUSES or not env_hash:
            continue

        run_ids = [edge.src_id for edge in view.edges_to(cert_id, "PRODUCED_CERTIFICATE")]
        run_ids += [src for src, dst in produced if dst == cert_id]

        pinned_hashes: set[Any] = set()
        for run_id in run_ids:
            decl_ids = [edge.dst_id for edge in view.edges_from(run_id, "SEARCHES")]
            decl_ids += [dst for src, dst in searches if src == run_id]
            for decl_id in decl_ids:
                env_ids = [
                    edge.dst_id for edge in view.edges_from(decl_id, "PINNED_ENVIRONMENT")
                ]
                env_ids += [dst for src, dst in pinned if src == decl_id]
                for env_id in env_ids:
                    h = committed_env_hash(env_id)
                    if h is not None:
                        pinned_hashes.add(h)

        if pinned_hashes and env_hash not in pinned_hashes:
            yield Rejection(
                Reason.ENVIRONMENT_DRIFT,
                f"certificate {cert_id!r} was produced under {env_hash!r} but "
                f"its declaration pins {sorted(map(str, pinned_hashes))}; "
                "commit it as stale instead",
                index,
            )
