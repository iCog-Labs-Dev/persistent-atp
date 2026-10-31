"""Coordinator rounds over the journal and the real MORK projection."""

from uuid import uuid4

import pytest

from coordinator.rounds import assess, new_track, work
from mork.backend import MorkSpace, MorkUnavailable, MorkView

from .fixtures import JournalCase, declaration, explorer


class TestMorkRound(JournalCase):
    def test_stagnation_escalation_is_projected_and_worked(self):
        try:
            view = MorkView(MorkSpace())
        except MorkUnavailable:
            pytest.skip("MORK library unavailable")

        proof_id = f"coord-{uuid4().hex[:12]}"
        self.seed(proof_id, declaration(proof_id))
        worked = []
        workspace = self.workspace(
            proof_id,
            handlers={"llm-research": explorer(worked=worked)},
            plan={f"{proof_id}/fr-1": ["stagnated"]},
        )
        workspace.view = view

        track, raised, _ = assess(workspace, new_track(), work(workspace, 1))
        self.assertTrue(raised)
        moves = view.list_nodes(proof_id, "ResearchMove")
        self.assertEqual(len(moves), len(raised))
        self.assertTrue(all(view.edges_from(move.node_id, "MOVE_TARGETS") for move in moves))

        assess(workspace, track, work(workspace, 2))
        self.assertEqual(len(worked), 1)
        self.assertIn(worked[0], {move.node_id for move in moves})
        self.assertEqual(view.node(worked[0]).fields["status"], "exhausted")
        self.assertEqual(self.open_store().verify_chain(proof_id), self.open_store().head(proof_id)[0])
