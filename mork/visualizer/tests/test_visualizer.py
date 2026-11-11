"""Tests for the visualizer, exercising parser.py and graph.py against the
real fixture files already in this repo -- not synthetic ones -- so a
change in the projector's actual output shape is caught here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ..graph import GraphBuildError, build_graph
from ..parser import MettaSyntaxError, extract_graph_atoms, parse_top_level_forms
from ..render import to_dot

FIXTURES_DIR = Path(__file__).resolve().parents[2]
EVEN_SUM_FIXTURE = FIXTURES_DIR / "proofs" / "even-sum-proof.metta"
TEST_GRAPH_FIXTURE = FIXTURES_DIR / "rules" / "tests" / "test_graph.metta"


# ---------------------------------------------------------------------------
# parser.py
# ---------------------------------------------------------------------------


def test_tokenizer_keeps_a_multiword_field_value_as_one_string():
    text = '!(add-atom &mork (field "p" "s1" "description" "Prove: if n even and m even, then n+m even"))'
    atoms = extract_graph_atoms(text).graph_atoms
    assert len(atoms) == 1
    assert atoms[0].args == ("p", "s1", "description", "Prove: if n even and m even, then n+m even")


def test_comments_are_stripped():
    text = ';; this is a comment\n!(add-atom &mork (node "p" "s1" "State"))\n'
    atoms = extract_graph_atoms(text).graph_atoms
    assert len(atoms) == 1


def test_escaped_quotes_in_a_field_value_are_unescaped():
    text = r'!(add-atom &mork (field "p" "s1" "note" "she said \"hi\""))'
    atoms = extract_graph_atoms(text).graph_atoms
    assert atoms[0].args[-1] == 'she said "hi"'


def test_match_query_blocks_are_skipped_not_treated_as_graph_data():
    text = (
        '!(add-atom &mork (node "p" "s1" "State"))\n'
        "!(match &mork\n"
        '  (, (node $proof $sid "State"))\n'
        "  (open-state $proof $sid))\n"
    )
    result = extract_graph_atoms(text)
    assert len(result.graph_atoms) == 1
    assert len(result.skipped_forms) == 1
    assert result.skipped_forms[0].head == "match"


def test_mm2_exec_is_skipped():
    text = "!(mm2-exec &mork 1)\n"
    result = extract_graph_atoms(text)
    assert result.graph_atoms == []
    assert len(result.skipped_forms) == 1


def test_an_unrecognized_add_atom_head_is_reported_not_silently_dropped():
    text = '!(add-atom &mork (some-future-atom-type "p" "x"))\n'
    result = extract_graph_atoms(text)
    assert result.graph_atoms == []
    assert result.unrecognized_add_atom_heads == ["some-future-atom-type"]


def test_unterminated_string_raises():
    with pytest.raises(MettaSyntaxError):
        list(parse_top_level_forms('!(add-atom &mork (node "unterminated))'))


def test_unclosed_paren_raises():
    with pytest.raises(MettaSyntaxError):
        list(parse_top_level_forms('!(add-atom &mork (node "p" "s1" "State")'))


# ---------------------------------------------------------------------------
# graph.py, against the real fixtures
# ---------------------------------------------------------------------------


def _graph_from(path: Path):
    atoms = extract_graph_atoms(path.read_text(encoding="utf-8")).graph_atoms
    return build_graph(atoms)


def test_even_sum_proof_fixture_builds_the_expected_node_set():
    g = _graph_from(EVEN_SUM_FIXTURE)
    assert g.proof_id == "even-sum-proof"
    assert set(g.nodes) == {"s1", "c1", "m1", "a1"}
    assert g.nodes["s1"].label == "State"
    assert g.nodes["c1"].fields["status"] == "conjectural"


def test_even_sum_proof_fixture_builds_the_expected_edge_set():
    g = _graph_from(EVEN_SUM_FIXTURE)
    assert set(g.edges) == {"e1", "e2", "e3", "e4"}
    assert (g.edges["e1"].rel_type, g.edges["e1"].src_id, g.edges["e1"].dst_id) == (
        "SUPPORTED_BY", "s1", "c1",
    )


def test_even_sum_proof_fixture_has_no_rev_edge_mismatches():
    g = _graph_from(EVEN_SUM_FIXTURE)
    assert g.rev_edge_mismatches == []


def test_even_sum_proof_committed_layer_is_applied_to_nodes_and_edges():
    g = _graph_from(EVEN_SUM_FIXTURE)
    assert g.nodes["s1"].layer == "committed"
    assert g.edges["e1"].layer == "committed"


def test_test_graph_fixture_includes_the_documented_isolated_node():
    g = _graph_from(TEST_GRAPH_FIXTURE)
    assert "c2" in g.nodes
    assert not any(e.src_id == "c2" or e.dst_id == "c2" for e in g.edges.values())


def test_rev_edge_mismatch_is_detected_when_injected():
    text = TEST_GRAPH_FIXTURE.read_text(encoding="utf-8")
    text += '\n!(add-atom &mork (rev-edge "test-proof" "m1" "WRONG_REL" "s1" "e1"))\n'
    atoms = extract_graph_atoms(text).graph_atoms
    g = build_graph(atoms)
    assert len(g.rev_edge_mismatches) == 1
    assert "WRONG_REL" in g.rev_edge_mismatches[0].reason


def test_rev_edge_to_a_nonexistent_edge_id_is_detected():
    text = '!(add-atom &mork (rev-edge "p" "b" "REL" "a" "nonexistent-edge"))\n'
    atoms = extract_graph_atoms(text).graph_atoms
    g = build_graph(atoms)
    assert len(g.rev_edge_mismatches) == 1
    assert "no edge atom exists" in g.rev_edge_mismatches[0].reason


def test_mixed_proof_ids_without_an_explicit_selection_raises():
    text = (
        '!(add-atom &mork (node "p1" "a" "Claim"))\n'
        '!(add-atom &mork (node "p2" "b" "Claim"))\n'
    )
    atoms = extract_graph_atoms(text).graph_atoms
    with pytest.raises(GraphBuildError):
        build_graph(atoms)


def test_explicit_proof_id_selection_is_honored_without_requiring_a_match():
    """proof_id is a hint for which graph to build, not itself validated
    against every atom -- passing it lets a caller name the graph even
    before any atom confirms it."""
    atoms = extract_graph_atoms('!(add-atom &mork (node "p1" "a" "Claim"))\n').graph_atoms
    g = build_graph(atoms, proof_id="p1")
    assert g.proof_id == "p1"
    assert "a" in g.nodes


# ---------------------------------------------------------------------------
# render.py
# ---------------------------------------------------------------------------


def test_to_dot_produces_valid_looking_dot_with_every_node_and_edge():
    g = _graph_from(EVEN_SUM_FIXTURE)
    dot = to_dot(g, title=g.proof_id)
    assert dot.startswith("digraph proof {")
    assert dot.rstrip().endswith("}")
    for node_id in g.nodes:
        assert f'"{node_id}"' in dot
    for edge in g.edges.values():
        assert f'"{edge.src_id}" -> "{edge.dst_id}"' in dot


def test_to_dot_escapes_a_quote_in_a_field_value():
    text = (
        '!(add-atom &mork (node "p" "s1" "State"))\n'
        + r'!(add-atom &mork (field "p" "s1" "description" "she said \"hi\""))'
        + "\n"
    )
    atoms = extract_graph_atoms(text).graph_atoms
    g = build_graph(atoms)
    dot = to_dot(g)
    assert 'she said \\"hi\\"' in dot


def test_to_dot_flags_rev_edge_mismatches_visibly():
    text = TEST_GRAPH_FIXTURE.read_text(encoding="utf-8")
    text += '\n!(add-atom &mork (rev-edge "test-proof" "m1" "WRONG_REL" "s1" "e1"))\n'
    atoms = extract_graph_atoms(text).graph_atoms
    g = build_graph(atoms)
    dot = to_dot(g)
    assert "integrity" in dot.lower()


def test_to_dot_never_truncates_a_long_field_value():
    """A long description/statement must be wrapped across lines, never
    cut with '...' -- every word of the original value has to survive
    into the rendered label, just spread over more lines."""
    long_value = "Prove: if n even and m even, then n+m even, for every integer n and m"
    text = (
        '!(add-atom &mork (node "p" "s1" "State"))\n'
        + f'!(add-atom &mork (field "p" "s1" "description" "{long_value}"))\n'
    )
    atoms = extract_graph_atoms(text).graph_atoms
    g = build_graph(atoms)
    dot = to_dot(g)
    # The DOT label wraps with literal '\n' between lines; join what the
    # renderer produced back together and confirm every original word survived.
    for word in long_value.split():
        assert word in dot
    assert "..." not in dot