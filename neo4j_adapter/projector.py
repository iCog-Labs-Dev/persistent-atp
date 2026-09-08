"""Projects committed journal events onto the Neo4j adapter.

The journal (SQLite) is the durability authority. This projector reads
committed events from a `JournalStore` via `read_events_since`, translates
each op into the appropriate `Neo4jAdapter` write call, and advances a
watermark stored as a `:ProjectionWatermark {proof_id, revision}` node in
Neo4j — in the same transaction as the op writes, so a crash mid-event
leaves the watermark behind and the event is safely replayed on the next
`catch_up` call.

After writing each event's ops, the projector inspects them for semantic
signals and triggers derived graph rules automatically:
  - FormalState closed  → adapter.close_state()   (AND/OR closure fixpoint)
  - Claim refuted       → adapter.propagate_taint() (taint cascade)
"""

from __future__ import annotations

from typing import Any, Dict, List

from neo4j import ManagedTransaction

from .adapter import Neo4jAdapter
from .constants import (
    CLAIM_CONJECTURAL,
    CLAIM_REFUTED,
    MOVE_OPEN,
    STATE_CLOSED,
    STATE_KIND_OR,
)

_UPSERT = "upsert_node"
_SET = "set_field"
_ADD_EDGE = "add_edge"
_REMOVE_EDGE = "remove_edge"


class Neo4jProjector:
    """Incrementally projects a `JournalStore` onto a `Neo4jAdapter`.

    Construct with an existing `Neo4jAdapter` (which already owns a driver
    and database connection) and the `JournalStore` to read from.
    """

    def __init__(self, adapter: Neo4jAdapter, store: Any) -> None:
        self._adapter = adapter
        self._store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def watermark(self, proof_id: str) -> int:
        """The journal revision this projection has applied up to. 0 if none."""
        def work(tx: ManagedTransaction) -> int:
            record = tx.run(
                "MATCH (w:ProjectionWatermark {proof_id: $pid}) RETURN w.revision AS r",
                pid=proof_id,
            ).single()
            return record["r"] if record is not None else 0

        return self._adapter._read("watermark", work)

    def catch_up(self, proof_id: str) -> int:
        """Apply every event committed since the watermark, in order.

        Returns how many events were applied. Safe to call repeatedly.
        """
        applied = 0
        current = self.watermark(proof_id)
        for revision, payload in self._store.read_events_since(proof_id, current):
            ops = payload.get("ops", [])
            self._apply_event(proof_id, revision, ops)
            self._trigger_derived(proof_id, ops, event_id=payload.get("event_hash", ""))
            applied += 1
        return applied

    def wipe_and_rebuild(self, proof_id: str) -> int:
        """Delete the proof's graph projection and replay from revision 0.

        Not one transaction by design: a full replay can be arbitrarily
        large. An interrupted rebuild is recovered by calling this again.
        """
        self._adapter._write_all(
            "wipe_and_rebuild",
            [
                ("MATCH (n) WHERE n.proof_id = $pid DETACH DELETE n", {"pid": proof_id}),
                ("MATCH (w:ProjectionWatermark {proof_id: $pid}) DELETE w", {"pid": proof_id}),
            ],
        )
        return self.catch_up(proof_id)

    # ------------------------------------------------------------------
    # Event application
    # ------------------------------------------------------------------

    def _apply_event(self, proof_id: str, revision: int, ops: List[Dict[str, Any]]) -> None:
        """Write all ops for one event + advance the watermark atomically."""
        adapter = self._adapter

        def work(tx: ManagedTransaction) -> None:
            for op in ops:
                self._apply_op(tx, proof_id, op)
            tx.run(
                "MERGE (w:ProjectionWatermark {proof_id: $pid}) SET w.revision = $rev",
                pid=proof_id, rev=revision,
            )

        adapter._write("_apply_event", work)

    def _apply_op(self, tx: ManagedTransaction, proof_id: str, op: Dict[str, Any]) -> None:
        kind = op.get("op")
        label = op.get("label", "")
        node_id = op.get("id", "")
        fields = op.get("fields") or {}
        event_id = op.get("event_id", "")

        if kind == _UPSERT:
            self._upsert(tx, proof_id, label, node_id, fields, event_id)
        elif kind == _SET:
            self._set_field(tx, proof_id, label, node_id, op["field"], op["value"])
        elif kind == _ADD_EDGE:
            self._add_edge(tx, proof_id, op)
        elif kind == _REMOVE_EDGE:
            tx.run(
                "MATCH ()-[r {edge_id: $eid}]-() DELETE r",
                eid=op["edge_id"],
            )

    def _upsert(
        self,
        tx: ManagedTransaction,
        proof_id: str,
        label: str,
        node_id: str,
        fields: Dict[str, Any],
        event_id: str,
    ) -> None:
        """Translate a UpsertNode op into the right adapter Cypher, in-transaction."""
        f = fields

        if label == "Proof":
            tx.run(
                "MERGE (p:Proof {proof_id: $pid, id: $pid}) "
                "ON CREATE SET p.theorem_kernel = $k, p.theorem_hash = $h, "
                "              p.active_revision = 0, p.created_in_event = $evt",
                pid=proof_id,
                k=f.get("theorem_kernel", ""),
                h=f.get("theorem_hash", ""),
                evt=event_id,
            )

        elif label in ("FormalState", "State"):
            tx.run(
                "MERGE (st:State {proof_id: $pid, id: $id}) "
                "ON CREATE SET st.description = $desc, st.status = $status, "
                "              st.kind = $kind, st.assumptions = $ass, "
                "              st.created_in_event = $evt",
                pid=proof_id, id=node_id,
                desc=f.get("description", ""),
                status=f.get("status", "open"),
                kind=f.get("kind", STATE_KIND_OR),
                ass=f.get("assumptions", ""),
                evt=event_id,
            )

        elif label in ("TacticApplication", "Move"):
            tx.run(
                "MERGE (m:Move {proof_id: $pid, id: $id}) "
                "ON CREATE SET m.move_summary = $sum, m.kind = $kind, "
                "              m.note = $note, m.status = $status, "
                "              m.score = $score, m.cost_estimate = $cost, "
                "              m.repeated_failure_count = 0, m.created_in_event = $evt",
                pid=proof_id, id=node_id,
                sum=f.get("move_summary", f.get("description", "")),
                kind=f.get("kind", "reduction"),
                note=f.get("note", ""),
                status=f.get("status", MOVE_OPEN),
                score=f.get("score"),
                cost=f.get("cost_estimate"),
                evt=event_id,
            )

        elif label == "Claim":
            tx.run(
                "MERGE (c:Claim {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.statement = $stmt, c.status = $status, "
                "              c.statement_blob = $blob, c.created_in_event = $evt",
                pid=proof_id, id=node_id,
                stmt=f.get("statement", ""),
                status=f.get("status", CLAIM_CONJECTURAL),
                blob=f.get("statement_blob", ""),
                evt=event_id,
            )

        elif label == "Attempt":
            tx.run(
                "MERGE (a:Attempt {proof_id: $pid, id: $id}) "
                "ON CREATE SET a.move_summary = $sum, a.worker = $worker, "
                "              a.note = $note, a.status = $status, "
                "              a.model_persona = $persona, a.disposition = $disp, "
                "              a.result_relation = $rel, a.created_in_event = $evt",
                pid=proof_id, id=node_id,
                sum=f.get("move_summary", ""),
                worker=f.get("worker", "explorer"),
                note=f.get("note", ""),
                status=f.get("status", "pending"),
                persona=f.get("model_persona", ""),
                disp=f.get("disposition", ""),
                rel=f.get("result_relation", ""),
                evt=event_id,
            )

        elif label == "Route":
            tx.run(
                "MERGE (r:Route {proof_id: $pid, id: $id}) "
                "ON CREATE SET r.display_path = $path, r.created_in_event = $evt",
                pid=proof_id, id=node_id,
                path=f.get("display_path", ""),
                evt=event_id,
            )

        elif label == "Artifact":
            tx.run(
                "MERGE (a:Artifact {proof_id: $pid, id: $id}) "
                "ON CREATE SET a.kind = $kind, a.media_type = $media, "
                "              a.sha256 = $sha, a.filename = $fname, "
                "              a.created_in_event = $evt",
                pid=proof_id, id=node_id,
                kind=f.get("kind", ""),
                media=f.get("media_type", ""),
                sha=f.get("sha256", ""),
                fname=f.get("filename", ""),
                evt=event_id,
            )

        elif label == "Context":
            tx.run(
                "MERGE (c:Context {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.packet_hash = $hash, c.compiler_version = $ver, "
                "              c.token_budget = $budget, c.token_count = $count, "
                "              c.created_in_event = $evt",
                pid=proof_id, id=node_id,
                hash=f.get("packet_hash", ""),
                ver=f.get("compiler_version", ""),
                budget=f.get("token_budget", 0),
                count=f.get("token_count", 0),
                evt=event_id,
            )

        elif label == "Concept":
            tx.run(
                "MERGE (c:Concept {proof_id: $pid, id: $id}) "
                "ON CREATE SET c.name = $name, c.mechanism_tags = $tags, "
                "              c.created_in_event = $evt",
                pid=proof_id, id=node_id,
                name=f.get("name", ""),
                tags=f.get("mechanism_tags", ""),
                evt=event_id,
            )

        elif label in ("SpeculativeHypothesis", "Hypothesis"):
            tx.run(
                "MERGE (h:Hypothesis {proof_id: $pid, id: $id}) "
                "ON CREATE SET h.kind = $kind, h.layer = 'speculative', "
                "              h.falsification_test = $test, h.novelty = $nov, "
                "              h.abductive_strength = $ab, h.cost = $cost, "
                "              h.risk = $risk, h.lifecycle_status = $lc, "
                "              h.created_in_event = $evt",
                pid=proof_id, id=node_id,
                kind=f.get("kind", ""),
                test=f.get("falsification_test", ""),
                nov=f.get("novelty", 0.0),
                ab=f.get("abductive_strength", 0.0),
                cost=f.get("cost", 0.0),
                risk=f.get("risk", 0.0),
                lc=f.get("lifecycle_status", "queued"),
                evt=event_id,
            )

        else:
            # Generic fallback: MERGE on (proof_id, id) and set all fields.
            tx.run(
                f"MERGE (n:{label} {{proof_id: $pid, id: $id}}) "
                "ON CREATE SET n += $fields, n.created_in_event = $evt",
                pid=proof_id, id=node_id, fields=f, evt=event_id,
            )

    def _set_field(
        self,
        tx: ManagedTransaction,
        proof_id: str,
        label: str,
        node_id: str,
        field: str,
        value: Any,
    ) -> None:
        graph_label = _GATE_TO_GRAPH.get(label, label)
        tx.run(
            f"MATCH (n:{graph_label} {{proof_id: $pid, id: $id}}) SET n += $props",
            pid=proof_id, id=node_id, props={field: value},
        )

    def _add_edge(
        self, tx: ManagedTransaction, proof_id: str, op: Dict[str, Any]
    ) -> None:
        rel = op["rel"]
        src = op["src"]
        dst = op["dst"]
        edge_id = op["edge_id"]
        extra = op.get("fields") or {}

        if rel == "FORMAL_REQUIRES":
            # REQUIRES in the adapter's schema
            tx.run(
                "MATCH (m:Move {proof_id: $pid, id: $mid}), "
                "      (st:State {proof_id: $pid, id: $sid}) "
                "MERGE (m)-[r:REQUIRES {edge_id: $eid}]->(st) "
                "ON CREATE SET r += $fields",
                pid=proof_id, mid=src, sid=dst, eid=edge_id, fields=extra,
            )
        elif rel == "DEPENDS_ON":
            tx.run(
                "MATCH (a:Claim {proof_id: $pid, id: $aid}), "
                "      (b:Claim {proof_id: $pid, id: $bid}) "
                "MERGE (a)-[r:DEPENDS_ON {edge_id: $eid}]->(b) "
                "ON CREATE SET r += $fields",
                pid=proof_id, aid=src, bid=dst, eid=edge_id, fields=extra,
            )
        else:
            # Generic: match any two nodes by proof_id+id and create the rel.
            tx.run(
                f"MATCH (a {{proof_id: $pid, id: $aid}}), "
                f"      (b {{proof_id: $pid, id: $bid}}) "
                f"MERGE (a)-[r:{rel} {{edge_id: $eid}}]->(b) "
                f"ON CREATE SET r += $fields",
                pid=proof_id, aid=src, bid=dst, eid=edge_id, fields=extra,
            )

    # ------------------------------------------------------------------
    # Derived-state triggers (run after the event transaction commits)
    # ------------------------------------------------------------------

    def _trigger_derived(
        self, proof_id: str, ops: List[Dict[str, Any]], event_id: str
    ) -> None:
        for op in ops:
            if op.get("op") != _SET:
                continue
            label = op.get("label", "")
            field = op.get("field", "")
            value = op.get("value")
            node_id = op.get("id", "")

            if label in ("FormalState", "State") and field == "status" and value == STATE_CLOSED:
                self._adapter.close_state(node_id, proof_id, event_id=event_id)

            elif label == "Claim" and field == "status" and value == CLAIM_REFUTED:
                self._adapter.propagate_taint(proof_id, node_id, event_id=event_id)


# Gate label -> adapter graph label translation for SetField
_GATE_TO_GRAPH: Dict[str, str] = {
    "FormalState": "State",
    "TacticApplication": "Move",
    "SpeculativeHypothesis": "Hypothesis",
}
