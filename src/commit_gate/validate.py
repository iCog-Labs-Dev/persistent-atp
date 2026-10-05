"""Validators for a proposed change.

Most checks are decidable from the proposal alone: they read no committed
state, run before any lock is taken, and need no graph backend. Checks that do
need committed state take a `ReadView` and are skipped when none is supplied.

Each validator yields one `Rejection` per violation; `validate_proposal` runs
all of them and returns every finding, so a worker gets a complete diagnosis
rather than the first failure.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterator, Mapping

from .ops import UNSET, AddEdge, Op, RemoveEdge, SetField, UpsertNode
from .proposal import Proposal
from .reasons import Reason, Rejection
from .state import EdgeRecord, NodeRecord, ReadView
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
formal states, and a critic must not invent declarations. Every non-trusted
worker class must be present in this table; unknown classes have no authority.
"""

UNIVERSAL_WORKER_AUTHORITY = frozenset({"Attempt"})
"""Provenance every worker journals about its own work: any schedulable
class may create the Attempt that closes its result."""

PROVENANCE_ACTOR_LABELS = frozenset(
    {"Certificate", "LeanReplay", "Alignment", "Attempt"}
)
"""Evidence records whose stated actor must be the leased worker."""


UNSCOPED_LABELS = frozenset({"Artifact"})
"""Labels that are content-addressed and therefore carry no proof scope."""

WORKER_CLASS_EDGE_AUTHORITY: dict[str, frozenset[str]] = {
    WorkerClass.FORMAL_ATP.value: frozenset(
        {
            "HAS_TACTIC",
            "FORMAL_REQUIRES",
            "CLOSES_STATE",
            "HAS_ROOT",
            "HAS_CHECKPOINT",
            "CHECKPOINT_FRONTIER",
            "RAN_UNDER",
            "SEARCHES",
            "PRODUCED_CERTIFICATE",
            "CERTIFIES",
            "CERTIFICATE_ENVIRONMENT",
            "PROVED_BY",
            "RAISED_OBSTRUCTION",
            "AT_STATE",
            "HAS_TARGET",
            "HAS_ARTIFACT",
        }
    ),
    WorkerClass.REPLAYER.value: frozenset({"REPLAYED_BY", "REPLAY_ENVIRONMENT"}),
    WorkerClass.LLM_RESEARCH.value: frozenset(
        {"DEPENDS_ON", "PROMOTED_TO", "RESOLVES", "PROPOSES", "MOVE_TARGETS"}
    ),
    WorkerClass.HYPERON.value: frozenset(
        {"DEPENDS_ON", "PROMOTED_TO", "RESOLVES", "PROPOSES", "MOVE_TARGETS"}
    ),
    WorkerClass.CRITIC.value: frozenset({"REVIEWS_CLAIM"}),
    WorkerClass.ALIGNMENT_REVIEWER.value: frozenset(
        {"ALIGNS_CLAIM", "ALIGNS_DECLARATION"}
    ),
    WorkerClass.EXPERIMENT.value: frozenset(),
}
"""The relationships each worker class may assert or remove.

Relationship authority is separate from endpoint validation: compatible node
labels do not establish that a worker may make the assertion.
"""

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
    compare the head against, and stale work commits silently. Every mutation
    needs a lease so the gate can bind the proposer to its assigned worker
    class and reject stale fencing tokens.
    """
    if proposal.base_revision is None:
        yield Rejection(
            Reason.MISSING_CONCURRENCY_TOKEN,
            "proposal names no base_revision; it cannot be checked against the journal",
        )

    if proposal.lease_id is not None and proposal.fencing_token is not None:
        return
    if proposal.ops:
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
            f"mutation requires the write lease; missing {missing}",
            0,
        )


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

    Edge endpoint types are validated separately; this check also limits who
    may assert or remove each relationship.
    """
    worker_class = proposal.worker_class
    if worker_class in TRUSTED_WORKER_CLASSES:
        return

    authority = WORKER_CLASS_AUTHORITY.get(worker_class)
    if authority is None:
        yield Rejection(
            Reason.WORKER_CLASS_OUT_OF_AUTHORITY,
            f"unknown worker class {worker_class!r} has no assigned authority",
        )
        return
    authority = authority | UNIVERSAL_WORKER_AUTHORITY
    edge_authority = WORKER_CLASS_EDGE_AUTHORITY.get(worker_class, frozenset())

    for index, op in enumerate(proposal.ops):
        if (
            isinstance(op, UpsertNode)
            and op.label in PROVENANCE_ACTOR_LABELS
            and "actor" in op.fields
            and op.fields["actor"] != proposal.actor
        ):
            yield Rejection(
                Reason.PROVENANCE_ACTOR_MISMATCH,
                f"{op.label} {op.node_id!r} names actor {op.fields['actor']!r}, "
                f"but the leased proposer is {proposal.actor!r}",
                index,
            )
        if isinstance(op, (UpsertNode, SetField)) and op.label not in authority:
            yield Rejection(
                Reason.WORKER_CLASS_OUT_OF_AUTHORITY,
                f"worker class {worker_class!r} has no authority over "
                f"{op.label} nodes (authority: {sorted(authority)})",
                index,
            )
        elif isinstance(op, (AddEdge, RemoveEdge)) and op.rel_type not in edge_authority:
            yield Rejection(
                Reason.WORKER_CLASS_OUT_OF_AUTHORITY,
                f"worker class {worker_class!r} has no authority over "
                f"{op.rel_type!r} relationships "
                f"(authority: {sorted(edge_authority)})",
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
    current_nodes: dict[str, NodeRecord] = {}
    current_edges: dict[str, EdgeRecord | None] = {}

    def get_node(node_id: str) -> NodeRecord | None:
        return current_nodes.get(node_id) or view.node(node_id)

    def get_label(node_id: str) -> str | None:
        record = get_node(node_id)
        return record.label if record else None

    for index, op in enumerate(proposal.ops):
        if isinstance(op, SetField):
            record = get_node(op.node_id)
            if record is None:
                yield Rejection(
                    Reason.UNKNOWN_NODE,
                    f"SetField targets unknown node {op.node_id!r}",
                    index,
                )
            elif record.label != op.label:
                yield Rejection(
                    Reason.NODE_ALREADY_EXISTS_WITH_LABEL,
                    f"SetField label {op.label!r} does not match node {op.node_id!r}",
                    index,
                )
            else:
                current_nodes[op.node_id] = NodeRecord(
                    op.node_id, op.label, {**record.fields, op.field: op.value}
                )
        elif isinstance(op, AddEdge):
            existing = current_edges.get(op.edge_id, view.edge(op.edge_id))
            proposed = EdgeRecord(
                op.edge_id, op.rel_type, op.src_id, op.dst_id, dict(op.fields or {})
            )
            if existing is not None and existing != proposed:
                yield Rejection(
                    Reason.EDGE_ID_CONFLICT,
                    f"edge id {op.edge_id!r} already identifies a different edge",
                    index,
                )
            elif existing is None:
                current_edges[op.edge_id] = proposed
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
            record = get_node(op.node_id)
            if record is None:
                current_nodes[op.node_id] = NodeRecord(
                    op.node_id, op.label, dict(op.fields)
                )
                continue
            if record.label != op.label:
                yield Rejection(
                    Reason.NODE_ALREADY_EXISTS_WITH_LABEL,
                    f"UpsertNode label {op.label!r} does not match node "
                    f"{op.node_id!r}, which is {record.label!r}",
                    index,
                )
                continue

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
            existing = current_edges.get(op.edge_id, view.edge(op.edge_id))
            actual_rel = existing.rel_type if existing else None
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
            else:
                current_edges[op.edge_id] = None


def check_prior_values(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A SetField prior must match the state at that point in the proposal."""
    for index, op, record in _field_writes_with_prior_state(proposal, view):
        if not isinstance(op, SetField) or op.prior is UNSET:
            continue
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
    for index, op, record in _field_writes_with_prior_state(proposal, view):
        table = STATUS_TRANSITIONS.get((op.label, op.field))
        if table is None:
            continue
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


def _field_writes_with_prior_state(
    proposal: Proposal, view: ReadView
) -> Iterator[tuple[int, SetField, NodeRecord | None]]:
    """Walk field writes over the same node order that apply_ops uses."""
    state: dict[str, NodeRecord] = {}
    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode):
            record = state.get(op.node_id) or view.node(op.node_id)
            if record is None:
                state[op.node_id] = NodeRecord(op.node_id, op.label, dict(op.fields))
        elif isinstance(op, SetField):
            record = state.get(op.node_id) or view.node(op.node_id)
            yield index, op, record
            if record is not None:
                state[op.node_id] = NodeRecord(
                    op.node_id, record.label, {**record.fields, op.field: op.value}
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
    promotions = [
        (index, claim_id)
        for index, claim_id, _ in _claim_promotions(
            proposal, {ClaimStatus.CRITIC_ACCEPTED.value}
        )
    ]
    created = _created_fields(proposal, view)
    promoted = {claim_id for _, claim_id in promotions}
    promotions.extend(
        (index, claim_id)
        for claim_id, index in _affected_claims(proposal, view).items()
        if claim_id not in promoted
        and (_fields_after(claim_id, created, proposal, view) or {}).get("status")
        == ClaimStatus.CRITIC_ACCEPTED.value
    )
    for index, claim_id in promotions:
        if any(
            fields.get("worker_class") == WorkerClass.CRITIC.value
            and fields.get("status") in FAVORABLE_CRITIC_VERDICTS
            for attempt_id in _edge_sources_after(
                proposal, view, claim_id, "REVIEWS_CLAIM"
            )
            if (fields := _fields_after(attempt_id, created, proposal, view)) is not None
        ):
            continue

        yield Rejection(
            Reason.CRITIC_VERDICT_REQUIRED,
            f"Claim {claim_id!r} cannot be promoted to critic-accepted "
            "without a favorable critic verdict (a REVIEWS_CLAIM edge from "
            "an accepted critic Attempt)",
            index,
        )


def _created_fields(proposal: Proposal, view: ReadView) -> dict[str, Any]:
    """Fields of genuinely new nodes, matching the first applied upsert."""
    created: dict[str, Any] = {}
    for op in proposal.ops:
        if (
            isinstance(op, UpsertNode)
            and op.node_id not in created
            and view.node(op.node_id) is None
        ):
            created[op.node_id] = dict(op.fields)
    return created


EVIDENCE_RELATIONS = frozenset(
    {
        "PROVED_BY", "REPLAYED_BY", "CERTIFIES", "PRODUCED_CERTIFICATE",
        "SEARCHES", "PINNED_ENVIRONMENT", "CERTIFICATE_ENVIRONMENT",
        "RAN_UNDER", "REPLAY_ENVIRONMENT", "ALIGNS_CLAIM",
        "ALIGNS_DECLARATION", "REVIEWS_CLAIM",
    }
)


def _affected_claims(proposal: Proposal, view: ReadView) -> dict[str, int]:
    """Find claims whose evidence can change, including paths severed by a removal."""
    seeds: list[tuple[int, str]] = []
    created = {
        op.node_id: op.label for op in proposal.ops if isinstance(op, UpsertNode)
    }
    for index, op in enumerate(proposal.ops):
        if isinstance(op, SetField) and op.label in {
            "Attempt", "Alignment", "Certificate", "LeanReplay", "FormalRun",
            "FormalDeclaration", "Environment",
        }:
            seeds.append((index, op.node_id))
        elif isinstance(op, AddEdge) and op.rel_type in EVIDENCE_RELATIONS:
            seeds.extend(((index, op.src_id), (index, op.dst_id)))
        elif isinstance(op, RemoveEdge) and op.rel_type in EVIDENCE_RELATIONS:
            edge = view.edge(op.edge_id)
            if edge is not None:
                seeds.extend(((index, edge.src_id), (index, edge.dst_id)))

    claims: dict[str, int] = {}
    visited: set[str] = set()
    queue = list(seeds)
    while queue:
        index, node_id = queue.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        record = view.node(node_id)
        label = created.get(node_id, record.label if record else None)
        if label == "Claim":
            claims[node_id] = index
            continue
        # Each step follows a possible evidence path toward a claim. Include
        # both committed and resulting edges so removals retain their owners.
        links: tuple[tuple[str, str], ...] = {
            "LeanReplay": (("in", "REPLAYED_BY"),),
            "Certificate": (("in", "PROVED_BY"),),
            "FormalRun": (("out", "PRODUCED_CERTIFICATE"),),
            "FormalDeclaration": (
                ("in", "CERTIFIES"), ("in", "ALIGNS_DECLARATION"),
                ("in", "SEARCHES"),
            ),
            "Alignment": (("out", "ALIGNS_CLAIM"),),
            "Attempt": (("out", "REVIEWS_CLAIM"),),
            "Environment": (
                ("in", "PINNED_ENVIRONMENT"),
                ("in", "CERTIFICATE_ENVIRONMENT"), ("in", "RAN_UNDER"),
                ("in", "REPLAY_ENVIRONMENT"),
            ),
        }.get(label, ())
        for direction, rel_type in links:
            old = (
                view.edges_to(node_id, rel_type)
                if direction == "in"
                else view.edges_from(node_id, rel_type)
            )
            new = _edges_after(
                proposal, view, rel_type,
                **({"dst_id": node_id} if direction == "in" else {"src_id": node_id}),
            )
            for edge in (*old, *new):
                queue.append((index, edge.src_id if direction == "in" else edge.dst_id))
    return claims


def _claim_promotions(
    proposal: Proposal, targets: frozenset[str] | set[str]
) -> Iterator[tuple[int, str, str]]:
    """Claim creations or status writes that publish a promoted status."""
    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode) and op.label == "Claim":
            status = op.fields.get("status")
            if status in targets:
                yield index, op.node_id, status
        elif (
            isinstance(op, SetField)
            and op.label == "Claim"
            and op.field == "status"
            and op.value in targets
        ):
            yield index, op.node_id, op.value


def _proposed_edges(proposal: Proposal, rel_type: str) -> list[tuple[str, str]]:
    """(src, dst) of every `rel_type` edge this proposal adds."""
    return [
        (op.src_id, op.dst_id)
        for op in proposal.ops
        if isinstance(op, AddEdge) and op.rel_type == rel_type
    ]


def _edge_targets_after(
    proposal: Proposal, view: ReadView, src_id: str, rel_type: str
) -> set[str]:
    """Targets of an edge type after this proposal's additions and removals."""
    return {
        edge.dst_id
        for edge in _edges_after(proposal, view, rel_type, src_id=src_id)
    }


def _edge_sources_after(
    proposal: Proposal, view: ReadView, dst_id: str, rel_type: str
) -> set[str]:
    """Sources of an edge type after this proposal's additions and removals."""
    return {
        edge.src_id
        for edge in _edges_after(proposal, view, rel_type, dst_id=dst_id)
    }


def _edges_after(
    proposal: Proposal,
    view: ReadView,
    rel_type: str,
    *,
    src_id: str | None = None,
    dst_id: str | None = None,
) -> tuple[EdgeRecord, ...]:
    """Project touched edges in op order, using the same ID semantics as apply_ops."""
    if src_id is not None:
        initial = view.edges_from(src_id, rel_type)
    elif dst_id is not None:
        initial = view.edges_to(dst_id, rel_type)
    else:
        raise ValueError("an edge query needs an endpoint")
    edges = {edge.edge_id: edge for edge in initial}
    touched: set[str] = set()
    for op in proposal.ops:
        if isinstance(op, (AddEdge, RemoveEdge)) and op.edge_id not in touched and op.edge_id not in edges:
            existing = view.edge(op.edge_id)
            if existing is not None:
                edges[op.edge_id] = existing
        if isinstance(op, RemoveEdge):
            edges.pop(op.edge_id, None)
            touched.add(op.edge_id)
        elif isinstance(op, AddEdge) and op.edge_id not in edges:
            edges[op.edge_id] = EdgeRecord(
                op.edge_id, op.rel_type, op.src_id, op.dst_id, dict(op.fields or {})
            )
            touched.add(op.edge_id)
    return tuple(
        edge for edge in edges.values()
        if edge.rel_type == rel_type
        and (src_id is None or edge.src_id == src_id)
        and (dst_id is None or edge.dst_id == dst_id)
    )


def _fields_of(
    node_id: str, created: dict[str, Any], view: ReadView
) -> Mapping[str, Any] | None:
    """A node's fields whether it is created here or already committed."""
    if node_id in created:
        return created[node_id]
    record = view.node(node_id)
    return record.fields if record is not None else None


def _fields_after(
    node_id: str, created: dict[str, Any], proposal: Proposal, view: ReadView
) -> Mapping[str, Any] | None:
    """Node fields after all field writes in a proposal."""
    fields = _fields_of(node_id, created, view)
    if fields is None:
        return None
    effective = dict(fields)
    for op in proposal.ops:
        if isinstance(op, SetField) and op.node_id == node_id:
            effective[op.field] = op.value
    return effective


def _reviewed_alignments(
    proposal: Proposal, view: ReadView, created: dict[str, Any], claim_id: str
) -> set[str]:
    """Alignments still accepted for this claim after the proposal."""
    accepted = set()
    for alignment_id in _edge_sources_after(proposal, view, claim_id, "ALIGNS_CLAIM"):
        fields = _fields_after(alignment_id, created, proposal, view)
        if fields is None:
            continue
        if (
            fields.get("lifecycle") == AlignmentLifecycle.REVIEWED.value
            and fields.get("verdict") == AlignmentVerdict.ALIGNED.value
        ):
            accepted.add(alignment_id)
    return accepted


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _certificate_binding_issue(
    proposal: Proposal,
    view: ReadView,
    created: dict[str, Any],
    cert_id: str,
    declaration_id: str,
) -> tuple[Reason, str] | None:
    """Validate the certificate's run, declaration and pinned environment."""
    cert = _fields_after(cert_id, created, proposal, view)
    if cert is None or not _is_sha256(cert.get("artifact_hash")):
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "missing valid artifact_hash"
    if not _is_sha256(cert.get("environment_hash")):
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "missing valid environment_hash"
    if _edge_targets_after(proposal, view, cert_id, "CERTIFIES") != {declaration_id}:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "no unique certified declaration"

    run_id = cert.get("producer_run_id")
    if not isinstance(run_id, str) or not run_id:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "missing producer_run_id"
    run = _fields_after(run_id, created, proposal, view)
    if run is None or run.get("status") != RunDisposition.PROVED_PENDING_REPLAY.value:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "missing successful producing run"
    if _edge_sources_after(proposal, view, cert_id, "PRODUCED_CERTIFICATE") != {run_id}:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "producing run is not linked to certificate"
    if _edge_targets_after(proposal, view, run_id, "SEARCHES") != {declaration_id}:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "run did not search certified declaration"

    pinned = _edge_targets_after(proposal, view, declaration_id, "PINNED_ENVIRONMENT")
    if len(pinned) != 1:
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "declaration has no unique environment pin"
    env_id = next(iter(pinned))
    environment = _fields_after(env_id, created, proposal, view)
    env_hash = environment.get("environment_hash") if environment else None
    if not _is_sha256(env_hash):
        return Reason.CERTIFICATE_BINDING_INCOMPLETE, "pinned environment has no valid hash"
    if cert.get("environment_hash") != env_hash or run.get("environment_hash") != env_hash:
        return Reason.ENVIRONMENT_DRIFT, "certificate or run environment hash differs from the pin"
    for source, relation in (
        (cert_id, "CERTIFICATE_ENVIRONMENT"),
        (run_id, "RAN_UNDER"),
    ):
        if _edge_targets_after(proposal, view, source, relation) != {env_id}:
            return Reason.CERTIFICATE_BINDING_INCOMPLETE, f"{relation} does not identify the pin"
    return None


def _promotion_environment_issue(
    proposal: Proposal,
    view: ReadView,
    created: dict[str, Any],
    cert_id: str,
    declaration_id: str,
    replay_id: str,
) -> str | None:
    """Why this exact certificate, declaration and replay lack a common pin."""
    certificate_issue = _certificate_binding_issue(
        proposal, view, created, cert_id, declaration_id
    )
    if certificate_issue is not None:
        return certificate_issue[1]

    replay = _fields_after(replay_id, created, proposal, view)
    if replay is None:
        return "replay record is missing"
    pinned = _edge_targets_after(proposal, view, declaration_id, "PINNED_ENVIRONMENT")
    env_id = next(iter(pinned))
    environment = _fields_after(env_id, created, proposal, view)
    if replay.get("environment_hash") != environment["environment_hash"]:
        return "replay environment hash differs from the pin"
    if _edge_targets_after(proposal, view, replay_id, "REPLAY_ENVIRONMENT") != {env_id}:
        return "REPLAY_ENVIRONMENT does not identify the pinned environment"
    return None


def check_claim_replay_evidence(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """A claim reaches lean-verified only over an independent verified replay.

    The promotion must be backed by a full evidence chain — PROVED_BY to a
    certificate whose REPLAYED_BY edge names a LeanReplay reporting
    status=verified with sorry_detected=false. A replay that does not state
    the flag is not evidence. A replay run by the certificate's producer or
    by this proposal's actor is self-certification: it never counts as
    evidence. It is reported when no independent replay is available; an
    unrelated self-certified replay cannot veto a complete valid path.

    A lean-verified claim also needs one declaration reached both by that
    replayed certificate's CERTIFIES edge and by a reviewed alignment's
    ALIGNS_DECLARATION edge. The certificate, its run, its replay and the
    declaration must all bind to the same pinned environment.
    """
    created = _created_fields(proposal, view)

    promotions = [
        (index, claim_id, True)
        for index, claim_id, _ in _claim_promotions(
            proposal, {ClaimStatus.LEAN_VERIFIED.value}
        )
    ]
    promoted = {claim_id for _, claim_id, _ in promotions}
    promotions.extend(
        (index, claim_id, False)
        for claim_id, index in _affected_claims(proposal, view).items()
        if claim_id not in promoted
        and (_fields_after(claim_id, created, proposal, view) or {}).get("status")
        == ClaimStatus.LEAN_VERIFIED.value
    )

    for index, claim_id, is_promotion in promotions:
        cert_ids = _edge_targets_after(proposal, view, claim_id, "PROVED_BY")

        verified_certs: dict[str, set[str]] = defaultdict(set)
        self_certifications: dict[str, str] = {}
        for cert_id in sorted(cert_ids):
            cert_fields = _fields_after(cert_id, created, proposal, view)
            producer_actor = cert_fields.get("actor") if cert_fields else None

            replay_ids = _edge_targets_after(proposal, view, cert_id, "REPLAYED_BY")
            for replay_id in sorted(replay_ids):
                fields = _fields_after(replay_id, created, proposal, view)
                if fields is None:
                    continue
                if (
                    fields.get("certificate_id") != cert_id
                    or (
                        cert_fields is not None
                        and _is_sha256(cert_fields.get("artifact_hash"))
                        and fields.get("artifact_hash") != cert_fields.get("artifact_hash")
                    )
                    or not _is_sha256(fields.get("artifact_hash"))
                    or _edge_sources_after(proposal, view, replay_id, "REPLAYED_BY")
                    != {cert_id}
                ):
                    continue
                actor = fields.get("actor")
                if actor is not None and (
                    actor == producer_actor or (is_promotion and actor == proposal.actor)
                ):
                    self_certifications[replay_id] = actor
                    continue
                if (
                    isinstance(actor, str)
                    and bool(actor)
                    and fields.get("status") == ReplayStatus.VERIFIED.value
                    and fields.get("sorry_detected") is False
                ):
                    verified_certs[cert_id].add(replay_id)

        if not verified_certs:
            for replay_id, actor in sorted(self_certifications.items()):
                yield Rejection(
                    Reason.SELF_CERTIFICATION,
                    f"replay {replay_id!r} was run by {actor!r}, who also "
                    f"produced the certificate or submits this proposal",
                    index,
                )
            yield Rejection(
                Reason.PROMOTION_WITHOUT_REPLAY,
                f"claim {claim_id!r} has no independent replay with "
                "status=verified and sorry_detected=false",
                index,
            )
            continue

        usable_certs = {
            cert_id: replay_ids
            for cert_id, replay_ids in verified_certs.items()
            if (
                (fields := _fields_after(cert_id, created, proposal, view)) is not None
                and isinstance(fields.get("actor"), str)
                and bool(fields.get("actor"))
                and fields.get("status") == CertificateStatus.REPLAY_ACCEPTED.value
                and _is_sha256(fields.get("artifact_hash"))
                and _is_sha256(fields.get("environment_hash"))
            )
        }
        if not usable_certs:
            yield Rejection(
                Reason.PROMOTION_WITHOUT_VALID_CERTIFICATE,
                f"claim {claim_id!r} has no independently replayed certificate "
                "with replay-accepted status and valid artifact/environment hashes",
                index,
            )
            continue

        aligned_declarations = {
            declaration_id
            for alignment_id in _reviewed_alignments(proposal, view, created, claim_id)
            for declaration_id in _edge_targets_after(
                proposal, view, alignment_id, "ALIGNS_DECLARATION"
            )
        }
        matching_paths: list[tuple[str, str, str]] = []
        for cert_id, replay_ids in sorted(usable_certs.items()):
            declarations = _edge_targets_after(proposal, view, cert_id, "CERTIFIES")
            if len(declarations) != 1:
                continue
            declaration_id = next(iter(declarations))
            if declaration_id in aligned_declarations:
                matching_paths.extend(
                    (cert_id, declaration_id, replay_id)
                    for replay_id in sorted(replay_ids)
                )
        if not matching_paths:
            yield Rejection(
                Reason.PROMOTION_WITHOUT_DECLARATION_CHAIN,
                f"claim {claim_id!r} has no independently replayed certificate "
                "that certifies a declaration in a reviewed, aligned "
                "alignment for this claim",
                index,
            )
            continue

        environment_issues = [
            issue
            for cert_id, declaration_id, replay_id in matching_paths
            if (issue := _promotion_environment_issue(
                proposal, view, created, cert_id, declaration_id, replay_id
            )) is not None
        ]
        if len(environment_issues) == len(matching_paths):
            yield Rejection(
                Reason.PROMOTION_WITHOUT_ENVIRONMENT_BINDING,
                f"claim {claim_id!r} has no replayed certificate bound to its "
                f"declaration's pinned environment: {environment_issues[0]}",
                index,
            )


def check_claim_alignment(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """An upward claim promotion requires reviewed, aligned statement agreement.

    critic-accepted, formally-closed and lean-verified all publish the claim
    as established, so an ALIGNS_CLAIM edge must reach an Alignment whose
    lifecycle is reviewed and whose verdict is aligned. Draft, unreviewed,
    superseded or disagreeing alignments do not qualify.
    """
    created = _created_fields(proposal, view)

    promotions = list(_claim_promotions(proposal, CLAIM_PROMOTION_TARGETS))
    promoted = {claim_id for _, claim_id, _ in promotions}
    promotions.extend(
        (index, claim_id, status)
        for claim_id, index in _affected_claims(proposal, view).items()
        if claim_id not in promoted
        if (status := (_fields_after(claim_id, created, proposal, view) or {}).get("status"))
        in CLAIM_PROMOTION_TARGETS
    )

    for index, claim_id, status in promotions:
        if not _reviewed_alignments(proposal, view, created, claim_id):
            yield Rejection(
                Reason.PROMOTION_WITHOUT_ALIGNMENT,
                f"claim {claim_id!r} promotes to {status!r} without an "
                "alignment that is reviewed and aligned",
                index,
            )


CERTIFICATE_VALID_STATUSES = frozenset(
    {CertificateStatus.CANDIDATE.value, CertificateStatus.REPLAY_ACCEPTED.value}
)
"""Certificate statuses whose pinned binding must be complete at commit.

Committing a certificate under one of them binds it to the declaration's
pinned toolchain; under any other environment hash it may only enter the
graph as `stale`.
"""


def check_environment_binding(proposal: Proposal, view: ReadView) -> Iterator[Rejection]:
    """Candidate and accepted certificates require a complete pinned binding.

    Incomplete certificates may be recorded as stale, but cannot enter a
    status that makes them available as proof evidence.
    """
    created = _created_fields(proposal, view)

    for index, op in enumerate(proposal.ops):
        if isinstance(op, UpsertNode) and op.label == "Certificate":
            cert_id, status = op.node_id, op.fields.get("status")
        elif (
            isinstance(op, SetField)
            and op.label == "Certificate"
            and op.field == "status"
        ):
            cert_id, status = op.node_id, op.value
        else:
            continue

        if status not in CERTIFICATE_VALID_STATUSES:
            continue
        declarations = _edge_targets_after(proposal, view, cert_id, "CERTIFIES")
        if len(declarations) != 1:
            yield Rejection(
                Reason.CERTIFICATE_BINDING_INCOMPLETE,
                f"certificate {cert_id!r} has no unique CERTIFIES declaration",
                index,
            )
            continue
        declaration_id = next(iter(declarations))
        issue = _certificate_binding_issue(
            proposal, view, created, cert_id, declaration_id
        )
        if issue is not None:
            reason, detail = issue
            yield Rejection(reason, f"certificate {cert_id!r}: {detail}", index)
