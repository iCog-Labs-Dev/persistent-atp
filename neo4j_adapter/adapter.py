from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from neo4j import GraphDatabase, ManagedTransaction

load_dotenv()

from .constants import (
    ATTEMPT_PENDING,
    ATTEMPT_STATUSES,
    CLAIM_CONJECTURAL,
    CLAIM_STATUSES,
    MOVE_QUEUED,
    MOVE_STATUSES,
    STATE_KIND_AND,
    STATE_KIND_OR,
    STATE_KINDS,
    STATE_OPEN,
    STATE_STATUSES,
    _check,
    _edge_id,
)
from .rules import RulesMixin
from .replay import ReplayMixin
from .schema import REL_WHITELIST, ensure_constraints
from .session import Statement, TransactionMixin, translate_errors


class Neo4jAdapter(TransactionMixin, ReplayMixin, RulesMixin):
    """A Neo4j projection of the paper's MORK-backed PeTTa metagraph.

    Three linked DAGs (search / justification / provenance) plus a speculative
    hypothesis layer, all scoped by proof_id (one proof = one namespace).

    Neo4j is the semantic and query authority only. The append-only journal
    (SQL / JSONL) is the durability authority — this graph can always be wiped
    and rebuilt by replay (see wipe_and_rebuild).

    Every method below is one managed transaction, so a multi-statement
    operation never half-applies, and driver failures are raised as
    :class:`~neo4j.errors.GraphError` subclasses.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
        uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = user or os.environ.get("NEO4J_USER", "neo4j")
        if password is None:
            password = os.environ.get("NEO4J_PASSWORD", "")
        self._database = database or os.environ.get("NEO4J_DATABASE") or None
        with translate_errors("connect"):
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            # Fail here, with a GraphUnavailable, rather than on the first write.
            self._driver.verify_connectivity()
        ensure_constraints(self._driver, self._database)

    def close(self) -> None:
        with translate_errors("close"):
            self._driver.close()

    def __enter__(self) -> "Neo4jAdapter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Proof node — one per theorem project (namespace anchor)
    # ------------------------------------------------------------------

    def init_proof(
        self,
        proof_id: str,
        theorem_kernel: str,
        theorem_hash: str = "",
        event_id: str = "",
    ) -> None:
        self._write_all("init_proof", [(
            "MERGE (p:Proof {proof_id: $pid, id: $pid}) "
            "ON CREATE SET p.theorem_kernel = $k, p.theorem_hash = $h, "
            "              p.active_revision = 0, p.created_in_event = $evt "
            "ON MATCH SET p.theorem_kernel = $k, p.theorem_hash = $h",
            {
                "pid": proof_id,
                "k": theorem_kernel,
                "h": theorem_hash,
                "evt": event_id,
            },
        )])

    # ------------------------------------------------------------------
    # States (search DAG — OR point)
    # ------------------------------------------------------------------

    def add_state(
        self,
        proof_id: str,
        state_id: str,
        description: str,
        parent_id: Optional[str] = None,
        kind: str = STATE_KIND_OR,
        assumptions: str = "",
        event_id: str = "",
    ) -> None:
        kind = _check(kind, STATE_KINDS, "state kind")
        statements: List[Statement] = [
            (
                "MERGE (st:State {proof_id: $pid, id: $id}) "
                "ON CREATE SET st.description = $desc, st.status = $open, "
                "              st.kind = $kind, st.assumptions = $ass, "
                "              st.created_in_event = $evt "
                "ON MATCH SET st.description = $desc, st.kind = $kind",
                {
                    "pid": proof_id, "id": state_id, "desc": description,
                    "open": STATE_OPEN, "kind": kind, "ass": assumptions,
                    "evt": event_id,
                },
            ),
            (
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (st:State {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_STATE {edge_id: $eid}]->(st) "
                "ON CREATE SET st.description = $desc, st.status = $open, st.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "id": state_id, "desc": description,
                    "open": STATE_OPEN, "eid": _edge_id(event_id, "HAS_STATE"),
                    "evt": event_id,
                },
            ),
        ]
        if parent_id:
            statements.append((
                "MATCH (child:State {proof_id: $pid, id: $cid}), "
                "(parent:State {proof_id: $pid, id: $pid2}) "
                "MERGE (child)-[r:CHILD_OF {edge_id: $eid}]->(parent) "
                "ON CREATE SET child.status = $open, child.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "cid": state_id, "pid2": parent_id,
                    "open": STATE_OPEN, "eid": _edge_id(event_id, "CHILD_OF"),
                    "evt": event_id,
                },
            ))
        self._write_all("add_state", statements)

    def get_state(self, state_id: str, proof_id: str = "") -> Optional[Dict[str, Any]]:
        return self._read_one(
            "get_state",
            "MATCH (st:State {id: $id}) "
            "WHERE $pid = '' OR st.proof_id = $pid "
            "RETURN st",
            {"id": state_id, "pid": proof_id},
            "st",
        )

    def update_state_status(
        self,
        proof_id: str,
        state_id: str,
        status: str,
        reason: str = "",
        event_id: str = "",
    ) -> None:
        status = _check(status, STATE_STATUSES, "state status")
        self._write_all("update_state_status", [(
            "MATCH (st:State {proof_id: $pid, id: $id}) "
            "SET st.status = $status, st.status_updated_in_event = $evt, "
            "    st.closed_reason = $reason",
            {
                "pid": proof_id, "id": state_id, "status": status,
                "evt": event_id, "reason": reason,
            },
        )])

    # ------------------------------------------------------------------
    # Claims (justification DAG)
    # ------------------------------------------------------------------

    def add_claim(
        self,
        proof_id: str,
        claim_id: str,
        statement: str,
        status: str = CLAIM_CONJECTURAL,
        statement_blob: str = "",
        event_id: str = "",
    ) -> None:
        status = _check(status, CLAIM_STATUSES, "claim status")
        self._write_all("add_claim", [
            (
                "MERGE (c:Claim {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.statement = $stmt, c.status = $status, "
                "              c.statement_blob = $blob, c.created_in_event = $evt "
                "ON MATCH SET c.statement = $stmt, c.status = $status",
                {
                    "pid": proof_id, "id": claim_id, "stmt": statement,
                    "status": status, "blob": statement_blob, "evt": event_id,
                },
            ),
            (
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (c:Claim {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_CLAIM {edge_id: $eid}]->(c) "
                "ON CREATE SET c.statement = $stmt, c.status = $status, c.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "id": claim_id, "stmt": statement,
                    "status": status, "eid": _edge_id(event_id, "HAS_CLAIM"),
                    "evt": event_id,
                },
            ),
        ])

    def update_claim_status(
        self,
        claim_id: str,
        status: str,
        proof_id: str = "",
        event_id: str = "",
        reason: str = "",
    ) -> None:
        status = _check(status, CLAIM_STATUSES, "claim status")
        self._write_all("update_claim_status", [(
            "MATCH (c:Claim {id: $id}) "
            "WHERE $pid = '' OR c.proof_id = $pid "
            "SET c.status = $status, c.status_updated_in_event = $evt, "
            "    c.status_reason = CASE WHEN $reason <> '' "
            "                            THEN $reason ELSE c.status_reason END",
            {
                "id": claim_id, "status": status, "pid": proof_id,
                "evt": event_id, "reason": reason,
            },
        )])

    def get_all_claims(self, proof_id: str) -> List[Dict[str, Any]]:
        return self._read_many(
            "get_all_claims",
            "MATCH (p:Proof {proof_id: $pid, id: $pid})-[:HAS_CLAIM]->(c:Claim) "
            "RETURN c",
            {"pid": proof_id},
            "c",
        )

    def add_claim_dependency(
        self,
        dependent_claim_id: str,
        depends_on_claim_id: str,
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        """DEPENDS_ON edge — raises ValueError if it would create a cycle.

        The acyclicity check and the MERGE share one transaction, so two
        concurrent writers cannot each see an acyclic graph and then close a
        cycle together.
        """
        edge_id = _edge_id(event_id, "DEPENDS_ON")

        def work(tx: ManagedTransaction) -> None:
            if proof_id and _cycle_exists(
                tx, dependent_claim_id, depends_on_claim_id, proof_id
            ):
                raise ValueError(
                    f"Adding DEPENDS_ON {dependent_claim_id} -> {depends_on_claim_id} "
                    f"would create a cycle in the claim dependency graph"
                )
            tx.run(
                "MATCH (a:Claim {id: $a_id}), (b:Claim {id: $b_id}) "
                "WHERE ($pid = '' OR a.proof_id = $pid) "
                "  AND ($pid = '' OR b.proof_id = $pid) "
                "MERGE (a)-[r:DEPENDS_ON {edge_id: $eid}]->(b) "
                "ON CREATE SET a.proof_id = CASE WHEN $pid = '' THEN a.proof_id ELSE $pid END "
                "SET r.event_id = $evt",
                a_id=dependent_claim_id, b_id=depends_on_claim_id,
                pid=proof_id, eid=edge_id, evt=event_id,
            )

        self._write("add_claim_dependency", work)

    def link_state_claim(
        self,
        proof_id: str,
        state_id: str,
        claim_id: str,
        event_id: str = "",
    ) -> None:
        """(:State)-[:USES_CLAIM]->(:Claim) — a state's established/provisional refs."""
        self._write_all("link_state_claim", [(
            "MATCH (st:State {proof_id: $pid, id: $sid}), (c:Claim {proof_id: $pid, id: $cid}) "
            "MERGE (st)-[r:USES_CLAIM {edge_id: $eid}]->(c) "
            "ON CREATE SET st.created_in_event = $evt "
            "SET r.event_id = $evt",
            {
                "pid": proof_id, "sid": state_id, "cid": claim_id,
                "eid": _edge_id(event_id, "USES_CLAIM"), "evt": event_id,
            },
        )])

    # ------------------------------------------------------------------
    # Moves (search DAG — AND point)
    # ------------------------------------------------------------------

    def add_move(
        self,
        proof_id: str,
        move_id: str,
        state_id: str,
        move_summary: str,
        kind: str = "reduction",
        note: str = "",
        event_id: str = "",
        score: Optional[Dict[str, Any]] = None,
        cost_estimate: Optional[str] = None,
        status: str = MOVE_QUEUED,
    ) -> None:
        status = _check(status, MOVE_STATUSES, "move status")
        self._write_all("add_move", [
            (
                "MERGE (m:Move {proof_id: $pid, id: $id}) "
                "ON CREATE SET m.move_summary = $sum, m.kind = $kind, "
                "              m.note = $note, m.status = $status, "
                "              m.score = $score, m.cost_estimate = $cost, "
                "              m.repeated_failure_count = 0, m.created_in_event = $evt "
                "ON MATCH SET m.move_summary = $sum, m.kind = $kind, m.note = $note",
                {
                    "pid": proof_id, "id": move_id, "sum": move_summary,
                    "kind": kind, "note": note, "status": status,
                    "score": score, "cost": cost_estimate, "evt": event_id,
                },
            ),
            (
                "MATCH (st:State {proof_id: $pid, id: $sid}), (m:Move {proof_id: $pid, id: $mid}) "
                "MERGE (st)-[r:PROPOSES {edge_id: $eid}]->(m) "
                "ON CREATE SET m.status = $status, m.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "sid": state_id, "mid": move_id,
                    "eid": _edge_id(event_id, "PROPOSES"), "evt": event_id,
                    "status": status,
                },
            ),
        ])

    def add_required_subgoal(
        self,
        proof_id: str,
        move_id: str,
        subgoal_id: str,
        description: str,
        parent_state_id: Optional[str] = None,
        event_id: str = "",
    ) -> None:
        statements: List[Statement] = [
            (
                "MERGE (st:State {proof_id: $pid, id: $id}) "
                "ON CREATE SET st.description = $desc, st.status = $open, "
                "              st.kind = $kind, st.created_in_event = $evt",
                {
                    "pid": proof_id, "id": subgoal_id, "desc": description,
                    "open": STATE_OPEN, "kind": STATE_KIND_AND, "evt": event_id,
                },
            ),
            (
                "MATCH (p:Proof {proof_id: $pid, id: $pid}), (st:State {proof_id: $pid, id: $id}) "
                "MERGE (p)-[r:HAS_STATE {edge_id: $eid}]->(st) "
                "ON CREATE SET st.description = $desc, st.status = $open, st.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "id": subgoal_id, "desc": description,
                    "open": STATE_OPEN, "eid": _edge_id(event_id, "HAS_STATE"),
                    "evt": event_id,
                },
            ),
            (
                "MATCH (m:Move {proof_id: $pid, id: $mid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (m)-[r:REQUIRES {edge_id: $eid}]->(st) "
                "ON CREATE SET st.status = $open, st.created_in_event = $evt "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "mid": move_id, "sid": subgoal_id,
                    "open": STATE_OPEN, "eid": _edge_id(event_id, "REQUIRES"),
                    "evt": event_id,
                },
            ),
        ]
        if parent_state_id:
            statements.append((
                "MATCH (child:State {proof_id: $pid, id: $cid}), "
                "(parent:State {proof_id: $pid, id: $pid2}) "
                "MERGE (child)-[r:CHILD_OF {edge_id: $eid}]->(parent) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "cid": subgoal_id, "pid2": parent_state_id,
                    "eid": _edge_id(event_id, "CHILD_OF"), "evt": event_id,
                },
            ))
        self._write_all("add_required_subgoal", statements)

    def update_move_status(
        self,
        move_id: str,
        status: str,
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        status = _check(status, MOVE_STATUSES, "move status")
        self._write_all("update_move_status", [(
            "MATCH (m:Move {id: $id}) "
            "WHERE $pid = '' OR m.proof_id = $pid "
            "SET m.status = $status, m.status_updated_in_event = $evt",
            {"id": move_id, "status": status, "pid": proof_id, "evt": event_id},
        )])

    def get_moves_for_state(self, state_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        return self._read_many(
            "get_moves_for_state",
            "MATCH (st:State {id: $sid})-[:PROPOSES]->(m:Move) "
            "WHERE $pid = '' OR m.proof_id = $pid "
            "RETURN m ORDER BY m.id",
            {"sid": state_id, "pid": proof_id},
            "m",
        )

    def get_subgoals_for_move(self, move_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        return self._read_many(
            "get_subgoals_for_move",
            "MATCH (m:Move {id: $mid})-[:REQUIRES]->(st:State) "
            "WHERE $pid = '' OR st.proof_id = $pid "
            "RETURN st ORDER BY st.id",
            {"mid": move_id, "pid": proof_id},
            "st",
        )

    # ------------------------------------------------------------------
    # Attempts (provenance DAG)
    # ------------------------------------------------------------------

    def add_attempt(
        self,
        proof_id: str,
        attempt_id: str,
        state_id: str,
        move_summary: str,
        worker: str = "explorer",
        note: str = "",
        move_id: Optional[str] = None,
        event_id: str = "",
        route_id: Optional[str] = None,
        model_persona: str = "",
        disposition: str = "",
        result_relation: str = "",
    ) -> None:
        statements: List[Statement] = [
            (
                "MERGE (a:Attempt {proof_id: $pid, id: $id}) "
                "ON CREATE SET a.move_summary = $sum, a.worker = $worker, "
                "              a.note = $note, a.status = $pending, "
                "              a.model_persona = $persona, a.disposition = $disp, "
                "              a.result_relation = $rel, a.created_in_event = $evt "
                "ON MATCH SET a.move_summary = $sum, a.worker = $worker, a.note = $note",
                {
                    "pid": proof_id, "id": attempt_id, "sum": move_summary,
                    "worker": worker, "pending": ATTEMPT_PENDING, "note": note,
                    "persona": model_persona, "disp": disposition,
                    "rel": result_relation, "evt": event_id,
                },
            ),
            (
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (a)-[r:ON_STATE {edge_id: $eid}]->(st) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "sid": state_id,
                    "eid": _edge_id(event_id, "ON_STATE"), "evt": event_id,
                },
            ),
        ]
        if move_id:
            statements.append((
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (m:Move {proof_id: $pid, id: $mid}) "
                "MERGE (a)-[r:ON_MOVE {edge_id: $eid}]->(m) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "mid": move_id,
                    "eid": _edge_id(event_id, "ON_MOVE"), "evt": event_id,
                },
            ))
        if route_id:
            statements.append((
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (r:Route {proof_id: $pid, id: $rid}) "
                "MERGE (a)-[r2:VIA_ROUTE {edge_id: $eid}]->(r) "
                "SET r2.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "rid": route_id,
                    "eid": _edge_id(event_id, "VIA_ROUTE"), "evt": event_id,
                },
            ))
        self._write_all("add_attempt", statements)

    def update_attempt(
        self,
        attempt_id: str,
        status: str,
        evidence: str = "",
        proof_id: str = "",
        event_id: str = "",
    ) -> None:
        status = _check(status, ATTEMPT_STATUSES, "attempt status")
        self._write_all("update_attempt", [(
            "MATCH (a:Attempt {id: $id}) "
            "WHERE $pid = '' OR a.proof_id = $pid "
            "SET a.status = $status, a.evidence = $evidence, "
            "    a.status_updated_in_event = $evt",
            {
                "id": attempt_id, "status": status, "evidence": evidence,
                "pid": proof_id, "evt": event_id,
            },
        )])

    def get_attempts_for_state(self, state_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        return self._read_many(
            "get_attempts_for_state",
            "MATCH (a:Attempt)-[:ON_STATE]->(st:State {id: $sid}) "
            "WHERE $pid = '' OR a.proof_id = $pid "
            "RETURN a",
            {"sid": state_id, "pid": proof_id},
            "a",
        )

    def get_attempts_for_move(self, move_id: str, proof_id: str = "") -> List[Dict[str, Any]]:
        return self._read_many(
            "get_attempts_for_move",
            "MATCH (a:Attempt)-[:ON_MOVE]->(m:Move {id: $mid}) "
            "WHERE $pid = '' OR a.proof_id = $pid "
            "RETURN a",
            {"mid": move_id, "pid": proof_id},
            "a",
        )

    def link_attempt_route(self, proof_id: str, attempt_id: str, route_id: str, event_id: str = "") -> None:
        self._write_all("link_attempt_route", [(
            "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (r:Route {proof_id: $pid, id: $rid}) "
            "MERGE (a)-[r2:VIA_ROUTE {edge_id: $eid}]->(r) "
            "SET r2.event_id = $evt",
            {
                "pid": proof_id, "aid": attempt_id, "rid": route_id,
                "eid": _edge_id(event_id, "VIA_ROUTE"), "evt": event_id,
            },
        )])

    def link_attempt_context(self, proof_id: str, attempt_id: str, context_id: str, event_id: str = "") -> None:
        self._write_all("link_attempt_context", [(
            "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (c:Context {proof_id: $pid, id: $cid}) "
            "MERGE (a)-[r:USED_CONTEXT {edge_id: $eid}]->(c) "
            "SET r.event_id = $evt",
            {
                "pid": proof_id, "aid": attempt_id, "cid": context_id,
                "eid": _edge_id(event_id, "USED_CONTEXT"), "evt": event_id,
            },
        )])

    def link_produced_claim(self, proof_id: str, attempt_id: str, claim_id: str, event_id: str = "") -> None:
        self._write_all("link_produced_claim", [(
            "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (c:Claim {proof_id: $pid, id: $cid}) "
            "MERGE (a)-[r:PRODUCED_CLAIM {edge_id: $eid}]->(c) "
            "SET r.event_id = $evt",
            {
                "pid": proof_id, "aid": attempt_id, "cid": claim_id,
                "eid": _edge_id(event_id, "PRODUCED_CLAIM"), "evt": event_id,
            },
        )])

    # ------------------------------------------------------------------
    # Routes, artifacts, contexts (provenance DAG)
    # ------------------------------------------------------------------

    def add_route(self, proof_id: str, route_id: str, display_path: str, event_id: str = "") -> None:
        self._write_all("add_route", [(
            "MERGE (r:Route {proof_id: $pid, id: $id}) "
            "ON CREATE SET r.display_path = $path, r.created_in_event = $evt",
            {"pid": proof_id, "id": route_id, "path": display_path, "evt": event_id},
        )])

    def add_artifact(
        self,
        proof_id: str,
        artifact_id: str,
        kind: str,
        media_type: str = "",
        sha256: str = "",
        filename: str = "",
        event_id: str = "",
    ) -> None:
        self._write_all("add_artifact", [(
            "MERGE (art:Artifact {proof_id: $pid, id: $id}) "
            "ON CREATE SET art.kind = $kind, art.media_type = $media, "
            "              art.sha256 = $sha, art.filename = $fname, "
            "              art.created_in_event = $evt",
            {
                "pid": proof_id, "id": artifact_id, "kind": kind,
                "media": media_type, "sha": sha256, "fname": filename,
                "evt": event_id,
            },
        )])

    def link_artifact(self, proof_id: str, attempt_id: str, artifact_id: str, event_id: str = "") -> None:
        self._write_all("link_artifact", [(
            "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (art:Artifact {proof_id: $pid, id: $aid2}) "
            "MERGE (a)-[r:PRODUCED_ARTIFACT {edge_id: $eid}]->(art) "
            "SET r.event_id = $evt",
            {
                "pid": proof_id, "aid": attempt_id, "aid2": artifact_id,
                "eid": _edge_id(event_id, "PRODUCED_ARTIFACT"), "evt": event_id,
            },
        )])

    def add_context(
        self,
        proof_id: str,
        context_id: str,
        packet_hash: str = "",
        compiler_version: str = "",
        token_budget: int = 0,
        token_count: int = 0,
        event_id: str = "",
    ) -> None:
        self._write_all("add_context", [(
            "MERGE (c:Context {proof_id: $pid, id: $id}) "
            "ON CREATE SET c.packet_hash = $hash, c.compiler_version = $ver, "
            "              c.token_budget = $budget, c.token_count = $count, "
            "              c.created_in_event = $evt",
            {
                "pid": proof_id, "id": context_id, "hash": packet_hash,
                "ver": compiler_version, "budget": token_budget,
                "count": token_count, "evt": event_id,
            },
        )])

    # ------------------------------------------------------------------
    # Critics, experiments, verification (independent checks)
    # ------------------------------------------------------------------

    def add_critique(
        self,
        proof_id: str,
        critique_id: str,
        attempt_id: str,
        verdict: str,
        reason: str = "",
        critic_worker: str = "critic",
        event_id: str = "",
    ) -> None:
        self._write_all("add_critique", [
            (
                "MERGE (cr:Critique {proof_id: $pid, id: $id}) "
                "ON CREATE SET cr.verdict = $verdict, cr.reason = $reason, "
                "              cr.critic_worker = $worker, cr.created_in_event = $evt",
                {
                    "pid": proof_id, "id": critique_id, "verdict": verdict,
                    "reason": reason, "worker": critic_worker, "evt": event_id,
                },
            ),
            (
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (cr:Critique {proof_id: $pid, id: $cid}) "
                "MERGE (a)-[r:HAD_CRITIQUE {edge_id: $eid}]->(cr) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "cid": critique_id,
                    "eid": _edge_id(event_id, "HAD_CRITIQUE"), "evt": event_id,
                },
            ),
        ])

    def add_experiment(
        self,
        proof_id: str,
        experiment_id: str,
        attempt_id: str,
        question: str,
        status: str = "ran",
        event_id: str = "",
    ) -> None:
        self._write_all("add_experiment", [
            (
                "MERGE (e:Experiment {proof_id: $pid, id: $id}) "
                "ON CREATE SET e.question = $q, e.status = $status, "
                "              e.created_in_event = $evt",
                {
                    "pid": proof_id, "id": experiment_id, "q": question,
                    "status": status, "evt": event_id,
                },
            ),
            (
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (e:Experiment {proof_id: $pid, id: $eid}) "
                "MERGE (a)-[r:RAN {edge_id: $edge}]->(e) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "eid": experiment_id,
                    "edge": _edge_id(event_id, "RAN"), "evt": event_id,
                },
            ),
        ])

    def add_verification(
        self,
        proof_id: str,
        verification_id: str,
        attempt_id: str,
        claim_id: str,
        kind: str = "lean",
        status: str = "pending",
        lean_name: str = "",
        toolchain_hash: str = "",
        event_id: str = "",
    ) -> None:
        self._write_all("add_verification", [
            (
                "MERGE (v:Verification {proof_id: $pid, id: $id}) "
                "ON CREATE SET v.kind = $kind, v.status = $status, "
                "              v.lean_name = $lname, v.toolchain_hash = $tool, "
                "              v.created_in_event = $evt",
                {
                    "pid": proof_id, "id": verification_id, "kind": kind,
                    "status": status, "lname": lean_name, "tool": toolchain_hash,
                    "evt": event_id,
                },
            ),
            (
                "MATCH (a:Attempt {proof_id: $pid, id: $aid}), (v:Verification {proof_id: $pid, id: $vid}) "
                "MERGE (a)-[r:HAD_VERIFICATION {edge_id: $eid}]->(v) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "aid": attempt_id, "vid": verification_id,
                    "eid": _edge_id(event_id, "HAD_VERIFICATION"), "evt": event_id,
                },
            ),
            (
                "MATCH (v:Verification {proof_id: $pid, id: $vid}), (c:Claim {proof_id: $pid, id: $cid}) "
                "MERGE (v)-[r:OF {edge_id: $eid}]->(c) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "vid": verification_id, "cid": claim_id,
                    "eid": _edge_id(event_id, "OF"), "evt": event_id,
                },
            ),
        ])

    # ------------------------------------------------------------------
    # Concepts + speculative hypotheses (Hyperon layer)
    # ------------------------------------------------------------------

    def add_concept(
        self,
        proof_id: str,
        concept_id: str,
        name: str,
        mechanism_tags: str = "",
        event_id: str = "",
    ) -> None:
        self._write_all("add_concept", [(
            "MERGE (c:Concept {proof_id: $pid, id: $id}) "
            "ON CREATE SET c.name = $name, c.mechanism_tags = $tags, "
            "              c.created_in_event = $evt",
            {
                "pid": proof_id, "id": concept_id, "name": name,
                "tags": mechanism_tags, "evt": event_id,
            },
        )])

    def add_hypothesis(
        self,
        proof_id: str,
        hypothesis_id: str,
        kind: str,
        target_state_id: str,
        falsification_test: str = "",
        novelty: float = 0.0,
        abductive_strength: float = 0.0,
        cost: float = 0.0,
        risk: float = 0.0,
        lifecycle_status: str = "queued",
        event_id: str = "",
    ) -> None:
        self._write_all("add_hypothesis", [
            (
                "MERGE (h:Hypothesis {proof_id: $pid, id: $id}) "
                "ON CREATE SET h.kind = $kind, h.layer = 'speculative', "
                "              h.falsification_test = $test, h.novelty = $nov, "
                "              h.abductive_strength = $ab, h.cost = $cost, "
                "              h.risk = $risk, h.lifecycle_status = $lc, "
                "              h.created_in_event = $evt",
                {
                    "pid": proof_id, "id": hypothesis_id, "kind": kind,
                    "test": falsification_test, "nov": novelty,
                    "ab": abductive_strength, "cost": cost, "risk": risk,
                    "lc": lifecycle_status, "evt": event_id,
                },
            ),
            (
                "MATCH (h:Hypothesis {proof_id: $pid, id: $hid}), (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (h)-[r:TARGETS {edge_id: $eid}]->(st) "
                "SET r.event_id = $evt",
                {
                    "pid": proof_id, "hid": hypothesis_id, "sid": target_state_id,
                    "eid": _edge_id(event_id, "TARGETS"), "evt": event_id,
                },
            ),
        ])

    def add_relation(
        self,
        proof_id: str,
        rel: str,
        source_id: str,
        target_id: str,
        event_id: str = "",
        route_id: str = "",
    ) -> None:
        """Generic typed relationship linker (whitelisted rel types only)."""
        if rel not in REL_WHITELIST:
            raise ValueError(f"relationship type {rel!r} not in whitelist")
        self._write_all("add_relation", [(
            "MATCH (a {proof_id: $pid, id: $sid}), (b {proof_id: $pid, id: $tid}) "
            f"MERGE (a)-[r:{rel} {{edge_id: $eid}}]->(b) "
            "SET r.event_id = $evt, r.route_id = $rid",
            {
                "pid": proof_id, "sid": source_id, "tid": target_id,
                "eid": _edge_id(event_id, rel), "evt": event_id, "rid": route_id,
            },
        )])

    # ------------------------------------------------------------------
    # Context query
    # ------------------------------------------------------------------

    def context_for(self, proof_id: str, state_id: str) -> Dict[str, Any]:
        moves = self.get_moves_for_state(state_id, proof_id)
        return {
            "state": self.get_state(state_id, proof_id),
            "moves": moves,
            "attempts": self.get_attempts_for_state(state_id, proof_id),
            "claims": self.get_all_claims(proof_id),
            "subgoals": [
                sg
                for move in moves
                for sg in self.get_subgoals_for_move(move["id"], proof_id)
            ],
            "frontier": self.eligible_frontier(proof_id),
        }


def _cycle_exists(
    tx: ManagedTransaction,
    dependent_claim_id: str,
    depends_on_claim_id: str,
    proof_id: str,
) -> bool:
    """True if DEPENDS_ON dependent→depends_on would close a cycle (in-transaction)."""
    record = tx.run(
        "MATCH path = (b:Claim {id: $b_id, proof_id: $pid})"
        "-[:DEPENDS_ON*1..]->(a:Claim {id: $a_id, proof_id: $pid}) "
        "RETURN path LIMIT 1",
        b_id=depends_on_claim_id, a_id=dependent_claim_id, pid=proof_id,
    ).single()
    return record is not None
