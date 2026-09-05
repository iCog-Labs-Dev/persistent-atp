"""Run the benchmarks, one process per experiment.

    scripts/with-mork.sh python -m mork.bench --quick
    scripts/with-mork.sh python -m mork.bench --experiment E1
    scripts/with-mork.sh python -m mork.bench --json results.json

MORK's space is process-wide and cannot be emptied, so experiments cannot share
a process: the second would measure whatever the first left behind. The default
run therefore re-executes itself once per experiment, each child inheriting
`LD_PRELOAD` and `MORK_LIBRARY` from this process's environment. `--in-process`
runs one experiment here, and is what the children are given.

Every experiment reports the atom count it saw. A child whose first row shows a
non-zero count where it expects an empty space has been contaminated, and its
numbers should be discarded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from mork.backend import MorkUnavailable

from .experiments import EXPERIMENTS, run
from .harness import Report, Row

# sysexits.h, so a CI step can tell "no library" from "the benchmark broke".
_EX_UNAVAILABLE = 69
_EX_SOFTWARE = 70


class ChildFailed(RuntimeError):
    """A child process did not finish. Carries its exit code, so a missing
    library stays distinguishable from a broken benchmark all the way out."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _run_child(name: str, quick: bool) -> list[dict]:
    """Run one experiment in a fresh process; return its rows."""
    command = [sys.executable, "-m", "mork.bench", "--in-process", name, "--json", "-"]
    if quick:
        command.append("--quick")
    finished = subprocess.run(command, capture_output=True, text=True)
    if finished.returncode != 0:
        for stream in (finished.stdout, finished.stderr):
            if stream.strip():
                print(stream.strip(), file=sys.stderr)
        raise ChildFailed(
            f"{name} exited with code {finished.returncode}", finished.returncode
        )
    return json.loads(finished.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mork.bench")
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
    parser.add_argument(
        "--in-process",
        metavar="NAME",
        help="run one experiment in this process (used by the parent; a space "
        "already written to gives wrong numbers)",
    )
    args = parser.parse_args(argv)

    if args.in_process:
        try:
            report = run(args.in_process, args.quick)
        except MorkUnavailable as exc:
            print(f"MORK is unavailable: {exc}", file=sys.stderr)
            return _EX_UNAVAILABLE
        print(report.to_json() if args.json == "-" else report.render())
        return 0

    names = args.experiment or sorted(EXPERIMENTS)
    collected = Report()

    # With `--json -` stdout must hold the JSON document and nothing else, or a
    # caller redirecting it gets a file that will not parse. The progress lines
    # and the table are for a human either way, so they move to stderr.
    log = sys.stderr if args.json == "-" else sys.stdout

    print(f"MORK backend benchmark{' (quick)' if args.quick else ''}", file=log)
    print("one process per experiment; the space cannot be emptied\n", file=log)

    for name in names:
        title, _ = EXPERIMENTS[name]
        print(f"{name}  {title} ... ", end="", flush=True, file=log)
        try:
            produced = _run_child(name, args.quick)
        except ChildFailed as exc:
            print(f"\n{exc}", file=sys.stderr)
            return exc.code if exc.code == _EX_UNAVAILABLE else _EX_SOFTWARE
        for row in produced:
            collected.add(Row(**row))
        print(f"{len(produced)} rows", file=log)

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
