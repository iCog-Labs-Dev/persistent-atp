"""Renders a Graph (see graph.py) as Graphviz DOT.
"""

from __future__ import annotations

from typing import Dict, List

from .graph import Graph

# One color per label actually seen in the fixtures today. An unknown label
# still renders -- just in a neutral fallback color -- rather than failing,
# since a future rules module may introduce new node types.
_LABEL_COLORS: Dict[str, str] = {
    "State": "#8ecae6",
    "Move": "#ffb703",
    "Claim": "#219ebc",
    "Attempt": "#fb8500",
    "Certificate": "#06d6a0",
    "LeanReplay": "#118ab2",
    "Alignment": "#ef476f",
}
_FALLBACK_COLOR = "#adb5bd"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _wrap(text: str, width: int = 30) -> list:
    """Word-wraps text to a max width per line, never cutting a word or
    discarding information -- unlike truncation, every character survives,
    just spread across more lines."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def _node_label(node_id: str, label: str, fields: dict) -> str:
    lines = [f"{label}: {node_id}"]
    for key in ("description", "statement", "summary", "status"):
        if key in fields:
            value = fields[key]
            wrapped = _wrap(value)
            lines.append(f"{key}: {wrapped[0]}")
            lines.extend(wrapped[1:])
    return "\\n".join(_escape(line) for line in lines)


def to_dot(graph: Graph, *, title: str = "") -> str:
    lines = ["digraph proof {", '  rankdir="TB";', '  node [shape=box, style="rounded,filled", fontname="Helvetica"];']
    if title:
        lines.append(f'  labelloc="t"; label="{_escape(title)}";')

    for node in graph.nodes.values():
        color = _LABEL_COLORS.get(node.label, _FALLBACK_COLOR)
        style = "rounded,filled" if node.layer != "speculative" else "rounded,filled,dashed"
        label = _node_label(node.node_id, node.label, node.fields)
        lines.append(
            f'  "{_escape(node.node_id)}" [label="{label}", fillcolor="{color}", style="{style}"];'
        )

    for edge in graph.edges.values():
        edge_style = "solid" if edge.layer != "speculative" else "dashed"
        lines.append(
            f'  "{_escape(edge.src_id)}" -> "{_escape(edge.dst_id)}" '
            f'[label="{_escape(edge.rel_type)}", style="{edge_style}"];'
        )

    if graph.rev_edge_mismatches:
        note = f"{len(graph.rev_edge_mismatches)} rev-edge integrity mismatch(es) -- see console output"
        lines.append(f'  "__integrity_warning__" [label="{_escape(note)}", shape=note, fillcolor="#ffadad"];')

    lines.append("}")
    return "\n".join(lines)