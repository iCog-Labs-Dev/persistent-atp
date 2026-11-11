"""E1: the Appendix A ID-type namespace and allocator."""

import unittest

from mathproof.ids import (
    DERIVED_ID_TYPES,
    ID_PREFIXES,
    IdAllocator,
    IdType,
    full_id,
    is_valid_local_id,
    local_id,
    parse_local_id,
)


class TestReservedNamespace(unittest.TestCase):
    def test_all_eleven_appendix_a_prefixes_are_reserved(self):
        self.assertEqual(
            {t.value for t in ID_PREFIXES},
            {"rs", "rm", "c", "fd", "fr", "fs", "ta", "al", "obs", "cert", "lr"},
        )

    def test_derived_types_cover_remaining_gate_labels(self):
        self.assertEqual(
            {t.value for t in DERIVED_ID_TYPES},
            {"env", "fc", "sh", "at", "ls"},
        )

    def test_prefixes_are_unique(self):
        values = [t.value for t in IdType]
        self.assertEqual(len(values), len(set(values)))


class TestLocalIds(unittest.TestCase):
    def test_render_and_parse_round_trip(self):
        for id_type in IdType:
            raw = local_id(id_type, 12)
            parsed_type, serial = parse_local_id(raw)
            self.assertEqual((parsed_type, serial), (id_type, 12))

    def test_multi_character_prefixes_do_not_confuse_the_parser(self):
        """`cert-1` parses as CERT, not CLAIM followed by junk."""
        id_type, serial = parse_local_id("cert-1")
        self.assertEqual(id_type, IdType.CERTIFICATE)
        self.assertEqual(serial, 1)

    def test_invalid_forms_are_rejected(self):
        for bad in ("c-0", "c-01", "x-1", "-1", "cert-", "fs_1", "p1/fs-1", "sha256:ab"):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_local_id(bad))
                with self.assertRaises(ValueError):
                    parse_local_id(bad)

    def test_serial_must_be_positive(self):
        with self.assertRaises(ValueError):
            local_id(IdType.CLAIM, 0)


class TestIdAllocator(unittest.TestCase):
    def test_full_ids_carry_the_proof_scope(self):
        alloc = IdAllocator("p1")
        self.assertEqual(alloc.next(IdType.FORMAL_STATE), "p1/fs-1")
        self.assertEqual(alloc.next(IdType.FORMAL_STATE), "p1/fs-2")

    def test_counters_are_independent_per_type(self):
        alloc = IdAllocator("p1")
        claim = alloc.next(IdType.CLAIM)
        state = alloc.next(IdType.FORMAL_STATE)
        self.assertEqual(claim, "p1/c-1")
        self.assertEqual(state, "p1/fs-1")

    def test_allocates_every_reserved_prefix_without_collision(self):
        alloc = IdAllocator("p1")
        issued = {alloc.next(t) for t in IdType}
        self.assertEqual(len(issued), len(IdType))

    def test_state_round_trips_into_a_fresh_allocator(self):
        alloc = IdAllocator("p1")
        for _ in range(3):
            alloc.next(IdType.OBSTRUCTION)

        resumed = IdAllocator("p1", start=alloc.state())
        self.assertEqual(resumed.next(IdType.OBSTRUCTION), "p1/obs-4")


class TestGateNamespaceCompatibility(unittest.TestCase):
    """Issued IDs must satisfy the commit gate's namespace rule."""

    def test_full_ids_start_with_proof_scope(self):
        from commit_gate.ops import UpsertNode
        from commit_gate.proposal import Proposal
        from commit_gate.validate import check_namespace

        alloc = IdAllocator("p1")
        proposal = Proposal(
            proof_id="p1",
            actor="test",
            worker_class="test",
            ops=(
                UpsertNode(
                    "FormalState",
                    alloc.next(IdType.FORMAL_STATE),
                    {"status": "open"},
                ),
                UpsertNode(
                    "Claim",
                    alloc.next(IdType.CLAIM),
                    {"status": "conjectural"},
                ),
            ),
        )
        self.assertEqual(list(check_namespace(proposal)), [])


if __name__ == "__main__":
    unittest.main()
