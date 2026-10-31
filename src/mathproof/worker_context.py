"""Worker-specific context adapters (Section 9.3 / Context Compilation).

Workers never see the whole workspace: the compiler projects committed state
into the narrow packet a role needs, and stamps every packet with a manifest
digest so what a worker was told stays reproducible.

Research packets use the shared budget-aware compiler in ``context_compiler``.
Other worker contexts remain specific to their gate operations.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from commit_gate.canon import content_hash
from commit_gate.state import ReadView, belongs_to_proof
from .actions import STATEMENT_CHECK, statement_check_action
from .scheduler import Lease
from .review_fingerprint import critic_input_digest

__all__ = [
    "compile_context",
    "compile_formal_request",
]


class _NoArtifacts:
    def exists(self, artifact_hash: str) -> bool:
        return False

    def get(self, artifact_hash: str) -> bytes:
        raise KeyError(artifact_hash)


def compile_context(lease: Lease, view: ReadView) -> dict[str, Any]:
    """The packet for a non-formal worker class, keyed by its role."""
    if lease.worker_class == "formalizer":
        return _formalizer_packet(lease, view)
    if lease.worker_class == "alignment-reviewer":
        return _alignment_packet(lease, view)
    if lease.worker_class == "experiment":
        return _statement_check_packet(lease, view)
    if lease.worker_class == "critic":
        return _critic_packet(lease, view)
    if lease.worker_class in ("llm-research", "hyperon"):
        return _research_packet(lease, view)
    raise ValueError(f"no context compiler for {lease.worker_class!r}")


def manifest_digest(packet: Mapping[str, Any]) -> str:
    return content_hash(dict(packet))


def _formalizer_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    claim = view.node(lease.selected_move_id)
    if claim is None or claim.label != "Claim" or not belongs_to_proof(claim.node_id, lease.proof_id):
        raise ValueError("leased formalization target is not a claim in this proof")
    dependencies = _related(view, claim.node_id, "DEPENDS_ON", lease.proof_id)
    if any(item["fields"].get("status") != "lean-verified" for item in dependencies):
        raise ValueError("formalizer requires verified dependencies")
    packet: dict[str, Any] = {
        "role": "formalizer",
        "proof_id": lease.proof_id,
        "compiler_version": "formalizer-v1",
        "revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "claim": _node_payload(claim),
        "dependencies": dependencies,
        "theorem": _theorem_kernel(lease.proof_id, view, fallback_claim_ids=(claim.node_id,)),
    }
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


def _alignment_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    declaration = view.node(lease.selected_move_id)
    if declaration is None or declaration.label != "FormalDeclaration" or not belongs_to_proof(declaration.node_id, lease.proof_id):
        raise ValueError("leased alignment target is not a declaration in this proof")
    targets = [edge.dst_id for edge in view.edges_from(declaration.node_id, "DRAFTS_CLAIM")]
    if len(targets) != 1 or not belongs_to_proof(targets[0], lease.proof_id):
        raise ValueError("draft declaration needs exactly one claim")
    claim = view.node(targets[0])
    if claim is None or claim.label != "Claim":
        raise ValueError("draft declaration's claim is missing")
    packet: dict[str, Any] = {
        "role": "alignment",
        "proof_id": lease.proof_id,
        "compiler_version": "formalizer-v1",
        "revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "claim": _node_payload(claim),
        "declaration": _node_payload(declaration),
    }
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


def _draft_declarations(view: ReadView, claim_ids: Iterable[str], proof_id: str) -> list[dict[str, Any]]:
    records = {}
    for claim_id in claim_ids:
        if not belongs_to_proof(claim_id, proof_id):
            continue
        for edge in view.edges_to(claim_id, "DRAFTS_CLAIM"):
            if not belongs_to_proof(edge.src_id, proof_id):
                continue
            record = view.node(edge.src_id)
            if (record is not None and record.label == "FormalDeclaration"
                    and record.fields.get("status") in {"draft", "aligned"}
                    and record.fields.get("lean_type") and record.fields.get("imports")):
                records[record.node_id] = _node_payload(record)
    return [records[node_id] for node_id in sorted(records)]


def _statement_check_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    owner = view.node(lease.selected_move_id)
    if owner is None or owner.label not in {"ResearchMove", "Attempt"} or not belongs_to_proof(owner.node_id, lease.proof_id):
        raise ValueError("statement check lease has no eligible action owner")
    if owner.fields.get("action_result_id"):
        raise ValueError("statement check action is already complete")
    raw = owner.fields.get("action" if owner.label == "ResearchMove" else "follow_up")
    targets = view.edges_from(owner.node_id, "MOVE_TARGETS" if owner.label == "ResearchMove" else "REVIEWS_CLAIM")
    claim_ids = {edge.dst_id for edge in targets if belongs_to_proof(edge.dst_id, lease.proof_id)}
    declarations = _draft_declarations(view, claim_ids, lease.proof_id)
    action = statement_check_action(raw, {item["id"] for item in declarations})
    declaration = next(item for item in declarations if item["id"] == action["declaration_id"])
    drafted = {edge.dst_id for edge in view.edges_from(declaration["id"], "DRAFTS_CLAIM")}
    matching = sorted(claim_ids & drafted)
    if len(matching) != 1:
        raise ValueError("statement check action is not bound to one target claim")
    claim = view.node(matching[0])
    packet: dict[str, Any] = {
        "role": STATEMENT_CHECK,
        "proof_id": lease.proof_id,
        "compiler_version": "statement-check-v1",
        "revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "owner": _node_payload(owner),
        "claim": _node_payload(claim),
        "declaration": declaration,
        "action": action,
    }
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


# -- research packet (Explorer) ---------------------------------------------


def _research_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    """Compile the selected move with the shared, budget-aware compiler."""
    from context_compiler import CompileRequest, PacketKind, TextBudget
    from context_compiler.research import ResearchTask, compile_research
    from context_compiler.snapshot import ProofSnapshot
    from context_compiler.sources import RetrievalPlan, collect_sources
    from shared.vocab import WorkerClass

    move = view.node(lease.selected_move_id)
    if move is None or move.label != "ResearchMove" or not belongs_to_proof(move.node_id, lease.proof_id):
        raise ValueError("leased research move is outside the proof or has the wrong label")
    parents = [e.src_id for e in view.edges_to(move.node_id, "PROPOSES") if belongs_to_proof(e.src_id, lease.proof_id)]
    if len(parents) != 1 or view.node(parents[0]) is None:
        raise ValueError("research move needs exactly one committed parent state")
    targets = [e.dst_id for e in view.edges_from(move.node_id, "MOVE_TARGETS") if belongs_to_proof(e.dst_id, lease.proof_id)]
    if len(targets) != 1:
        raise ValueError("research move needs exactly one theorem target")
    request = CompileRequest(
        task_id=lease.lease_id, proof_id=lease.proof_id,
        base_revision=lease.base_revision, packet_kind=PacketKind.RESEARCH,
        worker_class=WorkerClass.LLM_RESEARCH, text_budget=TextBudget(4000),
    )
    collected = collect_sources(
        request, ProofSnapshot(lease.proof_id, lease.base_revision, view),
        _NoArtifacts(), RetrievalPlan(targets[0], seed_ids=(parents[0], move.node_id)),
    )
    if collected.kernel is None or collected.bundle is None or collected.missing:
        raise ValueError(f"research context is incomplete: {collected.missing}")
    kernel = collected.kernel
    if not kernel.assumptions:
        proofs = [
            view.node(edge.src_id)
            for edge in view.edges_to(targets[0], "HAS_TARGET")
            if belongs_to_proof(edge.src_id, lease.proof_id)
        ]
        if len(proofs) == 1 and proofs[0] is not None and proofs[0].label == "Proof":
            assumptions = proofs[0].fields.get("assumptions", ())
            if isinstance(assumptions, (list, tuple)) and all(
                isinstance(item, str) and item.strip() for item in assumptions
            ):
                kernel = replace(kernel, assumptions=tuple(assumptions))
    compilation = compile_research(
        request, collected.bundle, collected,
        ResearchTask(
            kernel, str(lease.base_revision), parents[0], move.node_id,
            "Investigate the leased move without changing the theorem target.",
            "JSON OR alternatives with optional AND subgoals",
            "Only propose testable next steps.",
        ),
    )
    included = {d.source_id for d in compilation.result.selection_decisions if d.included}
    claim_ids = sorted(
        ref.source_id for ref in collected.bundle.sources
        if ref.source_type == "claim" and ref.source_id in included
    )
    return {
        "role": "research", "proof_id": lease.proof_id,
        "revision": lease.base_revision, "lease_id": lease.lease_id,
        "move": {"id": move.node_id},
        "parent_state": {
            "id": parents[0],
            "branch_depth": view.node(parents[0]).fields.get("branch_depth", 0),
        },
        "theorem": {
            "target_claims": [{"id": targets[0]}],
            "statement": kernel.statement,
        },
        "claims": [{"id": claim_id} for claim_id in claim_ids],
        "declarations": [{"id": item["id"]} for item in _draft_declarations(view, targets, lease.proof_id)],
        "context_text": compilation.packet.content,
        "manifest_digest": compilation.packet_digest,
    }


def _critic_packet(lease: Lease, view: ReadView) -> dict[str, Any]:
    claim = view.node(lease.selected_move_id)
    if claim is not None and (
        claim.label != "Claim" or not belongs_to_proof(claim.node_id, lease.proof_id)
    ):
        raise ValueError("leased critic claim is outside the proof or has the wrong label")
    packet: dict[str, Any] = {
        "role": "critic",
        "proof_id": lease.proof_id,
        "compiler_version": "research-critic-v1",
        "revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "claim": _node_payload(claim),
        "input_digest": critic_input_digest(view, lease.selected_move_id),
        "dependencies": [],
        "evidence": [],
        "prior_reviews": [],
        "alignments": [],
        "declarations": [],
        "theorem": _theorem_kernel(
            lease.proof_id, view, fallback_claim_ids=(lease.selected_move_id,)
        ),
    }
    if claim is not None:
        packet["dependencies"] = _related(view, claim.node_id, "DEPENDS_ON", lease.proof_id)
        packet["prior_reviews"] = _related(
            view, claim.node_id, "REVIEWS_CLAIM", lease.proof_id, incoming=True
        )
        packet["alignments"] = _related(
            view, claim.node_id, "ALIGNS_CLAIM", lease.proof_id, incoming=True
        )
        packet["declarations"] = _draft_declarations(view, (claim.node_id,), lease.proof_id)
        for edge_type in ("PROVED_BY", "RESOLVES"):
            for edge in view.edges_from(claim.node_id, edge_type):
                if belongs_to_proof(edge.dst_id, lease.proof_id):
                    record = view.node(edge.dst_id)
                    if record is not None:
                        packet["evidence"].append({"rel": edge_type, **_node_payload(record)})
        for edge in view.edges_to(claim.node_id, "CHECKS_CLAIM"):
            if belongs_to_proof(edge.src_id, lease.proof_id):
                record = view.node(edge.src_id)
                if record is not None and record.label == "Experiment":
                    packet["evidence"].append({"rel": "CHECKS_CLAIM", **_node_payload(record)})
    packet["evidence"].sort(key=lambda item: (item["rel"], item["id"]))
    packet["manifest_digest"] = manifest_digest(packet)
    return packet


def _theorem_kernel(
    proof_id: str, view: ReadView, *, fallback_claim_ids: Iterable[str] = ()
) -> dict[str, Any]:
    """Return only committed theorem targets and declarations from this proof."""
    proofs = view.list_nodes(proof_id, "Proof")
    target_ids = {
        edge.dst_id
        for proof in proofs
        for edge in view.edges_from(proof.node_id, "HAS_TARGET")
        if belongs_to_proof(edge.dst_id, proof_id)
    }
    if not target_ids:
        target_ids.update(
            claim_id for claim_id in fallback_claim_ids
            if belongs_to_proof(claim_id, proof_id)
        )
    targets = [
        _node_payload(record)
        for node_id in sorted(target_ids)
        if (record := view.node(node_id)) is not None and record.label == "Claim"
    ]
    aligned_ids = set()
    for target in targets:
        for edge in view.edges_to(target["id"], "ALIGNS_CLAIM"):
            if not belongs_to_proof(edge.src_id, proof_id):
                continue
            alignment = view.node(edge.src_id)
            if alignment is None or alignment.fields.get("verdict") != "aligned":
                continue
            for declaration_edge in view.edges_from(alignment.node_id, "ALIGNS_DECLARATION"):
                if belongs_to_proof(declaration_edge.dst_id, proof_id):
                    aligned_ids.add(declaration_edge.dst_id)
    declarations = [
        _node_payload(record)
        for declaration_id in sorted(aligned_ids)
        if (record := view.node(declaration_id)) is not None
        and record.label == "FormalDeclaration"
    ]
    return {
        "proofs": [_node_payload(record) for record in proofs],
        "target_claims": targets,
        "aligned_declarations": declarations,
    }


def _related(
    view: ReadView, node_id: str, rel_type: str, proof_id: str, *, incoming: bool = False
) -> list[dict[str, Any]]:
    edges = view.edges_to(node_id, rel_type) if incoming else view.edges_from(node_id, rel_type)
    records = []
    for edge in edges:
        related_id = edge.src_id if incoming else edge.dst_id
        if belongs_to_proof(related_id, proof_id):
            record = view.node(related_id)
            if record is not None:
                records.append(_node_payload(record))
    return sorted(records, key=lambda item: item["id"])


# -- formal request -----------------------------------------------------------


def compile_formal_request(
    lease: Lease,
    view: ReadView,
    *,
    run_id: str | None = None,
    search_policy: str = "gnn-pln-best-first-v1",
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A formal-search-request for the leased declaration or resumable run.

    Reads only committed state: the declaration's Lean text, the pinned
    environment, and the run identity. `run_id` must be provided for a fresh
    declaration; a leased `fr-` move resumes that run as-is.
    """
    target_id = lease.selected_move_id
    target = view.node(target_id)
    if target is None:
        raise ValueError(f"leased formal target {target_id!r} is not committed")

    if target.label == "FormalRun":
        run_id = target_id
        searched = view.edges_from(run_id, "SEARCHES")
        if not searched:
            raise ValueError(f"run {run_id!r} commits no SEARCHES edge")
        declaration_id = searched[0].dst_id
        declaration = view.node(declaration_id)
    elif target.label == "FormalDeclaration":
        if run_id is None:
            raise ValueError("a fresh declaration needs a minted run_id")
        declaration_id = target_id
        declaration = target
    else:
        raise ValueError(
            f"{target_id!r} is a {target.label}, not a formal-run target"
        )

    environment = _pinned_environment(view, declaration_id)
    if view.edges_from(declaration_id, "DRAFTS_CLAIM") and not environment["environment_hash"]:
        raise ValueError("formalized declaration has no pinned Lean environment")

    request: dict[str, Any] = {
        "proof_id": lease.proof_id,
        "claim_id": _aligned_claim(view, declaration_id),
        "formal_declaration_id": declaration_id,
        "run_id": run_id,
        "base_revision": lease.base_revision,
        "lease_id": lease.lease_id,
        "fencing_token": lease.fencing_token,
        "lean_source_artifact": (declaration.fields or {}).get(
            "lean_value", (declaration.fields or {}).get("statement_source", "")
        ),
        "environment_id": environment["environment_id"],
        "environment_hash": environment["environment_hash"],
        "goal_text": (declaration.fields or {}).get("lean_type", ""),
        "search_policy": search_policy,
    }
    if budget is not None:
        request["budget"] = dict(budget)
    return request


def _aligned_claim(view: ReadView, declaration_id: str) -> str | None:
    for align_edge in view.edges_to(declaration_id, "ALIGNS_DECLARATION"):
        alignment = view.node(align_edge.src_id)
        if alignment is None:
            continue
        for claim_edge in view.edges_from(alignment.node_id, "ALIGNS_CLAIM"):
            return claim_edge.dst_id
    return None


def _pinned_environment(view: ReadView, declaration_id: str) -> dict[str, Any]:
    for edge in view.edges_from(declaration_id, "PINNED_ENVIRONMENT"):
        env = view.node(edge.dst_id)
        if env is not None:
            fields = dict(env.fields or {})
            return {
                "environment_id": env.node_id,
                "environment_hash": fields.get(
                    "lake_manifest_hash", ""
                ),
                "toolchain": fields.get("toolchain"),
            }
    return {"environment_id": "", "environment_hash": "", "toolchain": None}


# -- shared helpers -----------------------------------------------------------


def _node_payload(record) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "id": record.node_id,
        "label": record.label,
        "fields": dict(record.fields or {}),
    }
