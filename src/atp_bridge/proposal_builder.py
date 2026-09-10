"""ProposalBuilder: Translates formal proof-search results / hypergraphs into CommitGate Proposals.

Implements Invariant 2 (subgoal conservation) and proper namespaces, vocabulary,
and concurrency tokens according to the Technical Design (§6, §11, Appendix B).
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from commit_gate.ops import AddEdge, Op, SetField, UpsertNode
from commit_gate.proposal import Proposal
from shared.vocab import (
    ExecutorResult,
    FormalStateStatus,
    TacticStatus,
    WorkerClass,
)

from .request import FormalSearchRequest

__all__ = [
    "ProposalBuilder",
    "build_proposal_from_search",
]


def _hash_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class ProposalBuilder:
    """Builds a race-safe, validated Proposal from proof search outputs."""

    def __init__(self, request: FormalSearchRequest, actor: str = "formal-atp-worker") -> None:
        self.request = request
        self.actor = actor
        self.proof_id = request.proof_id
        self.run_id = request.run_id
        self._ops: list[Op] = []

    def _state_id(self, local_node_id: int | str) -> str:
        return f"{self.proof_id}/{self.run_id}/fs_{local_node_id}"

    def _tactic_id(self, local_edge_id: int | str) -> str:
        return f"{self.proof_id}/{self.run_id}/ta_{local_edge_id}"

    def add_formal_state(
        self,
        node_id: int | str,
        goal_text: str,
        depth: int = 0,
        status: str = FormalStateStatus.OPEN.value,
        prior_status: Any = None,
        exact_hash: str | None = None,
        gnn_probability: float | None = None,
        pln_strength: float | None = None,
        pln_confidence: float | None = None,
    ) -> str:
        """Add a FormalState node upsert, status set, and optional score annotations."""
        fs_id = self._state_id(node_id)
        if exact_hash is None:
            exact_hash = _hash_text(goal_text)

        # 1. Upsert node with immutable fields
        self._ops.append(
            UpsertNode(
                label="FormalState",
                node_id=fs_id,
                fields={
                    "goal_text": goal_text,
                    "exact_hash": exact_hash,
                },
            )
        )

        # 2. Set mutable status (prior=None expects null for new field)
        mapped_status = self._map_state_status(status)
        self._ops.append(
            SetField(
                label="FormalState",
                node_id=fs_id,
                field="status",
                value=mapped_status,
                prior=prior_status,
            )
        )

        # 3. Add annotation score fields
        if depth is not None:
            self._ops.append(
                SetField(
                    label="FormalState",
                    node_id=fs_id,
                    field="depth",
                    value=depth,
                )
            )
        if gnn_probability is not None:
            self._ops.append(
                SetField(
                    label="FormalState",
                    node_id=fs_id,
                    field="gnn_tactic_prior",
                    value=float(gnn_probability),
                )
            )
        if pln_strength is not None:
            self._ops.append(
                SetField(
                    label="FormalState",
                    node_id=fs_id,
                    field="pln_strength",
                    value=float(pln_strength),
                )
            )
        if pln_confidence is not None:
            self._ops.append(
                SetField(
                    label="FormalState",
                    node_id=fs_id,
                    field="pln_confidence",
                    value=float(pln_confidence),
                )
            )

        return fs_id

    def add_tactic_application(
        self,
        edge_id: int | str,
        source_node_id: int | str,
        tactic_name: str,
        child_node_ids: Sequence[int | str],
        executor_result: str = ExecutorResult.LEAN_ACCEPTED.value,
        status: str = TacticStatus.PENDING.value,
        prior_status: Any = None,
        arguments: Sequence[str] = (),
        tactic_probability: float | None = None,
    ) -> str:
        """Add a TacticApplication node and all child FORMAL_REQUIRES edges.
        
        Enforces Invariant 2: subgoal_count equals len(child_node_ids).
        """
        ta_id = self._tactic_id(edge_id)
        src_fs_id = self._state_id(source_node_id)
        child_count = len(child_node_ids)

        tactic_family = tactic_name.strip().split()[0] if tactic_name else "tactic"
        mapped_executor_result = self._map_executor_result(executor_result)

        # 1. Upsert TacticApplication with immutable fields
        self._ops.append(
            UpsertNode(
                label="TacticApplication",
                node_id=ta_id,
                fields={
                    "tactic_label": tactic_name,
                    "tactic_family": tactic_family,
                    "subgoal_count": child_count,  # Exact conservation check
                    "executor_result": mapped_executor_result,
                },
            )
        )

        # 2. Set mutable status (prior=None expects null for new field)
        mapped_status = self._map_tactic_status(status)
        self._ops.append(
            SetField(
                label="TacticApplication",
                node_id=ta_id,
                field="status",
                value=mapped_status,
                prior=prior_status,
            )
        )

        if tactic_probability is not None:
            self._ops.append(
                SetField(
                    label="TacticApplication",
                    node_id=ta_id,
                    field="gnn_tactic_prior",
                    value=float(tactic_probability),
                )
            )

        # 3. Add HAS_TACTIC edge from parent state -> tactic application
        self._ops.append(
            AddEdge(
                rel_type="HAS_TACTIC",
                src_id=src_fs_id,
                dst_id=ta_id,
                edge_id=f"{self.proof_id}/{self.run_id}/e_hastactic_{edge_id}",
            )
        )

        # 4. Add FORMAL_REQUIRES edges for EVERY child (subgoal conservation)
        for idx, child_node_id in enumerate(child_node_ids):
            child_fs_id = self._state_id(child_node_id)
            self._ops.append(
                AddEdge(
                    rel_type="FORMAL_REQUIRES",
                    src_id=ta_id,
                    dst_id=child_fs_id,
                    edge_id=f"{self.proof_id}/{self.run_id}/e_req_{edge_id}_{idx}",
                    fields={"child_index": idx},
                )
            )

        # 5. If child_count == 0 and status is closed/solved, add CLOSES_STATE edge
        if child_count == 0 and mapped_status in (TacticStatus.CLOSED.value, TacticStatus.OPEN.value):
            self._ops.append(
                AddEdge(
                    rel_type="CLOSES_STATE",
                    src_id=ta_id,
                    dst_id=src_fs_id,
                    edge_id=f"{self.proof_id}/{self.run_id}/e_closes_{edge_id}",
                )
            )

        return ta_id

    def build(self) -> Proposal:
        """Construct the final Proposal carrying all ops and request concurrency tokens."""
        return Proposal(
            proof_id=self.proof_id,
            actor=self.actor,
            worker_class=WorkerClass.FORMAL_ATP.value,
            ops=tuple(self._ops),
            base_revision=self.request.base_revision,
            lease_id=self.request.lease_id,
            fencing_token=self.request.fencing_token,
        )

    @staticmethod
    def _map_state_status(raw: str) -> str:
        mapping = {
            "open": FormalStateStatus.OPEN.value,
            "expanded": FormalStateStatus.EXPANDED.value,
            "solved": FormalStateStatus.FORMALLY_CLOSED.value,
            "formally-closed": FormalStateStatus.FORMALLY_CLOSED.value,
            "lean-verified": FormalStateStatus.LEAN_VERIFIED.value,
            "dead": FormalStateStatus.FAILED.value,
            "failed": FormalStateStatus.FAILED.value,
            "pruned": FormalStateStatus.PRUNED.value,
            "stale": FormalStateStatus.STALE.value,
        }
        return mapping.get(raw, raw)

    @staticmethod
    def _map_tactic_status(raw: str) -> str:
        mapping = {
            "pending": TacticStatus.PENDING.value,
            "open": TacticStatus.OPEN.value,
            "solved": TacticStatus.CLOSED.value,
            "closed": TacticStatus.CLOSED.value,
            "dead": TacticStatus.DEAD.value,
            "refuted": TacticStatus.REFUTED.value,
        }
        return mapping.get(raw, raw)

    @staticmethod
    def _map_executor_result(raw: str) -> str:
        mapping = {
            "lean-accepted": ExecutorResult.LEAN_ACCEPTED.value,
            "success": ExecutorResult.LEAN_ACCEPTED.value,
            "lean-rejected": ExecutorResult.LEAN_REJECTED.value,
            "failed": ExecutorResult.LEAN_REJECTED.value,
            "timeout": ExecutorResult.TIMEOUT.value,
            "backend-missing": ExecutorResult.BACKEND_MISSING.value,
            "parse-failure": ExecutorResult.PARSE_FAILURE.value,
            "crash": ExecutorResult.CRASH.value,
            "empty-output": ExecutorResult.EMPTY_OUTPUT.value,
        }
        return mapping.get(raw, raw)


def build_proposal_from_search(
    request: FormalSearchRequest,
    nodes: dict[int | str, Any] | Sequence[Any],
    edges: dict[int | str, Any] | Sequence[Any],
    actor: str = "formal-atp-worker",
) -> Proposal:
    """Convenience helper to convert hypergraph nodes & edges dict/list into a Proposal."""
    builder = ProposalBuilder(request, actor=actor)

    # Normalize nodes to iterable
    node_list = nodes.values() if isinstance(nodes, dict) else nodes
    for node in node_list:
        node_id = getattr(node, "id", None) if hasattr(node, "id") else node.get("id")
        goal_obj = getattr(node, "goal", None) if hasattr(node, "goal") else node.get("goal")

        if hasattr(goal_obj, "expression"):
            goal_text = goal_obj.expression
        elif isinstance(goal_obj, dict):
            goal_text = goal_obj.get("expression", str(goal_obj))
        else:
            goal_text = str(goal_obj or "")

        depth = getattr(node, "depth", 0) if hasattr(node, "depth") else node.get("depth", 0)
        status = getattr(node, "status", "open") if hasattr(node, "status") else node.get("status", "open")
        gnn_prob = getattr(node, "gnn_probability", None) if hasattr(node, "gnn_probability") else node.get("gnn_probability")

        stv = getattr(node, "stv", None) if hasattr(node, "stv") else node.get("stv")
        pln_strength = getattr(stv, "strength", None) if stv is not None else None
        pln_confidence = getattr(stv, "confidence", None) if stv is not None else None

        builder.add_formal_state(
            node_id=node_id,
            goal_text=goal_text,
            depth=depth,
            status=status,
            gnn_probability=gnn_prob,
            pln_strength=pln_strength,
            pln_confidence=pln_confidence,
        )

    # Normalize edges to iterable
    edge_list = edges.values() if isinstance(edges, dict) else edges
    for edge in edge_list:
        edge_id = getattr(edge, "id", None) if hasattr(edge, "id") else edge.get("id")
        source_id = getattr(edge, "source_id", None) if hasattr(edge, "source_id") else edge.get("source_id")

        if source_id is None:
            continue  # Skip root incoming dummy edge if any

        tactic_obj = getattr(edge, "tactic", None) if hasattr(edge, "tactic") else edge.get("tactic")
        if hasattr(tactic_obj, "tactic_name"):
            tactic_name = tactic_obj.tactic_name
            arguments = getattr(tactic_obj, "arguments", ())
            probability = getattr(tactic_obj, "probability", None)
        elif isinstance(tactic_obj, dict):
            tactic_name = tactic_obj.get("tactic_name", "")
            arguments = tactic_obj.get("arguments", ())
            probability = tactic_obj.get("probability")
        else:
            tactic_name = str(tactic_obj or "")
            arguments = ()
            probability = None

        child_ids = getattr(edge, "child_ids", ()) if hasattr(edge, "child_ids") else edge.get("child_ids", ())
        status = getattr(edge, "status", "pending") if hasattr(edge, "status") else edge.get("status", "pending")

        builder.add_tactic_application(
            edge_id=edge_id,
            source_node_id=source_id,
            tactic_name=tactic_name,
            child_node_ids=child_ids,
            status=status,
            arguments=arguments,
            tactic_probability=probability,
        )

    return builder.build()
