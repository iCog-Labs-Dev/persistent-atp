"""Worker-class authority at the gate.

The scheduler issues leases per worker class; a class that could write atom
types outside its remit would make multi-class dispatch unsafe. These tests
pin the boundaries the frontier relies on.
"""

import unittest

from commit_gate.ops import SetField, UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.reasons import Reason
from commit_gate.state import MemoryView
from commit_gate.validate import validate_proposal


def propose(worker_class: str, *ops) -> Proposal:
    return Proposal(
        proof_id="p1",
        actor="worker",
        worker_class=worker_class,
        ops=tuple(ops),
        base_revision=0,
        lease_id="lease-1",
        fencing_token=1,
    )


class TestWorkerAuthority(unittest.TestCase):
    def setUp(self):
        self.view = MemoryView()

    def validate(self, proposal: Proposal) -> list[Reason]:
        return [f.reason for f in validate_proposal(proposal, self.view)]

    def test_explorer_cannot_close_a_formal_state(self):
        proposal = propose(
            "llm-research",
            UpsertNode(
                "FormalState",
                "p1/fs-1",
                {"goal_text": "g", "status": "formally-closed"},
            ),
        )
        reasons = self.validate(proposal)
        self.assertIn(Reason.WORKER_CLASS_OUT_OF_AUTHORITY, reasons)

    def test_cannot_write_outside_authority_via_set_field(self):
        self.view.add_node("p1/fs-1", "FormalState", {"status": "open"})
        proposal = propose(
            "critic",
            SetField("FormalState", "p1/fs-1", "status", "failed", prior="open"),
        )
        reasons = self.validate(proposal)
        self.assertIn(Reason.WORKER_CLASS_OUT_OF_AUTHORITY, reasons)

    def test_critic_can_review_but_not_declare(self):
        verdict = propose(
            "critic",
            UpsertNode(
                "Attempt",
                "p1/at-1",
                {"worker_class": "critic", "status": "supported"},
            ),
        )
        declaration = propose(
            "critic",
            UpsertNode(
                "FormalDeclaration",
                "p1/fd-1",
                {"lean_name": "h", "lean_type": "T", "lean_value": "t"},
            ),
        )
        self.assertNotIn(
            Reason.WORKER_CLASS_OUT_OF_AUTHORITY, self.validate(verdict)
        )
        self.assertIn(
            Reason.WORKER_CLASS_OUT_OF_AUTHORITY, self.validate(declaration)
        )

    def test_formal_atp_cannot_invent_claims(self):
        proposal = propose(
            "formal-atp",
            UpsertNode("Claim", "p1/c-1", {"claim_text": "sneaky"}),
        )
        reasons = self.validate(proposal)
        self.assertIn(Reason.WORKER_CLASS_OUT_OF_AUTHORITY, reasons)

    def test_research_classes_share_the_research_layer(self):
        for worker_class in ("llm-research", "hyperon"):
            move = propose(
                worker_class,
                UpsertNode("ResearchMove", "p1/rm-1", {"status": "queued"}),
            )
            self.assertNotIn(
                Reason.WORKER_CLASS_OUT_OF_AUTHORITY, self.validate(move)
            )

    def test_trusted_classes_are_unrestricted(self):
        for worker_class in ("coordinator", "maintenance", "human"):
            anything = propose(
                worker_class,
                UpsertNode("Claim", f"p1/c-{worker_class}", {"claim_text": "x"}),
            )
            self.assertEqual(self.validate(anything), [])

    def test_unmanaged_worker_class_is_not_policed(self):
        """Legacy free-form classes get no lease, so no authority check."""
        legacy = propose("test", UpsertNode("Claim", "p1/c-9", {}))
        self.assertNotIn(
            Reason.WORKER_CLASS_OUT_OF_AUTHORITY, self.validate(legacy)
        )


if __name__ == "__main__":
    unittest.main()
