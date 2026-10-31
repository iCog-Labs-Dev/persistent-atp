"""Coordinator rounds over the journal and the real MORK projection."""

from uuid import uuid4

import pytest

from alignment.reviewer import LLMAlignmentReviewer
from alignment.worker import AlignmentWorker
from coordinator.portfolio import AGENT_PORTFOLIO
from coordinator.rounds import assess, new_track, work
from mathproof.dispatch import ScriptedDispatcher
from mathproof.lean_statement import StatementCheck
from mathproof.workers import FormalizerWorker
from mork.backend import MorkSpace, MorkUnavailable, MorkView
from shared.llm_client import MockLLMClient

from .fixtures import JournalCase, aligned_claim, critic, declaration, explorer


class TestMorkRound(JournalCase):
    def test_critic_formalizer_and_alignment_agents_share_one_round(self):
        try:
            view = MorkView(MorkSpace())
        except MorkUnavailable:
            pytest.skip("MORK library unavailable")

        proof_id = f"agents-{uuid4().hex[:12]}"
        self.seed(proof_id, *aligned_claim(proof_id))
        formalizer_llm = MockLLMClient(default_json={
            "lean_type": "True", "imports": ["Init"], "reasoning": "The claim is True.",
        })
        alignment_llm = MockLLMClient(default_json={
            "verdict": "aligned", "relation": "exact", "reasoning": "Both statements say True.",
        })

        class Checker:
            def check(self, lean_type, imports):
                assert lean_type == "True" and imports == ["Init"]
                return StatementCheck(True, "elaborated")

        workspace = self.workspace(proof_id)
        workspace.view = view
        workspace.dispatcher = ScriptedDispatcher({
            "critic": critic(),
            "formalizer": FormalizerWorker(formalizer_llm).run,
        })
        workspace.adapters = {
            "alignment-reviewer": AlignmentWorker(
                actor="alignment-reviewer",
                reviewer=LLMAlignmentReviewer(alignment_llm),
            ),
            "lean-statement-checker": Checker(),
        }

        report = work(workspace, 1, slots=AGENT_PORTFOLIO)

        self.assertEqual(
            [(slot["slot"], slot["accepted"]) for slot in report["slots"]],
            [("critic", True), ("formalize", True), ("alignment", True)],
        )
        self.assertEqual(view.node(f"{proof_id}/c-1").fields["status"], "critic-accepted")
        self.assertEqual(view.node(f"{proof_id}/fd-1").fields["status"], "aligned")
        self.assertEqual(len(formalizer_llm.history), 1)
        self.assertEqual(len(alignment_llm.history), 1)
        self.assertEqual(view.projected_revision(proof_id), self.open_store().head(proof_id)[0])

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
