"""Run the Neo4j backend benchmarks.

    python -m neo4j_adapter.bench --quick
    python -m neo4j_adapter.bench --experiment E5
    python -m neo4j_adapter.bench --json results.json

Neo4j is external state, so experiments can share a process -- each one wipes
its own proof namespace before running. No subprocess-per-experiment needed.

Requires a reachable Neo4j instance. Connection is configured via environment
variables (NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD) or .env file.
"""

from __future__ import annotations

import argparse
import sys

from .experiments import EXPERIMENTS, run
from .harness import Report, Row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m neo4j_adapter.bench")
    parser.add_argument(
        "--experiment",
        action="append",
        choices=sorted(EXPERIMENTS),
        help="run only this experiment; repeatable. Default is all of them.",
    )
    parser.add_argument(
        "--quick", action="store_true", help="smaller sizes, for a fast check"
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="write rows as JSON; '-' means stdout, and the table moves to stderr",
    )
    args = parser.parse_args(argv)

    names = args.experiment or sorted(EXPERIMENTS)
    collected = Report()

    log = sys.stderr if args.json == "-" else sys.stdout

    print(f"Neo4j backend benchmark{' (quick)' if args.quick else ''}", file=log)
    print("NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD control the connection\n", file=log)

    for name in names:
        title, _ = EXPERIMENTS[name]
        print(f"{name}  {title} ... ", end="", flush=True, file=log)
        try:
            report = run(name, args.quick)
        except Exception as exc:
            print(f"\n{name} failed: {exc}", file=sys.stderr)
            return 1
        for row in report.rows:
            collected.add(row)
        print(f"{len(report.rows)} rows", file=log)

    print(file=log)
    print(collected.render(), file=log)

    if args.json == "-":
        print(collected.to_json())
    elif args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(collected.to_json())
        print(f"\nwrote {len(collected.rows)} rows to {args.json}", file=log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
