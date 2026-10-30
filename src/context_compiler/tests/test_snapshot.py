import dataclasses
import unittest

from commit_gate.state import MemoryView
from context_compiler.contracts import CompileRequest, PacketKind, TextBudget
from context_compiler.errors import ContextValidationError
from context_compiler.snapshot import ProofSnapshot, StaleSnapshotError
from shared.vocab import WorkerClass


def _request(proof_id="p1", base_revision=7) -> CompileRequest:
    return CompileRequest(
        task_id="task-1",
        proof_id=proof_id,
        base_revision=base_revision,
        packet_kind=PacketKind.RESEARCH,
        worker_class=next(iter(WorkerClass)),
        text_budget=TextBudget(max_tokens=1000),
    )


class TestProofSnapshotConstruction(unittest.TestCase):
    def test_valid_snapshot(self):
        snap = ProofSnapshot("p1", 7, MemoryView())
        self.assertEqual((snap.proof_id, snap.revision), ("p1", 7))

    def test_revision_zero_is_allowed(self):
        ProofSnapshot("p1", 0, MemoryView())

    def test_rejects_blank_proof_id(self):
        with self.assertRaises(ContextValidationError):
            ProofSnapshot("  ", 0, MemoryView())

    def test_rejects_bad_revisions(self):
        for bad in (-1, True, 1.5, "3", None):
            with self.subTest(revision=bad), self.assertRaises(ContextValidationError):
                ProofSnapshot("p1", bad, MemoryView())

    def test_rejects_something_that_is_not_a_read_view(self):
        with self.assertRaises(ContextValidationError):
            ProofSnapshot("p1", 0, object())

    def test_is_frozen(self):
        snap = ProofSnapshot("p1", 0, MemoryView())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.revision = 1


class TestRequireMatches(unittest.TestCase):
    def setUp(self):
        self.snap = ProofSnapshot("p1", 7, MemoryView())

    def test_matching_request_passes(self):
        self.snap.require_matches(_request())

    def test_other_proof_is_refused(self):
        with self.assertRaises(StaleSnapshotError):
            self.snap.require_matches(_request(proof_id="p2"))

    def test_other_revision_is_refused(self):
        with self.assertRaises(StaleSnapshotError):
            self.snap.require_matches(_request(base_revision=8))

    def test_stale_error_is_a_validation_error(self):
        with self.assertRaises(ContextValidationError):
            self.snap.require_matches(_request(base_revision=1))


if __name__ == "__main__":
    unittest.main()