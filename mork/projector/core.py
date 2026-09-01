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
from typing import Any, Dict, List


def sanitize_value(value: Any) -> str:
    """Sanitize a value for use in MORK atom syntax."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    # Escape quotes and wrap in quotes
    str_val = str(value).replace('"', '\\"')
    return f'"{str_val}"'


def extract_proof_id(node_id: str) -> str:
    """Extract proof_id from a node id (format: proof_id/local_id)."""
    if "/" in node_id:
        return node_id.split("/")[0]
    return node_id


def extract_local_id(node_id: str) -> str:
    """Extract local id from a node id (format: proof_id/local_id)."""
    if "/" in node_id:
        return node_id.split("/", 1)[1]
    return node_id


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
        self.edges: List[Dict[str, Any]] = []
        self.node_state_map: Dict[str, str] = {}  # node_id -> state_local_id
        self.removed_edge_ids: set = set()  # edge_ids retracted by remove_edge

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

        for edge in self.edges:
            if edge.get("edge_id", "") in self.removed_edge_ids:
                continue
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
            self.edges.append({
                "rel": op.get("rel", "RELATED"),
                "src": op.get("src", ""),
                "dst": op.get("dst", ""),
                "edge_id": op.get("edge_id", ""),
                "fields": dict(op.get("fields") or {}),
            })
        elif op_type == "remove_edge":
            self.removed_edge_ids.add(op.get("edge_id", ""))
        else:
            print(f"projector: ignoring unknown op {op_type!r}", file=sys.stderr)

    def _infer_state_references(self):
        """
        Infer state_id for Move and Attempt nodes from edges.

        - Move: state_id comes from PROPOSES edge (src is state, dst is move)
        - Attempt: state_id comes from ON_STATE edge (src is attempt, dst is state)

        The inferred id is emitted as a regular `state_id` field atom.
        """
        for edge in self.edges:
            rel = edge.get("rel", "")
            src = edge.get("src", "")
            dst = edge.get("dst", "")

            if rel == "PROPOSES":
                # src is the state that proposes the move (dst)
                state_local_id = extract_local_id(src)
                self.node_state_map[dst] = state_local_id

            elif rel == "ON_STATE":
                # src is attempt, dst is state
                state_local_id = extract_local_id(dst)
                self.node_state_map[src] = state_local_id

    def _node_to_atoms(self, node_id: str, node_data: Dict[str, Any]) -> List[str]:
        """Convert a node to its node atom plus one field atom per field."""
        label = node_data.get("label", "Node")
        fields = node_data.get("fields", {})

        proof_id = extract_proof_id(node_id)
        local_id = extract_local_id(node_id)

        atoms = [
            f'!(add-atom &mork (node "{proof_id}" "{local_id}" {sanitize_value(label)}))'
        ]

        # Layer scoping: all event-journal nodes are committed atoms.
        atoms.append(
            f'!(add-atom &mork (layer "{proof_id}" "{local_id}" "committed"))'
        )

        # Inject the inferred state reference as a regular field atom
        state_id = self.node_state_map.get(node_id)
        if state_id is not None:
            atoms.append(
                f'!(add-atom &mork (field "{proof_id}" "{local_id}" '
                f'"state_id" "{state_id}"))'
            )

        for field_name, value in fields.items():
            atoms.append(
                f'!(add-atom &mork (field "{proof_id}" "{local_id}" '
                f'{sanitize_value(field_name)} {sanitize_value(value)}))'
            )

        return atoms

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

        proof_id = extract_proof_id(edge_id)
        local_edge_id = extract_local_id(edge_id)
        local_src = extract_local_id(src)
        local_dst = extract_local_id(dst)

        atoms = [
            # Forward edge
            f'!(add-atom &mork (edge "{proof_id}" "{local_edge_id}" '
            f'{sanitize_value(rel)} "{local_src}" "{local_dst}"))',
            # Reverse edge 
            f'!(add-atom &mork (rev-edge "{proof_id}" "{local_dst}" '
            f'{sanitize_value(rel)} "{local_src}" "{local_edge_id}"))',
            # Edge layer
            f'!(add-atom &mork (layer "{proof_id}" '
            f'"edge:{local_edge_id}" "committed"))',
        ]

        for field_name, value in fields.items():
            atoms.append(
                f'!(add-atom &mork (efield "{proof_id}" "{local_edge_id}" '
                f'{sanitize_value(field_name)} {sanitize_value(value)}))'
            )

        return atoms


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


def generate_metta_file(commands: List[str], include_queries: bool = True) -> str:
    """
    Generate a .metta file content from MORK commands.

    Args:
        commands: List of MORK atom commands
        include_queries: Whether to include standard query templates

    Returns:
        A string containing the .metta file content
    """
    lines = [";; Auto-generated MORK atoms from event journal", ""]
    lines.append(";; Schema:")
    lines.append(';;   (node <proof> <id> <label>)')
    lines.append(';;   (field <proof> <id> <name> <value>)')
    lines.append(';;   (edge <proof> <eid> <rel> <src> <dst>)')
    lines.append(';;   (rev-edge <proof> <dst> <rel> <src> <eid>)  ;; generated index')
    lines.append(';;   (efield <proof> <eid> <name> <value>)')
    lines.append(';;   (layer <proof> <entity> <kind>)  ;; committed|speculative')
    lines.append("")

    # Add mork space initialization
    lines.append(";; Initialize MORK space")
    lines.append("!(mm2-exec &mork 1)")
    lines.append("")

    for cmd in commands:
        lines.append(cmd)

    lines.append("")

    # Add standard query templates
    if include_queries:
        lines.append("")
        lines.append(";; ============================================")
        lines.append(";; QUERY SECTION: Match patterns for querying proof data")
        lines.append(";; ============================================")
        lines.append("")
        lines.extend(generate_query_templates())

    return "\n".join(lines)


def generate_query_templates() -> List[str]:
    """
    Generate standard MORK match query templates for proof data.

    These queries provide easy access to common proof information:
    - Open states
    - Claims and their status
    - Moves and their kind
    - Attempts and their workers
    - Edge relationships
    - Complete proof graph summary

    Returns:
        A list of MORK query command strings
    """
    queries = [
        # Query 1: Find all open states
        ';; Query 1: Find all open states',
        '!(match &mork',
        '  (, (node $proof $sid "State")',
        '     (field $proof $sid "status" "open"))',
        '  (open-state $proof $sid))',
        '',
        # Query 2: Find all claims and their status
        ';; Query 2: Find all claims and their status',
        '!(match &mork',
        '  (, (node $proof $cid "Claim")',
        '     (field $proof $cid "statement" $stmt)',
        '     (field $proof $cid "status" $status))',
        '  (claim-info $proof $cid $stmt $status))',
        '',
        # Query 3: Find all moves and their kind
        ';; Query 3: Find all moves and their kind',
        '!(match &mork',
        '  (, (node $proof $mid "Move")',
        '     (field $proof $mid "state_id" $state)',
        '     (field $proof $mid "status" $status)',
        '     (field $proof $mid "kind" $kind)',
        '     (field $proof $mid "summary" $summary))',
        '  (move-info $proof $mid $state $status $kind $summary))',
        '',
        # Query 4: Find all attempts and their workers
        ';; Query 4: Find all attempts and their workers',
        '!(match &mork',
        '  (, (node $proof $aid "Attempt")',
        '     (field $proof $aid "state_id" $state)',
        '     (field $proof $aid "worker" $worker)',
        '     (field $proof $aid "move_summary" $summary))',
        '  (attempt-info $proof $aid $state $worker $summary))',
        '',
        # Query 5: Find all edges
        ';; Query 5: Find all edges',
        '!(match &mork',
        '  (edge $proof $eid $rel $src $dst)',
        '  (edge-info $eid $rel $src $dst))',
        '',
        # Query 6: Find supported attempts (completed work)
        ';; Query 6: Find supported attempts (completed work)',
        '!(match &mork',
        '  (, (node $proof $aid "Attempt")',
        '     (field $proof $aid "status" "supported")',
        '     (field $proof $aid "worker" $worker)',
        '     (field $proof $aid "move_summary" $summary))',
        '  (supported-attempt $proof $aid $worker $summary))',
        '',
       
    ]
    return queries
