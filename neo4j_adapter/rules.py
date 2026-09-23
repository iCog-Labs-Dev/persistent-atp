from __future__ import annotations

from typing import Any, Dict, List

from neo4j import ManagedTransaction

from .constants import (
    CLAIM_REFUTED,
    CLAIM_TAINTED,
    MOVE_CLOSED,
    MOVE_DOMINATED,
    MOVE_EXHAUSTED,
    MOVE_LEASED,
    MOVE_OPEN,
    MOVE_REFUTED,
    MOVE_REOPENED,
    STATE_CLOSED,
    STATE_REOPENED,
    STATE_TAINTED,
)


class RulesMixin:
    """Graph semantics: AND/OR closure, taint propagation, cycle detection.

    Expects the host class to provide the transaction helpers of
    ``neo4j.session.TransactionMixin``.
    """

    # ------------------------------------------------------------------
    # Cycle detection (claim dependency graph)
    # ------------------------------------------------------------------

    def _would_create_cycle(
        self,
        dependent_claim_id: str,
        depends_on_claim_id: str,
        proof_id: str = "",
    ) -> bool:
        """Return True if adding DEPENDS_ON from dependent→depends_on would close a cycle.

        Standalone query, so the answer is only advisory: a concurrent writer can
        invalidate it. ``add_claim_dependency`` re-checks inside the same
        transaction as the MERGE.
        """
        return self._read_value(
            "_would_create_cycle",
            "MATCH path = (b:Claim {id: $b_id, proof_id: $pid})"
            "-[:DEPENDS_ON*1..]->(a:Claim {id: $a_id, proof_id: $pid}) "
            "RETURN count(path) AS n LIMIT 1",
            {
                "b_id": depends_on_claim_id,
                "a_id": dependent_claim_id,
                "pid": proof_id,
            },
            "n",
            default=0,
        ) > 0

    # ------------------------------------------------------------------
    # AND/OR closure (paper §9.6)
    # ------------------------------------------------------------------

    def state_is_solved(self, proof_id: str, state_id: str) -> bool:
        """OR rule: a state is solved when any proposed move is closed."""
        return self._read_value(
            "state_is_solved",
            "MATCH (st:State {proof_id: $pid, id: $sid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
            "WHERE m.status = $closed RETURN count(m) AS c",
            {"pid": proof_id, "sid": state_id, "closed": MOVE_CLOSED},
            "c",
            default=0,
        ) > 0

    def move_is_complete(self, proof_id: str, move_id: str) -> bool:
        """AND rule: a move is complete when every REQUIRES subgoal is closed."""
        return self._read_value(
            "move_is_complete",
            "MATCH (m:Move {proof_id: $pid, id: $mid})-[:REQUIRES]->(sg:State {proof_id: $pid}) "
            "WHERE sg.status <> $closed "
            "RETURN count(sg) AS open_subgoals",
            {
                "pid": proof_id, "mid": move_id,
                "closed": STATE_CLOSED, "reopened": STATE_REOPENED,
            },
            "open_subgoals",
            default=0,
        ) == 0

    def close_state(
        self,
        state_id: str,
        proof_id: str,
        reason: str = "",
        event_id: str = "",
    ) -> None:
        """Mark a state closed, close its proposed moves, then propagate
        closures upward (AND then OR) to a fixpoint.

        All three steps share one transaction: the graph is never observed with
        a closed state whose moves are still open, or with a half-propagated
        fixpoint.

        NOTE: BYPASSES is deliberately NOT a PROPOSES edge, so a bypass never
        closes the literal target (N107 pattern) — that is enforced structurally.
        """

        def work(tx: ManagedTransaction) -> None:
            tx.run(
                "MATCH (st:State {proof_id: $pid, id: $id}) "
                "SET st.status = $status, st.status_updated_in_event = $evt, "
                "    st.closed_reason = $reason",
                pid=proof_id, id=state_id, status=STATE_CLOSED,
                evt=event_id, reason=reason,
            )
            tx.run(
                "MATCH (st:State {proof_id: $pid, id: $sid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
                "SET m.status = $closed, m.status_updated_in_event = $evt",
                pid=proof_id, sid=state_id, evt=event_id, closed=MOVE_CLOSED,
            )
            _propagate_closures_tx(tx, proof_id, event_id)

        self._write("close_state", work)

    def _propagate_closures(self, proof_id: str, event_id: str = "", max_iter: int = 64) -> None:
        """Run the AND/OR closure fixpoint on its own, in a single transaction."""
        self._write(
            "_propagate_closures",
            _propagate_closures_tx,
            proof_id=proof_id,
            event_id=event_id,
            max_iter=max_iter,
        )

    def reopen_state(self, proof_id: str, state_id: str, reason: str = "", event_id: str = "") -> None:
        self.update_state_status(proof_id, state_id, STATE_REOPENED, reason, event_id)

    # ------------------------------------------------------------------
    # Taint propagation (paper §4.10)
    # ------------------------------------------------------------------

    def propagate_taint(self, proof_id: str, claim_id: str, event_id: str = "", reason: str = "") -> Dict[str, Any]:
        """Refute a claim and cascade:
          1. mark the root claim refuted;
          2. taint every transitive DEPENDS_ON dependent (taint cone);
          3. reopen closed states that used a tainted claim.

        One transaction, so the cascade is atomic: a refuted claim can never be
        left with untainted dependents or with closed states still relying on it.
        Returns a summary for audit/milestones.
        """

        def work(tx: ManagedTransaction) -> Dict[str, Any]:
            tx.run(
                "MATCH (c:Claim {proof_id: $pid, id: $cid}) "
                "SET c.status = $refuted, c.status_updated_in_event = $evt, "
                "    c.status_reason = CASE WHEN $reason <> '' "
                "                            THEN $reason ELSE c.status_reason END",
                pid=proof_id, cid=claim_id, evt=event_id, reason=reason,
                refuted=CLAIM_REFUTED,
            )
            record = tx.run(
                "MATCH (root:Claim {proof_id: $pid, id: $cid})"
                "<-[:DEPENDS_ON*1..]-(d:Claim {proof_id: $pid}) "
                "SET d.status = $tainted, d.taint_source = $src, "
                "    d.status_updated_in_event = $evt "
                "RETURN collect(DISTINCT d.id) AS tainted",
                pid=proof_id, cid=claim_id, src=claim_id, evt=event_id,
                tainted=CLAIM_TAINTED,
            ).single()
            tainted = record["tainted"] if record else []

            reopened: List[str] = []
            if tainted:
                reopened_record = tx.run(
                    "MATCH (st:State {proof_id: $pid})"
                    "-[:USES_CLAIM]->(c:Claim {proof_id: $pid}) "
                    "WHERE c.id IN $tainted AND st.status = $closed "
                    "SET st.status = $reopened, "
                    "    st.closed_reason = 'taint: ' + $src, "
                    "    st.status_updated_in_event = $evt "
                    "RETURN collect(DISTINCT st.id) AS reopened",
                    pid=proof_id, tainted=tainted, src=claim_id, evt=event_id,
                    closed=STATE_CLOSED, reopened=STATE_REOPENED,
                ).single()
                reopened = reopened_record["reopened"] if reopened_record else []

            return {"refuted": claim_id, "tainted": tainted, "reopened_states": reopened}

        return self._write("propagate_taint", work)

    def taint_cone(self, proof_id: str, claim_id: str) -> List[str]:
        return self._read_value(
            "taint_cone",
            "MATCH (root:Claim {proof_id: $pid, id: $cid})"
            "<-[:DEPENDS_ON*1..]-(d:Claim {proof_id: $pid}) "
            "RETURN collect(DISTINCT d.id) AS ids",
            {"pid": proof_id, "cid": claim_id},
            "ids",
            default=[],
        )

    # ------------------------------------------------------------------
    # Eligible frontier (paper §4.7)
    # ------------------------------------------------------------------

    def eligible_frontier(self, proof_id: str) -> List[Dict[str, Any]]:
        """Eligible moves for leasing.

        (Open ∪ Reopened) − (Leased ∪ Refuted ∪ Dominated ∪ Exhausted) (4.7),
        restricted to moves whose state is neither tainted nor closed.
        """
        return self._read_many(
            "eligible_frontier",
            "MATCH (st:State {proof_id: $pid})-[:PROPOSES]->(m:Move {proof_id: $pid}) "
            "WHERE m.status IN $eligible "
            "  AND m.status <> $leased AND m.status <> $move_refuted "
            "  AND m.status <> $dominated AND m.status <> $exhausted "
            "  AND st.status <> $tainted AND st.status <> $closed "
            "RETURN m ORDER BY m.status, m.id",
            {
                "pid": proof_id, "eligible": [MOVE_OPEN, MOVE_REOPENED],
                "leased": MOVE_LEASED, "move_refuted": MOVE_REFUTED,
                "dominated": MOVE_DOMINATED, "exhausted": MOVE_EXHAUSTED,
                "tainted": STATE_TAINTED, "closed": STATE_CLOSED,
            },
            "m",
        )


def _propagate_closures_tx(
    tx: ManagedTransaction,
    proof_id: str,
    event_id: str = "",
    max_iter: int = 64,
) -> None:
    """AND/OR closure fixpoint, inside a caller-supplied transaction."""
    for _ in range(max_iter):
        # AND: a move closes once every REQUIRES subgoal is closed.
        r1 = tx.run(
            "MATCH (m:Move {proof_id: $pid}) "
            "WHERE m.status <> $move_closed "
            "AND NOT exists { (m)-[:REQUIRES]->(sg:State {proof_id: $pid}) "
            "                  WHERE sg.status <> $closed AND sg.status <> $reopened } "
            "SET m.status = $move_closed, m.status_updated_in_event = $evt "
            "RETURN count(m) AS n",
            pid=proof_id, evt=event_id, move_closed=MOVE_CLOSED,
            closed=STATE_CLOSED, reopened=STATE_REOPENED,
        ).single()["n"]
        # OR: a state closes once any proposed move is closed.
        r2 = tx.run(
            "MATCH (st:State {proof_id: $pid}) "
            "WHERE st.status <> $closed AND st.status <> $reopened "
            "AND exists { (st)-[:PROPOSES]->(m:Move {proof_id: $pid}) "
            "             WHERE m.status = $move_closed } "
            "SET st.status = $closed, st.status_updated_in_event = $evt "
            "RETURN count(st) AS n",
            pid=proof_id, evt=event_id, move_closed=MOVE_CLOSED,
            closed=STATE_CLOSED, reopened=STATE_REOPENED,
        ).single()["n"]
        if r1 == 0 and r2 == 0:
            break
