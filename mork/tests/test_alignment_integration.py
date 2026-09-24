"""Real-FFI integration from alignment review through journal and MORK."""

import unittest
from hashlib import sha1

from alignment.models import AlignmentReviewRequest
from alignment.worker import AlignmentWorker
from commit_gate.gate import CommitGate
from commit_gate.ops import UpsertNode
from commit_gate.proposal import Proposal
from commit_gate.store import JournalStore
from mork.backend import MorkSpace, MorkUnavailable, MorkView
from mork.projector import project_event_journal

try:
    _SPACE = MorkSpace()
except MorkUnavailable:
    _SPACE = None

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")


def _raw_atom(command: str) -> str:
    prefix = "!(add-atom &mork "
    if not command.startswith(prefix) or not command.endswith(")"):
        raise ValueError(f"not an add-atom command: {command!r}")
    return command[len(prefix) : -1]


@needs_mork
class TestAlignmentMorkIntegration(unittest.TestCase):
    def setUp(self):
        suffix = sha1(self._testMethodName.encode()).hexdigest()[:10]
        self.proof = f"ali-{suffix}"
        self.view = MorkView(_SPACE)
        self.store = JournalStore()
        self.gate = CommitGate(self.view, self.store)
        self.claim_id = f"{self.proof}/c-1"
        self.declaration_id = f"{self.proof}/fd-1"
        self.alignment_id = f"{self.proof}/al-1"

        seed = Proposal(
            proof_id=self.proof,
            actor="coordinator",
            worker_class="coordinator",
            base_revision=0,
            ops=(
                UpsertNode("Claim", self.claim_id, {"status": "provisional"}),
                UpsertNode(
                    "FormalDeclaration",
                    self.declaration_id,
                    {"exact_hash": "sha256:abc"},
                ),
            ),
        )
        result = self.gate.commit(seed)
        self.assertTrue(result.accepted, result.rejections)

    def _commit_alignment(self):
        token = self.store.acquire_lease(self.proof, "lease-alignment")
        request = AlignmentReviewRequest(
            proof_id=self.proof,
            claim_id=self.claim_id,
            declaration_id=self.declaration_id,
            informal_statement="For every natural n, n + 0 = n.",
            formal_statement="theorem add_zero (n : Nat) : n + 0 = n",
        )
        _, proposal = AlignmentWorker(actor="alignment-reviewer-agent").run_review(
            request,
            self.alignment_id,
            base_revision=1,
            lease_id="lease-alignment",
            fencing_token=token,
        )
        result = self.gate.commit(proposal)
        self.assertTrue(result.accepted, result.rejections)
        return result

    def test_alignment_review_commits_to_journal_and_live_mork(self):
        result = self._commit_alignment()

        self.assertEqual(result.revision, 2)
        self.assertEqual(self.store.head(self.proof), (2, result.event_hash))
        alignment = self.view.node(self.alignment_id)
        self.assertEqual(alignment.label, "Alignment")
        self.assertEqual(alignment.fields["verdict"], "aligned")
        self.assertEqual(alignment.fields["lifecycle"], "reviewed")
        self.assertTrue(alignment.fields["statement_hash"].startswith("sha256:"))
        self.assertEqual(len(alignment.fields["statement_hash"]), 71)
        self.assertTrue(alignment.fields["source_hash"].startswith("sha256:"))
        self.assertEqual(len(alignment.fields["source_hash"]), 71)

        atoms = self.view.atoms(self.proof)
        self.assertIn(
            f'(layer "{self.proof}" "al-1" "committed")', atoms
        )
        claim_edge = "al-1->c-1:ALIGNS_CLAIM"
        self.assertIn(
            f'(rev-edge "{self.proof}" "c-1" "ALIGNS_CLAIM" '
            f'"al-1" "{claim_edge}")',
            atoms,
        )
        self.assertEqual(self.view.projected_revision(self.proof), 2)

    def test_live_projection_matches_file_projection(self):
        self._commit_alignment()
        journal = {
            "proof_id": self.proof,
            "events": [
                {"revision": revision, "payload": payload}
                for revision, payload in enumerate(
                    self.store.read_events(self.proof), start=1
                )
            ],
        }
        file_atoms = sorted(_raw_atom(command) for command in project_event_journal(journal))
        live_atoms = sorted(
            atom
            for atom in self.view.atoms(self.proof)
            if not atom.startswith("(projected ")
        )
        self.assertEqual(live_atoms, file_atoms)

    def test_rejection_is_audited_without_changing_mork(self):
        before = self.view.atoms(self.proof)
        result = self.gate.commit(
            Proposal(
                proof_id=self.proof,
                actor="broken-worker",
                worker_class="formal-atp",
                base_revision=1,
                ops=(
                    UpsertNode(
                        "TacticApplication",
                        f"{self.proof}/ta-bad",
                        {"executor_result": "lean-accepted"},
                    ),
                ),
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(self.view.atoms(self.proof), before)
        self.assertEqual(self.view.projected_revision(self.proof), 1)
        rejections = self.store.read_rejections(self.proof)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["payload"]["actor"], "broken-worker")

    def test_atoms_are_isolated_by_proof(self):
        self._commit_alignment()
        other = f"{self.proof}-other"
        self.view.add_node(f"{other}/n1", "Claim", {"status": "conjectural"})

        self.assertTrue(self.view.atoms(self.proof))
        self.assertTrue(self.view.atoms(other))
        self.assertFalse(
            any(f'"{other}"' in atom for atom in self.view.atoms(self.proof))
        )


if __name__ == "__main__":
    unittest.main()
