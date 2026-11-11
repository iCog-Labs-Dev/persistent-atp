"""Command-line entry point: `python -m mork.visualizer <fixture.metta>`.

Usage:
    python -m mork.visualizer mork/proofs/even-sum-proof.metta
    python -m mork.visualizer mork/proofs/even-sum-proof.metta -o out.png
    python -m mork.visualizer mork/proofs/even-sum-proof.metta --proof even-sum-proof

Always writes a .dot file. If a `dot` binary is available (from Graphviz)
and an image output path is given, also renders it -- but the .dot file
alone is a complete, useful artifact even without Graphviz installed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .graph import build_graph
from .parser import extract_graph_atoms
from .render import to_dot


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render a .metta fixture as a graph.")
    parser.add_argument("fixture", type=Path, help="path to a .metta file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="image output path (.png, .svg, ...); requires the `dot` CLI",
    )
    parser.add_argument(
        "--dot-out", type=Path, default=None,
        help="where to write the .dot source (defaults next to the fixture)",
    )
    parser.add_argument(
        "--proof", default=None,
        help="select one proof_id if the file contains more than one",
    )
    args = parser.parse_args(argv)

    text = args.fixture.read_text(encoding="utf-8")
    result = extract_graph_atoms(text)

    if result.unrecognized_add_atom_heads:
        print(
            f"warning: {len(result.unrecognized_add_atom_heads)} add-atom form(s) "
            f"with an unrecognized head were skipped: "
            f"{sorted(set(result.unrecognized_add_atom_heads))}",
            file=sys.stderr,
        )

    graph = build_graph(result.graph_atoms, proof_id=args.proof)

    if graph.rev_edge_mismatches:
        print(
            f"warning: {len(graph.rev_edge_mismatches)} rev-edge integrity "
            f"mismatch(es) found -- this usually means a bug in whatever "
            f"generated this fixture:",
            file=sys.stderr,
        )
        for m in graph.rev_edge_mismatches:
            print(f"  - {m.reason}", file=sys.stderr)

    dot_source = to_dot(graph, title=graph.proof_id)
    dot_path = args.dot_out or args.fixture.with_suffix(".dot")
    dot_path.write_text(dot_source, encoding="utf-8")
    print(f"wrote {dot_path}")

    if args.output is not None:
        if shutil.which("dot") is None:
            print(
                "error: --output requires the Graphviz `dot` CLI, which "
                "isn't on PATH. The .dot file above can still be rendered "
                "elsewhere (e.g. https://dreampuf.github.io/GraphvizOnline/).",
                file=sys.stderr,
            )
            return 1
        fmt = args.output.suffix.lstrip(".") or "png"
        subprocess.run(
            ["dot", f"-T{fmt}", str(dot_path), "-o", str(args.output)], check=True
        )
        print(f"wrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())