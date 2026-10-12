"""Timing, statistics, and result reporting.

Three decisions worth stating.

**Median and p95, never a mean.** A mean over FFI calls is dominated by whichever
iteration hit an allocation; the median says what a typical call costs and p95
says what the tail looks like. Both are reported because a backend can be fast
and still have a bad tail.

**Warm-up discarded.** The first call into a fresh space pays lazy initialisation
that no later call pays, and a benchmark that averages it in measures startup.

**The atom count travels with the timing.** MORK keeps one space per process with
no way to empty it, so a number is meaningless without knowing what was in the
space when it was taken. Every `Row` carries it.
"""

from __future__ import annotations

import json
import resource
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

__all__ = ["Report", "Row", "measure", "peak_rss_mb", "timed"]


def peak_rss_mb() -> float:
    """Peak resident memory of this process, in MiB.

    `ru_maxrss` is a high-water mark, not current usage: it never falls. That is
    the point in E7 -- it answers "did this process ever need that much", which
    is what a long-lived gate is sized against.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@dataclass(slots=True)
class Row:
    """One measurement, with the conditions it was taken under.

    `scale` is the experiment's independent variable in its own units -- nodes,
    events, proposals -- and `atoms` is what the space actually held when the
    timing was taken. Two columns rather than one because the space deduplicates
    and MORK cannot be emptied, so the size asked for and the size measured are
    not always the same number, and only the second one explains a timing.

    `unit` names what `median` and `p95` are in. Most rows are microseconds per
    call; E6 reports milliseconds per event and E7 reports MiB, and a reader of
    the JSON should not have to infer that from the note.

    A one-shot measurement -- a replay, an RSS reading -- puts the same value in
    both `median` and `p95` and sets `iterations` to 1. There is no distribution
    to report, and inventing one would be worse than repeating the number.
    """

    experiment: str
    operation: str
    scale: int
    atoms: int
    median: float
    p95: float
    iterations: int
    unit: str = "us"
    note: str = ""

    def render(self) -> str:
        return (
            f"{self.experiment:<4} {self.operation:<22} {self.scale:>8} "
            f"{self.atoms:>9} {self.median:>10.1f} {self.p95:>10.1f} "
            f"{self.unit:<4} {self.note}"
        )


HEADER = (
    f"{'exp':<4} {'operation':<22} {'scale':>8} {'atoms':>9} "
    f"{'median':>10} {'p95':>10} {'unit':<4} notes"
)


@dataclass(slots=True)
class Report:
    """Rows collected by one run, printable and serialisable.

    Both directions are used: a child process serialises its rows to JSON, and
    the parent reads them back into `Row`s and collects them here, so the final
    table is rendered by the same code that would have rendered each child's.
    """

    rows: list[Row] = field(default_factory=list)

    def add(self, row: Row) -> Row:
        self.rows.append(row)
        return row

    def render(self) -> str:
        lines = [HEADER, "-" * len(HEADER)]
        lines.extend(row.render() for row in self.rows)
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps([asdict(row) for row in self.rows], indent=2)


def measure(
    call: Callable[[], Any],
    *,
    iterations: int = 50,
    warmup: int = 5,
) -> tuple[float, float, int]:
    """Time `call` repeatedly; return `(median, p95, iterations)` in microseconds.

    `call` takes no arguments and its result is discarded -- bind arguments in a
    closure at the call site, so the timed region holds nothing but the call.
    """
    for _ in range(warmup):
        call()

    samples: list[int] = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        call()
        samples.append(time.perf_counter_ns() - start)

    samples.sort()  # for the p95 index; the median does not care
    median = statistics.median(samples) / 1000
    p95 = samples[min(int(len(samples) * 0.95), len(samples) - 1)] / 1000
    return median, p95, iterations


def timed(call: Callable[[], Any]) -> tuple[Any, float]:
    """Run `call` once; return `(result, elapsed_ms)`.

    For the things that happen once and take a while -- a bulk load, a cold
    replay -- where a distribution over repeats would mean nothing.
    """
    start = time.perf_counter_ns()
    result = call()
    return result, (time.perf_counter_ns() - start) / 1_000_000
