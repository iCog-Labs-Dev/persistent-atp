"""Formal ATP request compiler (issue #53).

``compile_formal`` turns a selected formal task and its source records into one
precise, reproducible request for the ATP service, and packages its manifest
with :func:`context_compiler.finalize.finalize_packet`.

The compilation carries: theorem kernel, exact Lean declaration, root-state
artifact, pinned environment, permitted premises, model/corpus references,
search policy, execution limits, checkpoint policy, prior-run information and
accepted transpositions.

``payload.to_schema_dict()`` is the wire request and validates against
``schemas/formal-search-request.schema.json`` (which has no field for the
kernel, premises, prior runs, transpositions or checkpoint policy).  The packet
content is an envelope ``{"request": <schema dict>, "context": <the rest>}`` so
the digest and manifest cover everything that was selected.

Two properties are load bearing:

* **Verified vs. suggested premises.**  Only premises whose status records
  Lean-checked evidence are listed as ``verified_premises``; everything else
  permitted (including ``critic-accepted`` claims) is a ``suggested_premises``
  entry.  The two are never merged.
* **Reproducibility.**  Every source is addressed by id, version and content
  hash, the request body is canonical JSON, and every bundle source receives a
  ``SelectionDecision``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from shared.vocab import (
    AttemptStatus,
    CertificateStatus,
    ClaimStatus,
    DeclarationStatus,
    EvidenceKind,
    FormalStateStatus,
    RunDisposition,
    StateKind,
    values,
)

from .contracts import (
    CompiledResult,
    CompileRequest,
    ContextPacket,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TheoremKernel,
)
from .errors import ContextValidationError
from .finalize import _json_value, finalize_packet
from .obstruction import ArtifactReader

__all__ = [
    "CheckpointPolicy",
    "CorpusManifest",
    "FormalCompilation",
    "FormalRequestPayload",
    "FormalTask",
    "ModelBundle",
    "PremiseAccessibility",
    "PremiseEntry",
    "PriorRun",
    "SOURCE_TYPES",
    "TranspositionEntry",
    "compile_formal",
]

DECLARATION_TYPE = "formal-declaration"
ENVIRONMENT_TYPE = "environment"
STATE_TYPE = "formal-state"
PREMISE_TYPE = "premise"
RUN_TYPE = "formal-run"
TRANSPOSITION_TYPE = "transposition"

SOURCE_TYPES = frozenset(
    {
        DECLARATION_TYPE,
        ENVIRONMENT_TYPE,
        STATE_TYPE,
        PREMISE_TYPE,
        RUN_TYPE,
        TRANSPOSITION_TYPE,
    }
)

SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
STATE_ID_RE = re.compile(r"^[^/]+/fs-[1-9][0-9]*$")
DECLARATION_ID_RE = re.compile(r"^[^/]+/fd-[1-9][0-9]*$")
RUN_ID_RE = re.compile(r"^[^/]+/fr-[1-9][0-9]*$")
CLAIM_ID_RE = re.compile(r"^[^/]+/c-[1-9][0-9]*$")

DEFAULT_COMPILER_VERSION = "context/1"
DEFAULT_RENDERING_VERSION = "text/1"

# Statuses recording Lean-checked evidence.  ``critic-accepted`` is deliberately
# absent: a critic's approval is not a kernel check.
_VERIFIED = frozenset(
    {
        ClaimStatus.LEAN_VERIFIED.value,
        ClaimStatus.FORMALLY_CLOSED.value,
        DeclarationStatus.REPLAY_ACCEPTED.value,
        CertificateStatus.REPLAY_ACCEPTED.value,
    }
)
_UNUSABLE = frozenset(
    {
        ClaimStatus.REFUTED.value,
        ClaimStatus.RETRACTED.value,
        ClaimStatus.TAINTED.value,
        ClaimStatus.STALE.value,
        DeclarationStatus.REPLAY_REJECTED.value,
        CertificateStatus.REPLAY_REJECTED.value,
    }
)
_PENDING = AttemptStatus.PENDING.value


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _require_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{name} must be a non-empty string")
    return value


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContextValidationError(
            f"{name} must be sha256: followed by 64 lowercase hexadecimal characters"
        )
    return value


def _require_positive(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextValidationError(f"{name} must be a positive integer")
    return value


def _str_list(name: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ContextValidationError(f"{name} must be a list of strings")
    return tuple(_require_str(name, item) for item in value)


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PremiseAccessibility:
    """ATP-supplied view of which Lean names are usable in the pinned env."""

    accessible: frozenset[str]
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accessible, frozenset):
            raise ContextValidationError("accessible must be a frozenset")
        for name in self.accessible:
            _require_str("accessible premise", name)
        if not isinstance(self.unavailable, Mapping):
            raise ContextValidationError("unavailable must be a mapping")
        for name, reason in self.unavailable.items():
            _require_str("unavailable premise", name)
            _require_str("unavailable reason", reason)


@dataclass(frozen=True)
class CheckpointPolicy:
    every_steps: int
    on_timeout: bool = True

    def __post_init__(self) -> None:
        _require_positive("every_steps", self.every_steps)
        if not isinstance(self.on_timeout, bool):
            raise ContextValidationError("on_timeout must be a boolean")


@dataclass(frozen=True)
class ModelBundle:
    """Content addresses of the heuristic models (scheduling only)."""

    tactic_model_artifact: str | None = None
    argument_model_artifact: str | None = None
    premise_model_artifact: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "tactic_model_artifact",
            "argument_model_artifact",
            "premise_model_artifact",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(name, value)

    def to_schema_dict(self) -> dict[str, str]:
        return {k: v for k, v in _json_value(self).items() if v is not None}


@dataclass(frozen=True)
class CorpusManifest:
    premise_corpus_artifact: str | None = None
    retrieval_policy: str | None = None

    def __post_init__(self) -> None:
        if self.premise_corpus_artifact is not None:
            _require_sha256("premise_corpus_artifact", self.premise_corpus_artifact)
        if self.retrieval_policy is not None:
            _require_str("retrieval_policy", self.retrieval_policy)

    def to_schema_dict(self) -> dict[str, str]:
        return {k: v for k, v in _json_value(self).items() if v is not None}


@dataclass(frozen=True)
class FormalTask:
    kernel: TheoremKernel
    kernel_version: str
    declaration_id: str
    root_state_id: str
    run_id: str
    lease_id: str
    fencing_token: int
    search_policy: str
    checkpoint_policy: CheckpointPolicy
    claim_id: str | None = None
    max_tactic_steps: int | None = None
    models: ModelBundle | None = None
    corpus: CorpusManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kernel, TheoremKernel):
            raise ContextValidationError("kernel must be a TheoremKernel")
        for name in ("kernel_version", "search_policy", "lease_id"):
            _require_str(name, getattr(self, name))
        _require_positive("fencing_token", self.fencing_token)
        if DECLARATION_ID_RE.fullmatch(str(self.declaration_id)) is None:
            raise ContextValidationError("declaration_id must look like <proof>/fd-<n>")
        if STATE_ID_RE.fullmatch(str(self.root_state_id)) is None:
            raise ContextValidationError("root_state_id must look like <proof>/fs-<n>")
        if RUN_ID_RE.fullmatch(str(self.run_id)) is None:
            raise ContextValidationError("run_id must look like <proof>/fr-<n>")
        if not isinstance(self.checkpoint_policy, CheckpointPolicy):
            raise ContextValidationError("checkpoint_policy must be a CheckpointPolicy")
        if self.claim_id is not None and CLAIM_ID_RE.fullmatch(str(self.claim_id)) is None:
            raise ContextValidationError("claim_id must look like <proof>/c-<n>")
        if self.max_tactic_steps is not None:
            _require_positive("max_tactic_steps", self.max_tactic_steps)
        if self.models is not None and not isinstance(self.models, ModelBundle):
            raise ContextValidationError("models must be a ModelBundle")
        if self.corpus is not None and not isinstance(self.corpus, CorpusManifest):
            raise ContextValidationError("corpus must be a CorpusManifest")


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PremiseEntry:
    source_id: str
    lean_name: str
    statement: str
    status: str
    source_hash: str
    basis: EvidenceKind | None = None  # suggestions only


@dataclass(frozen=True)
class PriorRun:
    run_id: str
    disposition: str | None
    environment_hash: str | None
    frontier_state_ids: tuple[str, ...]
    summary: str | None


@dataclass(frozen=True)
class TranspositionEntry:
    source_id: str
    from_state_id: str
    to_state_id: str


@dataclass(frozen=True)
class FormalRequestPayload:
    proof_id: str
    task_id: str
    base_revision: int
    lease_id: str
    fencing_token: int
    run_id: str
    resume: bool
    declaration_id: str
    claim_id: str
    kernel: TheoremKernel
    kernel_version: str
    lean_type: str
    lean_value: str | None
    lean_name: str | None
    module_path: str | None
    declaration_hash: str
    lean_source_artifact: str
    root_state: Mapping[str, Any]
    environment: Mapping[str, Any]
    verified_premises: tuple[PremiseEntry, ...]
    suggested_premises: tuple[PremiseEntry, ...]
    corpus_manifest: Mapping[str, str] | None
    model_bundle: Mapping[str, str] | None
    search_policy: str
    max_tactic_steps: int | None
    limits: Mapping[str, int]
    checkpoint_policy: CheckpointPolicy
    prior_runs: tuple[PriorRun, ...]
    transpositions: tuple[TranspositionEntry, ...]
    source_artifacts: tuple[SourceRef, ...]

    def to_dict(self) -> dict[str, Any]:
        """Everything the compiler selected, for reuse and audit."""
        return _json_value(self)

    def to_schema_dict(self) -> dict[str, Any]:
        """The wire request, exactly as ``formal-search-request.schema.json``."""
        request: dict[str, Any] = {
            "proof_id": self.proof_id,
            "claim_id": self.claim_id,
            "formal_declaration_id": self.declaration_id,
            "run_id": self.run_id,
            "base_revision": self.base_revision,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "lean_source_artifact": self.lean_source_artifact,
            "environment_id": self.environment["environment_id"],
            "environment_hash": self.environment["environment_hash"],
        }
        if self.corpus_manifest:
            request["corpus_manifest"] = dict(self.corpus_manifest)
        if self.model_bundle:
            request["model_bundle"] = dict(self.model_bundle)
        policy: dict[str, Any] = {"policy_name": self.search_policy, "deny_sorry": True}
        if self.max_tactic_steps is not None:
            policy["max_tactic_steps"] = self.max_tactic_steps
        request["search_policy"] = policy
        request["budget"] = dict(self.limits)
        return request

    def to_context_dict(self) -> dict[str, Any]:
        """Selected context the schema has no field for (see PR notes)."""
        return _json_value(
            {
                "kernel": self.kernel,
                "kernel_version": self.kernel_version,
                "resume": self.resume,
                "lean_type": self.lean_type,
                "lean_value": self.lean_value,
                "lean_name": self.lean_name,
                "module_path": self.module_path,
                "declaration_hash": self.declaration_hash,
                "root_state": self.root_state,
                "environment": self.environment,
                "verified_premises": self.verified_premises,
                "suggested_premises": self.suggested_premises,
                "checkpoint_policy": self.checkpoint_policy,
                "prior_runs": self.prior_runs,
                "transpositions": self.transpositions,
                "source_artifacts": self.source_artifacts,
            }
        )


@dataclass(frozen=True)
class FormalCompilation:
    payload: FormalRequestPayload
    packet: ContextPacket
    result: CompiledResult

    @property
    def manifest(self) -> Mapping[str, object]:
        return self.result.manifest

    @property
    def packet_digest(self) -> str:
        return self.result.packet_digest


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Source:
    ref: SourceRef
    body: Mapping[str, Any]
    status: str


def _environment_hash(body: Mapping[str, Any]) -> str:
    """The pinned-environment digest: ``environment_hash``, else the lake manifest's."""
    value = body.get("environment_hash")
    if value is None:
        value = body.get("lake_manifest_hash")
    return _require_sha256("environment_hash", value)


def _premise_text(body: Mapping[str, Any]) -> str:
    """A premise's statement: ``statement``, a Claim's ``claim_text``, or a
    declaration's ``lean_type``."""
    for field_name in ("statement", "claim_text", "lean_type"):
        value = body.get(field_name)
        if isinstance(value, str) and value.strip():
            return value
    raise ContextValidationError("premise needs statement, claim_text or lean_type")


def _status(body: Mapping[str, Any], default: str = _PENDING) -> str:
    value = body.get("status", default)
    return _require_str("status", value)


def _load(ref: SourceRef, reader: ArtifactReader) -> _Source:
    if ref.source_type not in SOURCE_TYPES:
        raise ContextValidationError(
            f"unknown source_type {ref.source_type!r} for {ref.source_id}"
        )
    body = reader.fetch(ref.source_id)
    if not isinstance(body, Mapping):
        raise ContextValidationError(f"source artifact {ref.source_id} must be a mapping")

    kind = ref.source_type
    if kind == DECLARATION_TYPE:
        if body.get("declaration_id") not in (None, ref.source_id):
            raise ContextValidationError(
                f"declaration_id must match source_id {ref.source_id}"
            )
        status = _status(body)
        if status not in values(DeclarationStatus):
            raise ContextValidationError("declaration status must be a declaration status")
        _require_str("lean_type", body.get("lean_type"))
        for optional in ("lean_value", "lean_name", "module_path"):
            if body.get(optional) is not None:
                _require_str(optional, body.get(optional))
    elif kind == ENVIRONMENT_TYPE:
        if body.get("environment_id") not in (None, ref.source_id):
            raise ContextValidationError(
                f"environment_id must match source_id {ref.source_id}"
            )
        _environment_hash(body)
        _require_str("toolchain", body.get("toolchain"))
        status = _status(body)
    elif kind == STATE_TYPE:
        if body.get("state_id") != ref.source_id:
            raise ContextValidationError(f"state_id must match source_id {ref.source_id}")
        if STATE_ID_RE.fullmatch(ref.source_id) is None:
            raise ContextValidationError("state_id must look like <proof>/fs-<n>")
        status = _status(body)
        if status not in values(FormalStateStatus):
            raise ContextValidationError("status must be a formal state status")
        
        if body.get("environment_id") is not None:
            _require_str("environment_id", body.get("environment_id"))
            
        _require_sha256("exact_state_hash", body.get("exact_state_hash"))
        _require_sha256("semantic_signature", body.get("semantic_signature"))
        for optional_hash in ("context_digest", "environment_hash"):
            if body.get(optional_hash) is not None:
                _require_sha256(optional_hash, body.get(optional_hash))
        if body.get("goal_text") is not None:
            _require_str("goal_text", body.get("goal_text"))
        if body.get("kind") is not None and body.get("kind") not in values(StateKind):
            raise ContextValidationError("kind must be a state kind (or, and, goal)")
        if body.get("serialization_version") is not None:
            _require_positive("serialization_version", body.get("serialization_version"))
        if body.get("is_theorem") is not None and not isinstance(body.get("is_theorem"), bool):
            raise ContextValidationError("is_theorem must be a boolean")
    elif kind == PREMISE_TYPE:
        _require_str("lean_name", body.get("lean_name"))
        _premise_text(body)
        status = _status(body, default="")
        evidence = body.get("evidence_kind")
        if evidence is not None and evidence not in values(EvidenceKind):
            raise ContextValidationError("evidence_kind must be an EvidenceKind")
    elif kind == RUN_TYPE:
        if body.get("run_id") not in (None, ref.source_id):
            raise ContextValidationError(f"run_id must match source_id {ref.source_id}")
        if RUN_ID_RE.fullmatch(ref.source_id) is None:
            raise ContextValidationError("run_id must look like <proof>/fr-<n>")
        disposition = body.get("disposition")
        if disposition is not None and disposition not in values(RunDisposition):
            raise ContextValidationError("disposition must be a run disposition")
        if body.get("environment_hash") is not None:
            _require_sha256("environment_hash", body.get("environment_hash"))
        _str_list("frontier_state_ids", body.get("frontier_state_ids"))
        status = disposition or _PENDING
    else:  # transposition
        _require_str("from_state_id", body.get("from_state_id"))
        _require_str("to_state_id", body.get("to_state_id"))
        if not isinstance(body.get("accepted"), bool):
            raise ContextValidationError("accepted must be a boolean")
        status = _status(body)
    return _Source(ref=ref, body=body, status=status)


# --------------------------------------------------------------------------
# compiler
# --------------------------------------------------------------------------

def compile_formal(
    request: CompileRequest,
    bundle: SourceBundle,
    reader: ArtifactReader,
    task: FormalTask,
    accessibility: PremiseAccessibility,
    *,
    compiler_version: str = DEFAULT_COMPILER_VERSION,
    rendering_version: str = DEFAULT_RENDERING_VERSION,
) -> FormalCompilation:
    if not isinstance(request, CompileRequest):
        raise ContextValidationError("request must be a CompileRequest")
    if request.packet_kind is not PacketKind.FORMAL:
        raise ContextValidationError("request packet_kind must be formal")
    if request.formal_limits is None:
        raise ContextValidationError("formal requests require formal_limits")
    if not isinstance(bundle, SourceBundle):
        raise ContextValidationError("bundle must be a SourceBundle")
    if not isinstance(task, FormalTask):
        raise ContextValidationError("task must be a FormalTask")
    if not isinstance(accessibility, PremiseAccessibility):
        raise ContextValidationError("accessibility must be a PremiseAccessibility")

    refs = sorted(bundle.sources, key=lambda r: r.source_id)
    sources = {ref.source_id: _load(ref, reader) for ref in refs}
    decisions: dict[str, SelectionDecision] = {}

    def decide(src: _Source, included: bool, reason: str) -> None:
        decisions[src.ref.source_id] = SelectionDecision(
            source_id=src.ref.source_id,
            source_version=src.ref.source_version,
            status=src.status,
            included=included,
            reason=reason,
        )

    # ---- declaration, root state, environment ------------------------------
    declaration = sources.get(task.declaration_id)
    if declaration is None or declaration.ref.source_type != DECLARATION_TYPE:
        raise ContextValidationError("declaration is not a formal-declaration in the bundle")
    if declaration.status == DeclarationStatus.STALE.value:
        raise ContextValidationError(f"declaration {task.declaration_id} is stale")
    root = sources.get(task.root_state_id)
    if root is None or root.ref.source_type != STATE_TYPE:
        raise ContextValidationError("root state is not a formal-state in the bundle")

    environments = [s for s in sources.values() if s.ref.source_type == ENVIRONMENT_TYPE]
    named_env = declaration.body.get("environment_id")
    if isinstance(named_env, str):
        chosen = [e for e in environments if e.ref.source_id == named_env]
    else:
        chosen = environments
    if len(chosen) != 1:
        raise ContextValidationError(
            "exactly one pinned environment must be identifiable for the declaration"
        )
    environment = chosen[0]
    env_hash = _environment_hash(environment.body)
    root_env = root.body.get("environment_hash")
    if root_env is not None and root_env != env_hash:
        raise ContextValidationError(
            "root state was elaborated under a different environment than the pinned one"
        )

    decide(declaration, True, "selected formal declaration")
    decide(root, True, "root state")
    decide(environment, True, "pinned environment")

    # ---- prior runs / resume -------------------------------------------------
    resumed = sources.get(task.run_id)
    if resumed is not None:
        if resumed.ref.source_type != RUN_TYPE:
            raise ContextValidationError(f"{task.run_id} is not a formal-run")
        if resumed.body.get("declaration_id") != task.declaration_id:
            raise ContextValidationError("resumed run belongs to a different declaration")
        run_env = resumed.body.get("environment_hash")
        if run_env is not None and run_env != env_hash:
            raise ContextValidationError(
                "resumed run used a different environment than the pinned one"
            )

    prior_runs: list[PriorRun] = []
    for src in sources.values():
        if src.ref.source_type != RUN_TYPE:
            continue
        body = src.body
        if body.get("declaration_id") != task.declaration_id:
            decide(src, False, "run of a different or unnamed declaration")
        elif body.get("environment_hash") not in (None, env_hash):
            decide(src, False, "run used a different environment")
        else:
            decide(src, True, "resumed run" if src is resumed else "prior run of this declaration")
            summary = body.get("summary")
            prior_runs.append(
                PriorRun(
                    run_id=src.ref.source_id,
                    disposition=body.get("disposition"),
                    environment_hash=body.get("environment_hash"),
                    frontier_state_ids=_str_list("frontier_state_ids", body.get("frontier_state_ids")),
                    summary=summary if isinstance(summary, str) and summary.strip() else None,
                )
            )

    # ---- transpositions ------------------------------------------------------
    transpositions: list[TranspositionEntry] = []
    for src in sources.values():
        if src.ref.source_type != TRANSPOSITION_TYPE:
            continue
        if src.body["accepted"]:
            decide(src, True, "accepted transposition")
            transpositions.append(
                TranspositionEntry(
                    source_id=src.ref.source_id,
                    from_state_id=src.body["from_state_id"],
                    to_state_id=src.body["to_state_id"],
                )
            )
        else:
            decide(src, False, "transposition not accepted")

    # ---- premises ------------------------------------------------------------
    candidates: dict[str, list[_Source]] = {}
    for src in sources.values():
        is_premise = src.ref.source_type == PREMISE_TYPE
        is_other_declaration = (
            src.ref.source_type == DECLARATION_TYPE
            and src.ref.source_id != task.declaration_id
        )
        if not (is_premise or is_other_declaration):
            continue
        name = src.body.get("lean_name")
        if not isinstance(name, str) or not name.strip():
            decide(src, False, "declaration has no lean_name")
            continue
        if src.status in _UNUSABLE:
            decide(src, False, f"status {src.status} is not usable as a premise")
        elif name in accessibility.unavailable:
            decide(src, False, f"not accessible: {accessibility.unavailable[name]}")
        elif name not in accessibility.accessible:
            decide(src, False, "accessibility not reported for this premise")
        else:
            candidates.setdefault(name, []).append(src)

    verified: list[PremiseEntry] = []
    suggested: list[PremiseEntry] = []
    for name in sorted(candidates):
        group = sorted(
            candidates[name],
            key=lambda s: (s.status not in _VERIFIED, s.ref.source_id),
        )
        winner = group[0]
        is_verified = winner.status in _VERIFIED
        basis = None
        if not is_verified:
            raw = winner.body.get("evidence_kind")
            basis = EvidenceKind(raw) if raw else EvidenceKind.HEURISTIC_OPTIMIZER
        entry = PremiseEntry(
            source_id=winner.ref.source_id,
            lean_name=name,
            statement=_premise_text(winner.body),
            status=winner.status,
            source_hash=winner.ref.content_hash,
            basis=basis,
        )
        (verified if is_verified else suggested).append(entry)
        decide(
            winner,
            True,
            "verified premise" if is_verified else "suggested premise (unverified)",
        )
        for loser in group[1:]:
            if is_verified and loser.status not in _VERIFIED:
                decide(loser, False, f"superseded by verified premise {winner.ref.source_id}")
            else:
                decide(loser, False, f"duplicate of {winner.ref.source_id}")

    # ---- everything else -----------------------------------------------------
    for src in sources.values():
        if src.ref.source_id in decisions:
            continue
        if src.ref.source_type == STATE_TYPE:
            decide(src, False, "not the root state")
        elif src.ref.source_type == DECLARATION_TYPE:
            decide(src, False, "not the selected declaration")
        else:
            decide(src, False, "environment not pinned for the declaration")

    # ---- claim and Lean source identity ---------------------------------------
    body_claim = declaration.body.get("claim_id")
    if task.claim_id and isinstance(body_claim, str) and body_claim != task.claim_id:
        raise ContextValidationError(
            "task claim_id disagrees with the declaration's claim_id"
        )
    claim_id = task.claim_id or body_claim
    if not isinstance(claim_id, str) or CLAIM_ID_RE.fullmatch(claim_id) is None:
        raise ContextValidationError(
            "a claim_id like <proof>/c-<n> is required (task or declaration body)"
        )
    source_artifact = declaration.body.get("lean_source_artifact")
    if source_artifact is None:
        source_artifact = declaration.ref.content_hash
    _require_sha256("lean_source_artifact", source_artifact)

    # ---- payload ---------------------------------------------------------------
    limits = request.formal_limits
    root_body = root.body

    # semantic_signature may only *propose* transpositions; it is carried for
    # reference and never used to merge states.
    root_state: dict[str, Any] = {
        "state_id": root.ref.source_id,
        "status": root.status,
        "exact_state_hash": root_body["exact_state_hash"],
        "semantic_signature": root_body["semantic_signature"],
        "source_hash": root.ref.content_hash,
    }
    for optional_key in (
        "goal_text",
        "kind",
        "context_digest",
        "environment_id",
        "environment_hash",
        "serialization_version",
        "is_theorem",
    ):
        if root_body.get(optional_key) is not None:
            root_state[optional_key] = root_body[optional_key]

    payload = FormalRequestPayload(
        proof_id=request.proof_id,
        task_id=request.task_id,
        base_revision=request.base_revision,
        lease_id=task.lease_id,
        fencing_token=task.fencing_token,
        run_id=task.run_id,
        resume=resumed is not None,
        declaration_id=task.declaration_id,
        claim_id=claim_id,
        kernel=task.kernel,
        kernel_version=task.kernel_version,
        lean_type=declaration.body["lean_type"],
        lean_value=declaration.body.get("lean_value"),
        lean_name=declaration.body.get("lean_name"),
        module_path=declaration.body.get("module_path"),
        declaration_hash=declaration.ref.content_hash,
        lean_source_artifact=source_artifact,
        root_state=root_state,
        environment={
            "environment_id": environment.ref.source_id,
            "environment_hash": env_hash,
            "lake_manifest_hash": environment.body.get("lake_manifest_hash"),
            "mathlib_commit": environment.body.get("mathlib_commit"),
            "toolchain": environment.body["toolchain"],
            "source_hash": environment.ref.content_hash,
        },
        verified_premises=tuple(verified),
        suggested_premises=tuple(suggested),
        corpus_manifest=task.corpus.to_schema_dict() if task.corpus else None,
        model_bundle=task.models.to_schema_dict() if task.models else None,
        search_policy=task.search_policy,
        max_tactic_steps=task.max_tactic_steps,
        limits={"max_steps": limits.max_steps, "wall_clock_ms": limits.wall_clock_ms},
        checkpoint_policy=task.checkpoint_policy,
        prior_runs=tuple(sorted(prior_runs, key=lambda r: r.run_id)),
        transpositions=tuple(sorted(transpositions, key=lambda t: t.source_id)),
        source_artifacts=tuple(refs),
    )

    envelope = {
        "request": payload.to_schema_dict(),
        "context": payload.to_context_dict(),
    }
    packet = ContextPacket(
        task_id=request.task_id,
        proof_id=request.proof_id,
        base_revision=request.base_revision,
        packet_kind=PacketKind.FORMAL,
        content=json.dumps(envelope, ensure_ascii=True, sort_keys=True, indent=2),
        text_budget=request.text_budget,
        formal_limits=request.formal_limits,
    )
    kernel = task.kernel
    ordered: Sequence[SelectionDecision] = (
        SelectionDecision(
            source_id=kernel.kernel_id,
            source_version=task.kernel_version,
            status=kernel.status.value,
            included=True,
            reason="theorem kernel",
        ),
        *(decisions[ref.source_id] for ref in refs),
    )
    result = finalize_packet(
        packet,
        tuple(ordered),
        compiler_version=compiler_version,
        rendering_version=rendering_version,
    )
    return FormalCompilation(payload=payload, packet=packet, result=result)