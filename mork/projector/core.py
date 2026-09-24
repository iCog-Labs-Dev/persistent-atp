"""
projector.py — Project event journal JSON to MORK atoms

This module reads an event journal (JSON) and generates MORK atom commands.

The event journal carries the four operations of the commit-gate op algebra
(mirroring src/commit_gate/ops.py):
- upsert_node:  Create a node (State, Claim, Move, Attempt, etc.)
- set_field:    Overwrite one mutable field on an existing node
- add_edge:     Create a relationship between nodes, optionally with fields
- remove_edge:  Retract an edge by its stable id

The projector transforms these into normalized atoms with fixed arity:
!(add-atom &mork (node "proof_id" "local_id" "Label"))
!(add-atom &mork (field "proof_id" "local_id" "field_name" value))
!(add-atom &mork (edge "proof_id" "edge_id" "REL" "src_local" "dst_local"))
!(add-atom &mork (efield "proof_id" "edge_id" "field_name" value))

Reverse-edge index:
  For every forward edge, a reverse atom is emitted:
    !(add-atom &mork (rev-edge "proof_id" "dst" "REL" "src" "edge_id"))
  Reverse edges are first-class generated indexes: they allow O(1) lookups
  of incoming edges per node without scanning the full edge table.

Layer scoping:
  Every projected node carries a layer atom:
    !(add-atom &mork (layer "proof_id" "entity_id" "committed"))
  Committed atoms may serve as established premises; speculative atoms
  (Hyperon hypotheses, uncriticized proposals) can affect scheduling but
  never establish facts.

"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any, Dict, List

from mork.atoms import (
    edge_atoms,
    encode,
    extract_local_id,
    extract_proof_id,
    node_atoms,
    to_add_command,
)


def sanitize_value(value: Any) -> str:
    """Backward-compatible name for the canonical atom-slot encoder."""
    return encode(value)


class Projector:
    """
    Projects an event journal to MORK atom commands.

    Uses a two-pass approach:
    1. First pass: collect all nodes and edges, applying ops in revision order
       (set_field mutates collected fields; removed edges are marked)
    2. Infer state_id for Move and Attempt nodes from edges
    3. Generate normalized MORK atoms
    """

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}  # node_id -> {label, fields}
        self.edges: Dict[str, Dict[str, Any]] = {}
        self.node_state_map: Dict[str, str] = {}  # node_id -> state_local_id

    def process_event_journal(self, journal_data: Dict[str, Any]) -> List[str]:
        """Process the entire journal and return MORK commands."""
        events = journal_data.get("events", [])
        sorted_events = sorted(events, key=lambda e: e.get("revision", 0))

        # First pass: collect all nodes and edges
        for event in sorted_events:
            payload = event.get("payload", {})
            ops = payload.get("ops", [])
            for op in ops:
                self._collect_operation(op)

        # Second pass: infer state_id for Move and Attempt from edges
        self._infer_state_references()

        # Third pass: generate MORK commands
        commands = []
        for node_id, node_data in self.nodes.items():
            commands.extend(self._node_to_atoms(node_id, node_data))

        for edge in self.edges.values():
            commands.extend(self._edge_to_atoms(edge))

        return commands

    def _collect_operation(self, op: Dict[str, Any]):
        """Collect nodes and edges from an operation."""
        op_type = op.get("op", "")

        if op_type == "upsert_node":
            node_id = op.get("id", "")
            label = op.get("label", "Node")
            fields = op.get("fields", {})
            self.nodes[node_id] = {
                "label": label,
                "fields": dict(fields),
            }
        elif op_type == "set_field":
            node_id = op.get("id", "")
            if node_id not in self.nodes:
                print(
                    f"projector: set_field for unknown node {node_id!r}, ignoring",
                    file=sys.stderr,
                )
                return
            field_name = op.get("field", "")
            self.nodes[node_id]["fields"][field_name] = op.get("value")
        elif op_type == "add_edge":
            edge_id = op.get("edge_id", "")
            self.edges[edge_id] = {
                "rel": op.get("rel", "RELATED"),
                "src": op.get("src", ""),
                "dst": op.get("dst", ""),
                "edge_id": edge_id,
                "fields": dict(op.get("fields") or {}),
            }
        elif op_type == "remove_edge":
            self.edges.pop(op.get("edge_id", ""), None)
        else:
            print(f"projector: ignoring unknown op {op_type!r}", file=sys.stderr)

    def _infer_state_references(self):
        """
        Infer state_id for Move and Attempt nodes from edges.

        - Move: state_id comes from PROPOSES edge (src is state, dst is move)
        - Attempt: state_id comes from ON_STATE edge (src is attempt, dst is state)

        The inferred id is emitted as a regular `state_id` field atom.
        """
        candidates: dict[str, set[str]] = defaultdict(set)
        for edge in self.edges.values():
            rel = edge.get("rel", "")
            src = edge.get("src", "")
            dst = edge.get("dst", "")

            if rel == "PROPOSES" and self.nodes.get(dst, {}).get("label") == "Move":
                candidates[dst].add(extract_local_id(src))
            elif rel == "ON_STATE" and self.nodes.get(src, {}).get("label") == "Attempt":
                candidates[src].add(extract_local_id(dst))

        self.node_state_map = {
            node_id: next(iter(state_ids))
            for node_id, state_ids in candidates.items()
            if len(state_ids) == 1
        }

    def _node_to_atoms(self, node_id: str, node_data: Dict[str, Any]) -> List[str]:
        """Convert a node to its node atom plus one field atom per field."""
        label = node_data.get("label", "Node")
        fields = node_data.get("fields", {})

        return [
            to_add_command(atom)
            for atom in node_atoms(
                node_id,
                label,
                fields,
                derived_state_id=self.node_state_map.get(node_id),
            )
        ]

    def _edge_to_atoms(self, edge: Dict[str, Any]) -> List[str]:
        """Convert an edge to its forward + reverse atoms plus efields.

        Every forward edge atom generates a corresponding reverse-edge atom
        : the reverse index allows O(1) lookups of incoming
        edges per node.

        A layer atom is emitted for each edge so edge provenance is
        independently queryable (§4.2).
        """
        rel = edge.get("rel", "RELATED")
        src = edge.get("src", "")
        dst = edge.get("dst", "")
        edge_id = edge.get("edge_id", "")
        fields = edge.get("fields", {})

        return [
            to_add_command(atom)
            for atom in edge_atoms(rel, src, dst, edge_id, fields)
        ]


def project_event_journal(journal_data: Dict[str, Any]) -> List[str]:
    """
    Project an event journal to a list of MORK atom commands.

    Args:
        journal_data: The parsed event journal JSON data

    Returns:
        A list of MORK atom command strings
    """
    projector = Projector()
    return projector.process_event_journal(journal_data)


def project_from_file(filepath: str) -> List[str]:
    """
    Load an event journal from a JSON file and project to MORK commands.

    Args:
        filepath: Path to the event journal JSON file

    Returns:
        A list of MORK atom command strings
    """
    with open(filepath, 'r') as f:
        journal_data = json.load(f)
    return project_event_journal(journal_data)


def generate_metta_file(commands: List[str]) -> str:
    """Generate a .metta file content from MORK atom commands."""
    lines = [
        ";; Auto-generated MORK atoms from event journal",
        ";;",
        ";; Schema:",
        ';;   (node <proof> <id> <label>)',
        ';;   (field <proof> <id> <name> <value>)',
        ';;   (edge <proof> <eid> <rel> <src> <dst>)',
        ';;   (rev-edge <proof> <dst> <rel> <src> <eid>)',
        ';;   (efield <proof> <eid> <name> <value>)',
        ';;   (layer <proof> <entity> <kind>)',
        "",
        "!(mm2-exec &mork 1)",
        "",
    ]
    lines.extend(commands)
    return "\n".join(lines)
