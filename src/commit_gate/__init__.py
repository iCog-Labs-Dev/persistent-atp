"""The Commit Gate.

Validates proposed operations against committed state, enforcing all invariant
rules (schema types, status transitions, subgoal conservation, immutability).
Accepted proposals are cryptographically chained and appended to a journal, then
projected into the graph the next proposal is validated against.

The MORK graph backend lives in ``mork.backend``; import ``MorkView`` and
``MorkSpace`` from there. See ``mork.backend.ffi`` for how the library is loaded.
"""

from .apply import apply_ops
from .canon import GENESIS_HASH, chain_hash, content_hash
from .gate import CommitGate, CommitResult, ProjectionError

from .ops import (
    UNSET,
    AddEdge,
    Op,
    OpClass,
    RemoveEdge,
    SetField,
    UpsertNode,
    op_from_dict,
    ops_from_dicts,
)
from .proposal import Proposal
from .reasons import Reason, Rejection
from .state import (
    EdgeRecord,
    GraphView,
    MemoryView,
    NodeRecord,
    ReadView,
    WriteView,
)
from .store import ConcurrencyError, HashChainError, JournalStore
from .transitions import IMMUTABLE_FIELDS, STATUS_TRANSITIONS
from .validate import validate_proposal
from .vocab import (
    ANNOTATION_FIELDS,
    TERMINAL_EXECUTOR_FAILURES,
    AlignmentLifecycle,
    AlignmentVerdict,
    CertificateStatus,
    ClaimStatus,
    DeclarationStatus,
    EvidenceKind,
    ExecutorResult,
    FormalStateStatus,
    ObstructionKind,
    ReplayStatus,
    RunDisposition,
    TacticStatus,
    WorkerClass,
)

__all__ = [
    # Gate Orchestration
    "CommitGate",
    "CommitResult",
    "ProjectionError",

    # Proposal & Ops
    "Proposal",
    "Op",
    "OpClass",
    "UpsertNode",
    "SetField",
    "AddEdge",
    "RemoveEdge",
    "UNSET",
    "op_from_dict",
    "ops_from_dicts",

    # Validation & Rejections
    "validate_proposal",
    "Rejection",
    "Reason",

    # State Contracts
    "ReadView",
    "WriteView",
    "GraphView",
    "NodeRecord",
    "EdgeRecord",
    "MemoryView",
    "apply_ops",


    # Journal
    "JournalStore",
    "ConcurrencyError",
    "HashChainError",

    # Rules & Schema
    "STATUS_TRANSITIONS",
    "IMMUTABLE_FIELDS",
    
    # Cryptography
    "content_hash",
    "chain_hash",
    "GENESIS_HASH",
    
    # Vocabulary (Enums)
    "ClaimStatus",
    "FormalStateStatus",
    "DeclarationStatus",
    "CertificateStatus",
    "AlignmentLifecycle",
    "AlignmentVerdict",
    "RunDisposition",
    "ExecutorResult",
    "TacticStatus",
    "ReplayStatus",
    "ObstructionKind",
    "EvidenceKind",
    "WorkerClass",
    "ANNOTATION_FIELDS",
    "TERMINAL_EXECUTOR_FAILURES",
]
