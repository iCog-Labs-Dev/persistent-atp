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
import math
import re
from dataclasses import asdict, dataclass, field, replace
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
    FormalContextLimits,
    PacketKind,
    SelectionDecision,
    SourceBundle,
    SourceRef,
    TheoremKernel,
)
from .budgeting import CanonicalTokenCounter, TokenCounter, token_counter_manifest
from .errors import ContextValidationError
from .finalize import _json_value, finalize_packet
from .obstruction import ArtifactReader
from .selection import (
    DEFAULT_CONTEXT_POLICY,
    ContextSelectionPolicy,
    REASON_OVER_PREMISE_LIMIT,
    REASON_OVER_PRIOR_RUN_LIMIT,
    REASON_OVER_TEXT_BUDGET,
    REASON_OVER_TRANSPOSITION_LIMIT,
    apply_item_limit,
    duplicate_reason,
    trailing_ordinal,
    text_similarity,
    weighted_mmr_order,
)

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

DEFAULT_COMPILER_VERSION = "context/2"
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
class FormalV2Selection:
    """Explicit provider deployment and input mode, owned by the coordinator."""

    deployment_id: str
    backend: str
    profile: str
    input_kind: str
    universe_levels: tuple[str, ...] = ()
    proposal_mode: str | None = None
    seed: int | None = None
    provider_environment_id: str | None = None


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

    def to_schema_dict(
        self, *, protocol_version: int = 1, backend: Mapping[str, Any] | None = None,
        deployment_id: str | None = None, formal_input: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the selected wire version without changing declaration identity.

        V2 defaults to the named declaration in the stored Lean source. Goal
        mode requires an explicit complete input, including locals/universes;
        the pretty-printed root alone is not a faithful replacement.
        """
        if type(protocol_version) is not int or protocol_version not in (1, 2):
            raise ContextValidationError("formal protocol_version must be 1 or 2")
        if protocol_version == 1 and any(value is not None for value in (backend, deployment_id, formal_input)):
            raise ContextValidationError("backend, deployment and explicit input require formal v2")
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
            "goal_text": self.root_state["goal_text"],
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
        if protocol_version == 2:
            request["environment_id"] = self.environment.get("provider_environment_id") or self.environment["environment_id"]
            request.pop("lean_source_artifact")
            request.pop("goal_text")
            request["protocol_version"] = 2
            if formal_input is None:
                if self.lean_name is None:
                    raise ContextValidationError("formal v2 source input requires the declaration's lean_name")
                if self.lean_source_artifact == self.declaration_hash:
                    raise ContextValidationError("v2 source input requires stored Lean source bytes, not the declaration record hash")
                formal_input = {"kind": "source", "artifact_hash": self.lean_source_artifact,
                                "declaration": self.lean_name, "starting_state": "binders-introduced"}
            request["input"] = _json_value(formal_input)
            if formal_input.get("kind") == "source" and (
                formal_input.get("artifact_hash") != self.lean_source_artifact
                or formal_input.get("declaration") != self.lean_name
            ):
                raise ContextValidationError("formal v2 source input differs from the selected declaration")
            policy.pop("max_tactic_steps", None)
            if self.max_tactic_steps is not None:
                request["budget"]["max_steps"] = min(request["budget"]["max_steps"], self.max_tactic_steps)
            if backend is not None:
                request["backend"] = dict(backend)
            if deployment_id is not None:
                request["deployment_id"] = deployment_id
            # The pinned wire contract rejects unknown fields and incomplete
            # inputs before dispatch, without requiring a provider connection.
            from mathproof.formal_v2 import validate
            errors = validate("formal-search-request", request)
            if errors:
                raise ContextValidationError("invalid v2 request: " + "; ".join(errors))
        return request

    def to_v2_schema_dict(self, *, deployment_id: str, backend: str,
                          profile: str, input_kind: str,
                          universe_levels: tuple[str, ...] = (),
                          proposal_mode: str | None = None,
                          seed: int | None = None,
                          provider_environment_id: str | None = None) -> dict[str, Any]:
        """Export the explicit source/goal union for the maths-AI v2 service.

        Choose input_kind deliberately: a source failure must never fall back
        to a different text goal. The adapter resolves source bytes by hash.
        """
        if input_kind == "source":
            if not self.lean_name:
                raise ContextValidationError("v2 source input requires the declaration's lean_name")
            if self.lean_source_artifact == self.declaration_hash:
                raise ContextValidationError("v2 source input requires a stored Lean source artifact; the declaration record hash is not source bytes")
            formal_input = {"kind": "source", "artifact_hash": self.lean_source_artifact,
                                "declaration": self.lean_name,
                                "starting_state": "binders-introduced"}
        elif input_kind == "goal":
            formal_input = {"kind": "goal", "goal_text": self.root_state["goal_text"],
                                "hypotheses": list(self.root_state.get("hypotheses", ())),
                                "universe_levels": list(universe_levels)}
        else:
            raise ContextValidationError("v2 input_kind must be source or goal")
        selection = {"name": backend, "profile": profile}
        if proposal_mode is not None:
            selection["proposal_mode"] = proposal_mode
        if seed is not None:
            selection["seed"] = seed
        request = self.to_schema_dict(protocol_version=2, deployment_id=deployment_id,
                                      backend=selection, formal_input=formal_input)
        if provider_environment_id is not None:
            request["environment_id"] = provider_environment_id
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


def _source_text(source: _Source) -> str:
    """Stable lexical representation used only by context ranking."""
    body = source.body
    if source.ref.source_type == PREMISE_TYPE:
        parts = (body.get("lean_name"), _premise_text(body))
    elif source.ref.source_type == DECLARATION_TYPE:
        parts = (body.get("lean_name"), body.get("lean_type"))
    elif source.ref.source_type == STATE_TYPE:
        hypotheses = body.get("hypotheses")
        if isinstance(hypotheses, str):
            hypothesis_parts = (hypotheses,)
        elif isinstance(hypotheses, (tuple, list)):
            hypothesis_parts = tuple(item for item in hypotheses if isinstance(item, str))
        else:
            hypothesis_parts = ()
        parts = (body.get("goal_text"), *hypothesis_parts)
    elif source.ref.source_type == RUN_TYPE:
        parts = (body.get("disposition"), body.get("summary"), *(body.get("frontier_state_ids") or ()))
    elif source.ref.source_type == TRANSPOSITION_TYPE:
        parts = (body.get("from_state_id"), body.get("to_state_id"), body.get("status"))
    else:
        parts = (body.get("toolchain"), body.get("mathlib_commit"))
    return " ".join(part for part in parts if isinstance(part, str))


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


def _unit_score(value: object, default: float = 0.5) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(1.0, max(0.0, float(value)))


def _selection_features(
    source: _Source,
    reference_text: str,
    *,
    ancestry: float,
    verification: float,
    failure: float,
    text: str | None = None,
) -> dict[str, float]:
    annotations = source.body.get("annotations")

    def value(name: str):
        raw = source.body.get(name)
        return annotations.get(name) if raw is None and isinstance(annotations, Mapping) else raw

    similarity = text_similarity(text if text is not None else _source_text(source), reference_text)
    pln_values = [value(name) for name in ("pln_strength", "pln_confidence")]
    pln_values = [_unit_score(item) for item in pln_values if item is not None]
    proof_number = value("proof_number")
    if (isinstance(proof_number, bool) or not isinstance(proof_number, (int, float))
            or not math.isfinite(proof_number) or proof_number < 0):
        proof_priority = 0.5
    else:
        proof_priority = 1.0 / (1.0 + float(proof_number))
    novelty = value("state_novelty")
    if novelty is None:
        transpositions = value("transposition_count")
        novelty = (1.0 / (1.0 + float(transpositions))
            if isinstance(transpositions, (int, float)) and not isinstance(transpositions, bool)
            and math.isfinite(transpositions) and transpositions >= 0 else 1.0 - similarity)
    return {
        "dependency": _unit_score(value("dependency_centrality")),
        "ancestry": _unit_score(ancestry),
        "semantic_similarity": similarity,
        "verification": _unit_score(verification),
        "failure": _unit_score(failure),
        "pln": sum(pln_values) / len(pln_values) if pln_values else 0.5,
        "proof_number": _unit_score(proof_priority),
        "novelty": _unit_score(novelty),
    }


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
    context_limits: FormalContextLimits | None = None,
    selection_policy: ContextSelectionPolicy = DEFAULT_CONTEXT_POLICY,
    v2_selection: FormalV2Selection | None = None,
    compiler_version: str = DEFAULT_COMPILER_VERSION,
    rendering_version: str = DEFAULT_RENDERING_VERSION,
    protocol_version: int = 1,
    backend: Mapping[str, Any] | None = None,
    deployment_id: str | None = None,
    formal_input: Mapping[str, Any] | None = None,
    token_counter: TokenCounter | None = None,
) -> FormalCompilation:
    """Compile a selected formal task into one formal-search packet.

    Mandatory context (never limited, never dropped): theorem kernel, selected
    declaration, root state, pinned environment, and the resumed run when
    resuming. Optional context (premises, prior runs, transpositions) is scored
    and diversified by the selected weighted-MMR policy before item limits are
    applied. Verified premises remain ahead of suggestions, and a resumed run
    is mandatory. ``None`` keeps every eligible optional item.
    """
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
    if not isinstance(selection_policy, ContextSelectionPolicy):
        raise ContextValidationError("selection_policy must be a ContextSelectionPolicy")
    if context_limits is not None and not isinstance(context_limits, FormalContextLimits):
        raise ContextValidationError("context_limits must be FormalContextLimits")
    if token_counter is not None and not callable(getattr(token_counter, "count", None)):
        raise ContextValidationError("token_counter must provide count(text)")
    counter = token_counter if token_counter is not None else CanonicalTokenCounter()

    refs = sorted(bundle.sources, key=lambda r: r.source_id)
    sources = {ref.source_id: _load(ref, reader) for ref in refs}
    decisions: dict[str, SelectionDecision] = {}

    def decide(src: _Source, included: bool, reason: str,
               score_breakdown: Mapping[str, float] | None = None) -> None:
        decisions[src.ref.source_id] = SelectionDecision(
            source_id=src.ref.source_id,
            source_version=src.ref.source_version,
            status=src.status,
            included=included,
            reason=reason,
            selection_score=(score_breakdown.get("mmr_score") if score_breakdown else None),
            score_breakdown=score_breakdown or {},
            selection_policy=selection_policy.name if score_breakdown else None,
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

    goal_text = _require_str(
        "root state goal_text",
        root.body.get("goal_text"),
    )
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
    reference_text = " ".join((task.kernel.statement, goal_text, str(declaration.body["lean_type"])))
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

    # Eligible prior runs; the resumed run is mandatory (resuming without it is
    # meaningless) and is never removed by the optional context cap.
    eligible_runs: list[tuple[_Source, PriorRun]] = []
    for src in sources.values():
        if src.ref.source_type != RUN_TYPE:
            continue
        body = src.body
        if body.get("declaration_id") != task.declaration_id:
            decide(src, False, "run of a different or unnamed declaration")
        elif body.get("environment_hash") not in (None, env_hash):
            decide(src, False, "run used a different environment")
        else:
            summary = body.get("summary")
            eligible_runs.append(
                (
                    src,
                    PriorRun(
                        run_id=src.ref.source_id,
                        disposition=body.get("disposition"),
                        environment_hash=body.get("environment_hash"),
                        frontier_state_ids=_str_list(
                            "frontier_state_ids", body.get("frontier_state_ids")
                        ),
                        summary=summary if isinstance(summary, str) and summary.strip() else None,
                    ),
                )
            )
    eligible_runs.sort(key=lambda item: item[0].ref.source_id)
    mandatory_runs = [item for item in eligible_runs if item[0] is resumed]
    optional_runs = [item for item in eligible_runs if item[0] is not resumed]
    run_slots = (
        None
        if context_limits is None
        else max(context_limits.max_prior_runs - len(mandatory_runs), 0)
    )
    run_ordinals = [trailing_ordinal(src.ref.source_id) for src, _ in optional_runs]
    min_run_ordinal = min(run_ordinals, default=0)
    max_run_ordinal = max(run_ordinals, default=0)
    run_features: dict[str, Mapping[str, float]] = {}
    run_texts: dict[str, str] = {}
    failed_run_dispositions = {
        RunDisposition.BUDGET_EXHAUSTED.value,
        RunDisposition.STAGNATED.value,
        RunDisposition.COUNTEREXAMPLE.value,
        RunDisposition.INVALID_REQUEST.value,
        RunDisposition.ENVIRONMENT_ERROR.value,
        RunDisposition.INTERNAL_ERROR.value,
    }
    for src, prior in optional_runs:
        frontier_ids = set(prior.frontier_state_ids)
        run_texts[src.ref.source_id] = _source_text(src)
        features = _selection_features(
            src,
            reference_text,
            ancestry=1.0 if task.root_state_id in frontier_ids else 0.5 if not frontier_ids else 0.25,
            verification=0.5,
            failure=0.95 if prior.disposition in failed_run_dispositions else 0.5,
            text=run_texts[src.ref.source_id],
        )
        ordinal = trailing_ordinal(src.ref.source_id)
        features["novelty"] = (
            (ordinal - min_run_ordinal) / (max_run_ordinal - min_run_ordinal)
            if max_run_ordinal > min_run_ordinal else 0.5
        )
        run_features[src.ref.source_id] = features
    ranked_runs = weighted_mmr_order(
        [src for src, _ in optional_runs],
        features=lambda src: run_features[src.ref.source_id],
        similarity_text=lambda src: run_texts[src.ref.source_id],
        identity=lambda src: src.ref.source_id,
        token_cost=lambda src: len(run_texts[src.ref.source_id]),
        policy=selection_policy,
    )
    run_breakdowns = {item.item.ref.source_id: item.score_breakdown for item in ranked_runs}
    prior_by_id = {src.ref.source_id: prior for src, prior in optional_runs}
    ranked_optional_runs = [(item.item, prior_by_id[item.item.ref.source_id]) for item in ranked_runs]
    run_selection = apply_item_limit(ranked_optional_runs, run_slots)
    prior_runs: list[PriorRun] = []
    for src, prior in mandatory_runs:
        decide(src, True, "resumed run")
        prior_runs.append(prior)
    for src, prior in run_selection.kept:
        decide(src, True, "prior run selected by weighted MMR",
               run_breakdowns[src.ref.source_id])
        prior_runs.append(prior)
    for src, _ in run_selection.dropped:
        decide(src, False, REASON_OVER_PRIOR_RUN_LIMIT,
               run_breakdowns[src.ref.source_id])

    # ---- transpositions ------------------------------------------------------
    # Accepted transpositions: exact duplicates (same from/to pair) collapse to
    # the lowest source_id before weighted relevance/diversity ranking.
    accepted: list[_Source] = []
    for src in sources.values():
        if src.ref.source_type != TRANSPOSITION_TYPE:
            continue
        if src.body["accepted"]:
            accepted.append(src)
        else:
            decide(src, False, "transposition not accepted")
    accepted.sort(key=lambda s_: s_.ref.source_id)
    unique_transpositions: list[_Source] = []
    first_for_pair: dict[tuple[str, str], str] = {}
    for src in accepted:
        pair = (src.body["from_state_id"], src.body["to_state_id"])
        if pair in first_for_pair:
            decide(src, False, duplicate_reason(first_for_pair[pair]))
        else:
            first_for_pair[pair] = src.ref.source_id
            unique_transpositions.append(src)
    transposition_texts: dict[str, str] = {}
    transposition_features: dict[str, Mapping[str, float]] = {}
    for src in unique_transpositions:
        state_texts = [
            _source_text(sources[state_id])
            for state_id in (src.body["from_state_id"], src.body["to_state_id"])
            if state_id in sources and sources[state_id].ref.source_type == STATE_TYPE
        ]
        text = " ".join((_source_text(src), *state_texts))
        transposition_texts[src.ref.source_id] = text
        touches_root = task.root_state_id in (src.body["from_state_id"], src.body["to_state_id"])
        transposition_features[src.ref.source_id] = _selection_features(
            src,
            reference_text,
            ancestry=1.0 if touches_root else 0.25,
            verification=0.65,
            failure=0.5,
            text=text,
        )
    ranked_transpositions = weighted_mmr_order(
        unique_transpositions,
        features=lambda src: transposition_features[src.ref.source_id],
        similarity_text=lambda src: transposition_texts[src.ref.source_id],
        identity=lambda src: src.ref.source_id,
        token_cost=lambda src: len(transposition_texts[src.ref.source_id]),
        policy=selection_policy,
    )
    transposition_breakdowns = {
        item.item.ref.source_id: item.score_breakdown for item in ranked_transpositions
    }
    transposition_selection = apply_item_limit(
        [item.item for item in ranked_transpositions],
        None if context_limits is None else context_limits.max_transpositions,
    )
    transpositions: list[TranspositionEntry] = []
    for src in transposition_selection.kept:
        decide(src, True, "accepted transposition selected by weighted MMR",
               transposition_breakdowns[src.ref.source_id])
        transpositions.append(
            TranspositionEntry(
                source_id=src.ref.source_id,
                from_state_id=src.body["from_state_id"],
                to_state_id=src.body["to_state_id"],
            )
        )
    for src in transposition_selection.dropped:
        decide(src, False, REASON_OVER_TRANSPOSITION_LIMIT,
               transposition_breakdowns[src.ref.source_id])

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

    # One winner per lean_name (verified beats suggested, then lowest
    # source_id).  MMR ranks verified and suggested groups independently, and
    # the verified group fills the limit first so suggestions cannot displace
    # verified premises.
    winners: list[tuple[_Source, PremiseEntry, bool]] = []
    for name in sorted(candidates):
        group = sorted(
            candidates[name],
            key=lambda s_: (s_.status not in _VERIFIED, s_.ref.source_id),
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
        winners.append((winner, entry, is_verified))
        for loser in group[1:]:
            if is_verified and loser.status not in _VERIFIED:
                decide(loser, False, f"superseded by verified premise {winner.ref.source_id}")
            else:
                decide(loser, False, duplicate_reason(winner.ref.source_id))
    premise_texts: dict[str, str] = {}
    premise_features: dict[str, Mapping[str, float]] = {}
    for winner, entry, is_verified in winners:
        premise_texts[winner.ref.source_id] = f"{entry.lean_name} {entry.statement}"
        claim_id = winner.body.get("claim_id")
        state_id = winner.body.get("state_id")
        if claim_id == task.claim_id or state_id == task.root_state_id:
            ancestry = 1.0
        elif claim_id is None and state_id is None:
            ancestry = 0.5
        else:
            ancestry = 0.25
        premise_features[winner.ref.source_id] = _selection_features(
            winner,
            reference_text,
            ancestry=ancestry,
            verification=1.0 if is_verified else 0.45,
            failure=0.5,
            text=premise_texts[winner.ref.source_id],
        )

    ranked_premises: list[tuple[_Source, PremiseEntry, bool]] = []
    premise_breakdowns: dict[str, Mapping[str, float]] = {}
    for verified_group in (True, False):
        group = [item for item in winners if item[2] is verified_group]
        ranked_group = weighted_mmr_order(
            group,
            features=lambda item: premise_features[item[0].ref.source_id],
            similarity_text=lambda item: premise_texts[item[0].ref.source_id],
            identity=lambda item: item[0].ref.source_id,
            token_cost=lambda item: len(premise_texts[item[0].ref.source_id]),
            policy=selection_policy,
        )
        for ranked_item in ranked_group:
            ranked_premises.append(ranked_item.item)
            premise_breakdowns[ranked_item.item[0].ref.source_id] = ranked_item.score_breakdown
    premise_selection = apply_item_limit(
        ranked_premises, None if context_limits is None else context_limits.max_premises
    )
    verified: list[PremiseEntry] = []
    suggested: list[PremiseEntry] = []
    for winner, entry, is_verified in premise_selection.kept:
        (verified if is_verified else suggested).append(entry)
        decide(
            winner,
            True,
            "verified premise selected by weighted MMR"
            if is_verified else "suggested premise selected by weighted MMR (unverified)",
            premise_breakdowns[winner.ref.source_id],
        )
    for winner, _, _ in premise_selection.dropped:
        decide(winner, False, REASON_OVER_PREMISE_LIMIT,
               premise_breakdowns[winner.ref.source_id])

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
        "goal_text": goal_text,
        "exact_state_hash": root_body["exact_state_hash"],
        "semantic_signature": root_body["semantic_signature"],
        "source_hash": root.ref.content_hash,
    }
    for optional_key in (
        "hypotheses",
        "kind",
        "context_digest",
        "environment_id",
        "environment_hash",
        "serialization_version",
        "is_theorem",
    ):
        if root_body.get(optional_key) is not None:
            root_state[optional_key] = root_body[optional_key]

    budget_candidates: dict[str, dict[str, Any]] = {}
    for entry in (*verified, *suggested):
        source = sources[entry.source_id]
        budget_candidates[entry.source_id] = {
            "kind": "premise",
            "source": source,
            "entry": entry,
            "features": premise_features[entry.source_id],
            "text": premise_texts[entry.source_id],
            "verified": entry.status in _VERIFIED,
        }
    mandatory_run_ids = {src.ref.source_id for src, _ in mandatory_runs}
    for prior in prior_runs:
        if prior.run_id in mandatory_run_ids:
            continue
        budget_candidates[prior.run_id] = {
            "kind": "run",
            "source": sources[prior.run_id],
            "entry": prior,
            "features": run_features[prior.run_id],
            "text": run_texts[prior.run_id],
        }
    for entry in transpositions:
        source = sources[entry.source_id]
        budget_candidates[entry.source_id] = {
            "kind": "transposition",
            "source": source,
            "entry": entry,
            "features": transposition_features[entry.source_id],
            "text": transposition_texts[entry.source_id],
        }

    mandatory_source_ids = {declaration.ref.source_id, root.ref.source_id, environment.ref.source_id}
    mandatory_source_ids.update(mandatory_run_ids)

    def assemble_payload(selected_ids: set[str]) -> FormalRequestPayload:
        selected_premises = [budget_candidates[source_id] for source_id in selected_ids
                             if budget_candidates[source_id]["kind"] == "premise"]
        selected_runs = [budget_candidates[source_id]["entry"] for source_id in selected_ids
                         if budget_candidates[source_id]["kind"] == "run"]
        selected_transpositions = [budget_candidates[source_id]["entry"] for source_id in selected_ids
                                   if budget_candidates[source_id]["kind"] == "transposition"]
        selected_verified = sorted(
            (item["entry"] for item in selected_premises if item["verified"]),
            key=lambda entry: entry.lean_name,
        )
        selected_suggested = sorted(
            (item["entry"] for item in selected_premises if not item["verified"]),
            key=lambda entry: entry.lean_name,
        )
        selected_prior_runs = [prior for _source_id, prior in mandatory_runs]
        selected_prior_runs.extend(selected_runs)
        selected_prior_runs.sort(key=lambda prior: prior.run_id)
        selected_transpositions.sort(key=lambda entry: entry.source_id)
        included_source_ids = set(mandatory_source_ids) | set(selected_ids)
        for prior in selected_prior_runs:
            included_source_ids.update(prior.frontier_state_ids)
        for entry in selected_transpositions:
            included_source_ids.update((entry.from_state_id, entry.to_state_id))
        source_artifacts = tuple(ref for ref in refs if ref.source_id in included_source_ids)
        return replace(
            payload_base,
            verified_premises=tuple(selected_verified),
            suggested_premises=tuple(selected_suggested),
            prior_runs=tuple(selected_prior_runs),
            transpositions=tuple(selected_transpositions),
            source_artifacts=source_artifacts,
        )

    payload_base = FormalRequestPayload(
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
            **({"provider_environment_id": environment.body["provider_environment_id"]}
               if environment.body.get("provider_environment_id") is not None else {}),
            "environment_hash": env_hash,
            "lake_manifest_hash": environment.body.get("lake_manifest_hash"),
            "mathlib_commit": environment.body.get("mathlib_commit"),
            "toolchain": environment.body["toolchain"],
            "source_hash": environment.ref.content_hash,
        },
        verified_premises=(),
        suggested_premises=(),
        corpus_manifest=task.corpus.to_schema_dict() if task.corpus else None,
        model_bundle=task.models.to_schema_dict() if task.models else None,
        search_policy=task.search_policy,
        max_tactic_steps=task.max_tactic_steps,
        limits={"max_steps": limits.max_steps, "wall_clock_ms": limits.wall_clock_ms},
        checkpoint_policy=task.checkpoint_policy,
        prior_runs=tuple(prior for _source, prior in mandatory_runs),
        transpositions=(),
        source_artifacts=tuple(ref for ref in refs if ref.source_id in mandatory_source_ids),
    )

    if v2_selection is not None and any(value is not None for value in (backend, deployment_id, formal_input)):
        raise ContextValidationError("select the v2 request once")
    def make_packet(compiled_payload: FormalRequestPayload) -> ContextPacket:
        envelope = {
            "request": (
                compiled_payload.to_v2_schema_dict(**asdict(v2_selection))
                if v2_selection is not None
                else compiled_payload.to_schema_dict(
                    protocol_version=protocol_version,
                    backend=backend,
                    deployment_id=deployment_id,
                    formal_input=formal_input,
                )
            ),
            "context": compiled_payload.to_context_dict(),
        }
        return ContextPacket(
            task_id=request.task_id,
            proof_id=request.proof_id,
            base_revision=request.base_revision,
            packet_kind=PacketKind.FORMAL,
            content=json.dumps(envelope, ensure_ascii=True, sort_keys=True, indent=2),
            text_budget=request.text_budget,
            formal_limits=request.formal_limits,
        )

    selected_ids: set[str] = set()
    payload = assemble_payload(selected_ids)
    packet = make_packet(payload)
    if request.text_budget is not None:
        mandatory_tokens = counter.count(packet.content)
        if mandatory_tokens > request.text_budget.max_tokens:
            raise ContextValidationError(
                "mandatory formal context alone exceeds text budget "
                f"({mandatory_tokens} tokens required, {request.text_budget.max_tokens} available)"
            )

        remaining = set(budget_candidates)
        mandatory_texts = [task.kernel.statement, _source_text(declaration),
                           _source_text(root), _source_text(environment)]
        mandatory_texts.extend(_source_text(sources[run_id]) for run_id in mandatory_run_ids)
        selected_texts: list[str] = []
        current_tokens = mandatory_tokens
        while remaining:
            ranked_candidates: list[tuple[float, str, ContextPacket, FormalRequestPayload,
                                         Mapping[str, float]]] = []
            for source_id in remaining:
                candidate = budget_candidates[source_id]
                trial_ids = selected_ids | {source_id}
                trial_payload = assemble_payload(trial_ids)
                trial_packet = make_packet(trial_payload)
                trial_tokens = counter.count(trial_packet.content)
                marginal_cost = max(0, trial_tokens - current_tokens)
                redundancy = max(
                    (text_similarity(candidate["text"], text)
                     for text in (*mandatory_texts, *selected_texts)),
                    default=0.0,
                )
                breakdown = selection_policy.breakdown(
                    candidate["features"],
                    redundancy=redundancy,
                    token_cost=float(marginal_cost),
                    cost_fraction=marginal_cost / request.text_budget.max_tokens,
                )
                ranked_candidates.append((
                    breakdown["mmr_score"], source_id, trial_packet, trial_payload, breakdown
                ))
            ranked_candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
            _score, source_id, trial_packet, trial_payload, breakdown = ranked_candidates[0]
            remaining.remove(source_id)
            source = budget_candidates[source_id]["source"]
            if counter.count(trial_packet.content) <= request.text_budget.max_tokens:
                selected_ids.add(source_id)
                selected_texts.append(budget_candidates[source_id]["text"])
                packet, payload = trial_packet, trial_payload
                item_kind = budget_candidates[source_id]["kind"]
                reason = f"{item_kind} selected by weighted MMR within text budget"
                decide(source, True, reason, breakdown)
                current_tokens = counter.count(packet.content)
            else:
                decide(source, False, REASON_OVER_TEXT_BUDGET, breakdown)
    else:
        selected_ids.update(budget_candidates)
        payload = assemble_payload(selected_ids)
        packet = make_packet(payload)
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
        extra_budgets=(
            None
            if context_limits is None
            else {
                "context_limits": {
                    "max_premises": context_limits.max_premises,
                    "max_prior_runs": context_limits.max_prior_runs,
                    "max_transpositions": context_limits.max_transpositions,
                }
            }
        ),
        selection_policy_configs={selection_policy.name: selection_policy.manifest_config(
            cost_normalization=(
                "item caps: lexical text length / maximum candidate text length; "
                "text budget: marginal full-envelope counter tokens / text_budget.max_tokens"
            ),
        )},
        token_counter_config=token_counter_manifest(counter),
    )
    return FormalCompilation(payload=payload, packet=packet, result=result)
