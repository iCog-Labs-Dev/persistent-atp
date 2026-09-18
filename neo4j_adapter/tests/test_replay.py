"""Integration tests for Neo4jProjector, against a live Neo4j instance."""

import unittest
from unittest.mock import MagicMock

from neo4j_adapter.adapter import Neo4jAdapter
from neo4j_adapter.projector import Neo4jProjector


def _make_store(events):
    """Minimal JournalStore stand-in that returns a fixed event list."""
    store = MagicMock()
    # read_events_since(proof_id, after_revision) -> [(revision, payload), ...]
    store.read_events_since.side_effect = lambda pid, after: [
        (i + 1, payload)
        for i, payload in enumerate(events)
        if i >= after
    ]
    return store


class TestNeo4jProjector(unittest.TestCase):
    PROOF_ID = "test-proof-projector"

    def setUp(self):
        self.adapter = Neo4jAdapter()
        self.store = _make_store([])
        self.projector = Neo4jProjector(self.adapter, self.store)

    def tearDown(self):
        with self.adapter._driver.session() as s:
            s.run("MATCH (n {proof_id: $pid}) DETACH DELETE n", pid=self.PROOF_ID)
            s.run(
                "MATCH (w:ProjectionWatermark {proof_id: $pid}) DELETE w",
                pid=self.PROOF_ID,
            )
        self.adapter.close()

    # -- watermark --------------------------------------------------------

    def test_watermark_starts_at_zero(self):
        self.assertEqual(self.projector.watermark(self.PROOF_ID), 0)

    def test_catch_up_with_no_events_returns_zero(self):
        self.assertEqual(self.projector.catch_up(self.PROOF_ID), 0)
        self.assertEqual(self.projector.watermark(self.PROOF_ID), 0)

    # -- upsert node ------------------------------------------------------

    def test_catch_up_applies_upsert_state_and_advances_watermark(self):
        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "upsert_node", "label": "FormalState", "id": f"{self.PROOF_ID}/s1",
                     "fields": {"description": "root goal", "status": "open"}}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)

        applied = self.projector.catch_up(self.PROOF_ID)

        self.assertEqual(applied, 1)
        self.assertEqual(self.projector.watermark(self.PROOF_ID), 1)
        state = self.adapter.get_state(f"{self.PROOF_ID}/s1", self.PROOF_ID)
        self.assertIsNotNone(state)
        self.assertEqual(state["description"], "root goal")

    def test_catch_up_applies_upsert_claim(self):
        # A Claim hangs off the Proof node via HAS_CLAIM; create all three in
        # one event so get_all_claims can find the claim once projected.
        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [
                {"op": "upsert_node", "label": "Proof",
                 "id": self.PROOF_ID,
                 "fields": {"theorem_kernel": "n+0=n"}},
                {"op": "upsert_node", "label": "Claim", "id": f"{self.PROOF_ID}/c1",
                 "fields": {"statement": "n+0=n", "status": "conjectural"}},
                {"op": "add_edge", "rel": "HAS_CLAIM",
                 "src": self.PROOF_ID, "dst": f"{self.PROOF_ID}/c1",
                 "edge_id": f"{self.PROOF_ID}/ec1"},
            ],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)
        self.projector.catch_up(self.PROOF_ID)

        claims = self.adapter.get_all_claims(self.PROOF_ID)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["statement"], "n+0=n")

    # -- set_field --------------------------------------------------------

    def test_catch_up_applies_set_field_on_state(self):
        sid = f"{self.PROOF_ID}/s1"
        self.adapter.init_proof(self.PROOF_ID, "kernel", event_id="ev0")
        self.adapter.add_state(self.PROOF_ID, sid, "goal", event_id="ev0")

        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "set_field", "label": "FormalState", "id": sid,
                     "field": "status", "value": "formally-closed"}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)
        self.projector.catch_up(self.PROOF_ID)

        state = self.adapter.get_state(sid, self.PROOF_ID)
        self.assertEqual(state["status"], "formally-closed")

    # -- idempotency ------------------------------------------------------

    def test_catch_up_called_twice_only_applies_once(self):
        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "upsert_node", "label": "Claim", "id": f"{self.PROOF_ID}/c1",
                     "fields": {"statement": "stmt", "status": "conjectural"}}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)

        first = self.projector.catch_up(self.PROOF_ID)
        # After first catch_up watermark=1; read_events_since returns nothing new
        self.store.read_events_since.side_effect = lambda pid, after: []
        second = self.projector.catch_up(self.PROOF_ID)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    # -- wipe_and_rebuild -------------------------------------------------

    def test_wipe_and_rebuild_removes_stale_nodes_and_replays(self):
        # Write a stale node directly
        self.adapter.add_state(self.PROOF_ID, f"{self.PROOF_ID}/stale", "stale", event_id="ev0")

        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "upsert_node", "label": "FormalState",
                     "id": f"{self.PROOF_ID}/fresh",
                     "fields": {"description": "fresh goal", "status": "open"}}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)
        self.projector.wipe_and_rebuild(self.PROOF_ID)

        self.assertIsNone(self.adapter.get_state(f"{self.PROOF_ID}/stale", self.PROOF_ID))
        self.assertIsNotNone(self.adapter.get_state(f"{self.PROOF_ID}/fresh", self.PROOF_ID))

    # -- derived-state triggers -------------------------------------------

    def test_close_state_is_triggered_when_formal_state_set_to_closed(self):
        sid = f"{self.PROOF_ID}/s1"
        self.adapter.init_proof(self.PROOF_ID, "kernel", event_id="ev0")
        self.adapter.add_state(self.PROOF_ID, sid, "goal", event_id="ev0")

        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "set_field", "label": "FormalState", "id": sid,
                     "field": "status", "value": "formally-closed"}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)
        self.projector.catch_up(self.PROOF_ID)

        state = self.adapter.get_state(sid, self.PROOF_ID)
        self.assertEqual(state["status"], "formally-closed")

    def test_propagate_taint_is_triggered_when_claim_set_to_refuted(self):
        cid = f"{self.PROOF_ID}/c1"
        self.adapter.init_proof(self.PROOF_ID, "kernel", event_id="ev0")
        self.adapter.add_claim(self.PROOF_ID, cid, "stmt", event_id="ev0")

        self.store = _make_store([{
            "event_hash": "ev1",
            "ops": [{"op": "set_field", "label": "Claim", "id": cid,
                     "field": "status", "value": "refuted"}],
        }])
        self.projector = Neo4jProjector(self.adapter, self.store)
        self.projector.catch_up(self.PROOF_ID)

        claims = self.adapter.get_all_claims(self.PROOF_ID)
        c = next(c for c in claims if c["id"] == cid)
        self.assertEqual(c["status"], "refuted")


if __name__ == "__main__":
    unittest.main()
