"""Builds a graph model from the atoms parser.py extracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Dict, List, Optional, Tuple

from .parser import SExpr


@dataclass
class GraphNode:
    proof_id: str
    node_id: str
    label: str
    fields: Dict[str, str] = dataclass_field(default_factory=dict)
    layer: Optional[str] = None  # "committed" | "speculative" | None if unseen


@dataclass
class GraphEdge:
    proof_id: str
    edge_id: str
    rel_type: str
    src_id: str
    dst_id: str
    fields: Dict[str, str] = dataclass_field(default_factory=dict)
    layer: Optional[str] = None


@dataclass
class RevEdgeMismatch:
    """A rev-edge atom that doesn't correspond to any real edge atom --
    almost certainly a bug in whatever produced this fixture, not a
    legitimate state to render."""

    proof_id: str
    dst_id: str
    rel_type: str
    src_id: str
    edge_id: str
    reason: str


@dataclass
class Graph:
    proof_id: str
    nodes: Dict[str, GraphNode]
    edges: Dict[str, GraphEdge]
    rev_edge_mismatches: List[RevEdgeMismatch]


class GraphBuildError(ValueError):
    """The atoms don't describe a coherent graph (e.g. span >1 proof_id)."""


def _require_str(atom, index: int, atom_repr: str) -> str:
    if not isinstance(atom, str):
        raise GraphBuildError(f"expected a plain value at position {index} in {atom_repr}")
    return atom


def build_graph(graph_atoms: List[SExpr], *, proof_id: Optional[str] = None) -> Graph:
    """Builds one Graph from a flat list of graph atoms.

    If `proof_id` is None, it's inferred from the atoms themselves -- and
    every atom must agree on it. A `.metta` fixture describing more than one
    proof is a real possibility in principle (nothing in the schema forbids
    it), so this is checked rather than assumed away.
    """
    nodes: Dict[str, GraphNode] = {}
    edges: Dict[str, GraphEdge] = {}
    layers: Dict[Tuple[str, str], str] = {}  # (proof_id, entity_key) -> layer kind
    rev_edges: List[Tuple[str, str, str, str, str]] = []  # proof, dst, rel, src, eid
    inferred_proof_id = proof_id

    def _check_proof(candidate: str, atom_repr: str) -> None:
        nonlocal inferred_proof_id
        if inferred_proof_id is None:
            inferred_proof_id = candidate
        elif inferred_proof_id != candidate:
            raise GraphBuildError(
                f"atom for proof_id {candidate!r} found while building a graph "
                f"for {inferred_proof_id!r} -- pass proof_id explicitly to select "
                f"one, or split the file: {atom_repr}"
            )

    # First pass: nodes, edges, and layers. rev-edge is deferred to a second
    # pass so it can be checked against a fully-built edge set.
    for atom in graph_atoms:
        args = atom.args
        if atom.head == "node":
            pid, nid, label = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            nodes[nid] = GraphNode(proof_id=pid, node_id=nid, label=label)
        elif atom.head == "field":
            pid, nid, name, value = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            if nid in nodes:
                nodes[nid].fields[name] = value
        elif atom.head == "edge":
            pid, eid, rel, src, dst = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            edges[eid] = GraphEdge(proof_id=pid, edge_id=eid, rel_type=rel, src_id=src, dst_id=dst)
        elif atom.head == "efield":
            pid, eid, name, value = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            if eid in edges:
                edges[eid].fields[name] = value
        elif atom.head == "layer":
            pid, entity, kind = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            layers[(pid, entity)] = kind
        elif atom.head == "rev-edge":
            pid, dst, rel, src, eid = (_require_str(a, i, repr(atom)) for i, a in enumerate(args))
            _check_proof(pid, repr(atom))
            rev_edges.append((pid, dst, rel, src, eid))
        else:
            raise GraphBuildError(f"unhandled graph atom head: {atom.head!r}")

    if inferred_proof_id is None:
        inferred_proof_id = ""

    for (pid, entity), kind in layers.items():
        if entity in nodes:
            nodes[entity].layer = kind
        elif entity.startswith("edge:") and entity[len("edge:") :] in edges:
            edges[entity[len("edge:") :]].layer = kind

    mismatches = _check_rev_edges(rev_edges, edges)

    return Graph(
        proof_id=inferred_proof_id, nodes=nodes, edges=edges, rev_edge_mismatches=mismatches
    )


def _check_rev_edges(
    rev_edges: List[Tuple[str, str, str, str, str]], edges: Dict[str, GraphEdge]
) -> List[RevEdgeMismatch]:
    mismatches: List[RevEdgeMismatch] = []
    for pid, dst, rel, src, eid in rev_edges:
        real = edges.get(eid)
        if real is None:
            mismatches.append(
                RevEdgeMismatch(pid, dst, rel, src, eid, reason=f"no edge atom exists for {eid!r}")
            )
        elif (real.rel_type, real.src_id, real.dst_id) != (rel, src, dst):
            mismatches.append(
                RevEdgeMismatch(
                    pid, dst, rel, src, eid,
                    reason=(
                        f"edge {eid!r} is actually ({real.rel_type!r}, "
                        f"{real.src_id!r} -> {real.dst_id!r}), not "
                        f"({rel!r}, {src!r} -> {dst!r})"
                    ),
                )
            )
    return mismatches