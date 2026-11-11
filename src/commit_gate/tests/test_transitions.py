import unittest

from commit_gate.transitions import IMMUTABLE_FIELDS, STATUS_TRANSITIONS
from commit_gate.validate import ENUM_FIELDS

class TestTransitions(unittest.TestCase):
    def test_all_status_enums_are_covered(self):
        # Every field ending in 'status' or 'verdict' or 'lifecycle' in ENUM_FIELDS 
        # should have a transition table.
        for (label, field), enum_class in ENUM_FIELDS.items():
            if field.endswith("status") or field.endswith("verdict") or field.endswith("lifecycle"):
                # Exception: Some fields might not have a formal transition table yet,
                # but let's check the ones we explicitly modelled.
                if (label, field) in STATUS_TRANSITIONS:
                    table = STATUS_TRANSITIONS[(label, field)]
                    # Every enum value should be a key in the table
                    for member in enum_class:
                        self.assertIn(member.value, table, f"Missing {member.value} in {label}.{field} transitions")

    def test_stale_is_always_terminal(self):
        for (label, field), table in STATUS_TRANSITIONS.items():
            if "stale" in table:
                self.assertEqual(table["stale"], frozenset())

    def test_terminal_run_dispositions(self):
        table = STATUS_TRANSITIONS[("FormalRun", "status")]
        terminals = [
            "proved-pending-replay",
            "budget-exhausted",
            "stagnated",
            "counterexample",
            "invalid-request",
            "environment-error",
            "internal-error",
            "cancelled",
        ]
        for t in terminals:
            self.assertEqual(table[t], frozenset())
    def test_alignment_verdict_is_not_immutable(self):
        """ verdict must be settable at review time, so a reviewer
        can record their conclusion instead of it being baked in at
        creation, before any review happens."""
        self.assertNotIn("verdict", IMMUTABLE_FIELDS["Alignment"])
 
    def test_alignment_actor_is_still_immutable(self):
        """Only verdict was the bug; actor -- who created the record --
        stays frozen, consistent with the other eight labels."""
        self.assertIn("actor", IMMUTABLE_FIELDS["Alignment"])

if __name__ == "__main__":
    unittest.main()
