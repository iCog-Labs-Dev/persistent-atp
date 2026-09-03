"""The seven experiments.

Each takes a fresh process, because MORK has no way to empty its space: an
experiment that ran after another would measure whatever the first one left
behind. `__main__` enforces that; running one of these in a process that has
already run another is a measurement error, not a style violation.

E1 is the one the rest exists to contextualise. It asks whether atoms belonging
to other proofs slow down queries against this one -- the question raised by
MORK holding a single process-wide space. A flat curve means the space is
indexed in a way that isolates proofs, and proofs may share a process. A rising
curve means the "shard by theorem" rule in Ben's technical design is a
requirement rather than an optimisation.
"""

from __future__ import annotations

import gc
from typing import Callable

from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mork.backend import MorkSpace, MorkView
from mork.backend.view import encode

from .harness import Report, Row, measure, peak_rss_mb, timed
from .workload import background_atoms, load, node_id, proof_atoms, proposals, seeded

__all__ = ["EXPERIMENTS", "run"]

TARGET = "target"

_TARGET_NODES = 50  # held fixed in E1, so only the background varies
_BG_NODES_EACH = 25  # nodes per background proof: many small proofs, not one big one


def _count(space: MorkSpace) -> int:
    """Atoms in the whole space, not just one proof.

    Taken between measurements, never inside one -- it materialises every atom.
    """
    return len(space.atoms())


def _probes(view: MorkView, probe: str) -> list[tuple[str, Callable[[], object]]]:
    """The reads a validator performs, as timeable closures.

    Shared by E1 and E2 so the two sweeps differ in their load and in nothing
    else; a difference in probes would make the curves incomparable.
    """
    return [
        ("node()", lambda: view.node(probe)),
        ("edges_from()", lambda: view.edges_from(probe, "CHILD_OF")),
        ("edges_to()", lambda: view.edges_to(probe, "CHILD_OF")),
        ("projected_revision()", lambda: view.projected_revision(TARGET)),
    ]


# --------------------------------------------------------------------- E1

def e1_cross_proof(report: Report, quick: bool) -> None:
    """Does unrelated data slow this proof down?

    The target proof is built once and never changes. Background proofs are added
    in rounds, and after each round the same four queries run again. Only the
    background differs between rounds, so any change in timing is the background's
    doing.
    """
    rng = seeded()
    space = MorkSpace()
    view = MorkView(space)

    load(space, proof_atoms(TARGET, _TARGET_NODES, rng))

    rounds = [0, 250, 1000] if quick else [0, 500, 2500, 10000, 25000]
    probe = node_id(TARGET, _TARGET_NODES // 2)
    background_nodes = 0
    background_proofs = 0

    for target_nodes in rounds:
        # Each round continues the proof numbering rather than restarting it, so
        # the space really does grow by what the round claims to add.
        added = (target_nodes - background_nodes) // _BG_NODES_EACH
        if added > 0:
            load(
                space,
                background_atoms(
                    added, _BG_NODES_EACH, rng, first=background_proofs
                ),
            )
            background_proofs += added
            background_nodes += added * _BG_NODES_EACH

        atoms = _count(space)
        for name, call in _probes(view, probe):
            median, p95, iterations = measure(call, iterations=30 if quick else 50)
            report.add(
                Row(
                    "E1",
                    name,
                    background_nodes,
                    atoms,
                    median,
                    p95,
                    iterations,
                    note=f"{background_proofs} background proofs; target fixed at "
                    f"{_TARGET_NODES} nodes",
                )
            )


# --------------------------------------------------------------------- E2

def e2_own_growth(report: Report, quick: bool) -> None:
    """What does one proof cost as it grows?

    Same probes as E1, but now the growth is inside the proof being queried. The
    contrast between the two curves is the whole point: E1 rising and E2 flat
    would be a very different system from both rising.
    """
    rng = seeded()
    space = MorkSpace()
    view = MorkView(space)

    sizes = [50, 250, 1000] if quick else [100, 500, 2500, 10000]
    grown = 0

    for nodes in sizes:
        load(space, proof_atoms(TARGET, nodes, rng, start=grown))
        grown = nodes

        atoms = _count(space)
        probe = node_id(TARGET, nodes // 2)
        for name, call in _probes(view, probe):
            median, p95, iterations = measure(call, iterations=30 if quick else 50)
            report.add(Row("E2", name, nodes, atoms, median, p95, iterations))


# --------------------------------------------------------------------- E3

def e3_per_operation(report: Report, quick: bool) -> None:
    """Which operation dominates, at one fixed size?

    `set_field` appears twice on purpose. Writing a new key is one match that
    finds nothing plus one add. Overwriting is the ask-erase-write path in
    `MorkView._replace`: a match that finds the atom, a remove per hit, then the
    add. The second is the one a status update performs, and the gap between them
    is the cost of MORK reporting a removal as successful whether or not it
    matched -- which is why the code reads before it removes.
    """
    rng = seeded()
    space = MorkSpace()
    view = MorkView(space)

    nodes = 250 if quick else 2000
    load(space, proof_atoms(TARGET, nodes, rng))
    atoms = _count(space)
    probe = node_id(TARGET, nodes // 2)
    iterations = 30 if quick else 50

    # One counter across every case, so no two writes ever land on the same key
    # and each iteration does the work its name claims.
    counter = iter(range(1_000_000))

    cases: list[tuple[str, Callable[[], object], str]] = [
        ("node()", lambda: view.node(probe), "one match for the label, one for fields"),
        ("edges_from()", lambda: view.edges_from(probe, "CHILD_OF"), "grows: see E2"),
        (
            "add_node() new",
            lambda: view.add_node(
                node_id(TARGET, 900_000 + next(counter)),
                "FormalState",
                {"status": "open"},
            ),
            "replaces before it writes; grows the space as it runs",
        ),
        (
            "set_field() new key",
            lambda: view.set_field(probe, f"note{next(counter)}", "v"),
            "match finds nothing, then add",
        ),
        (
            "set_field() overwrite",
            lambda: view.set_field(probe, "depth", next(counter)),
            "ask-erase-write: match, remove, add",
        ),
        (
            "add_edge()",
            lambda: view.add_edge(
                "CHILD_OF", probe, node_id(TARGET, 0), f"{TARGET}/be{next(counter)}"
            ),
            "",
        ),
        ("projected_revision()", lambda: view.projected_revision(TARGET), ""),
        (
            "record_projected()",
            lambda: view.record_projected(TARGET, next(counter) + 1),
            "replaces the mark atom",
        ),
    ]

    for name, call, note in cases:
        median, p95, count = measure(call, iterations=iterations)
        report.add(Row("E3", name, nodes, atoms, median, p95, count, note=note))


# --------------------------------------------------------------------- E4

def e4_ffi_floor(report: Report, quick: bool) -> None:
    """What does crossing the boundary cost at all?

    A match that cannot succeed, against a space of a given size. Everything in
    E3 is at least this expensive, so this is what makes E3's numbers
    attributable: a slow operation is either many crossings or one slow query,
    and this says which.
    """
    rng = seeded()
    space = MorkSpace()

    sizes = [0, 250, 1000] if quick else [0, 500, 2500, 10000]
    grown = 0
    absent = f"(node {encode('nosuchproof')} $id $label)"

    for nodes in sizes:
        load(space, proof_atoms(TARGET, nodes, rng, start=grown))
        grown = nodes
        atoms = _count(space)

        median, p95, iterations = measure(
            lambda: space.match(absent, "$id"), iterations=50 if quick else 100
        )
        report.add(
            Row(
                "E4",
                "match, no results",
                nodes,
                atoms,
                median,
                p95,
                iterations,
                note="floor for every read",
            )
        )

        median, p95, iterations = measure(
            lambda: space.add('(bench "noop" "noop")'), iterations=50 if quick else 100
        )
        report.add(
            Row(
                "E4",
                "add, one atom",
                nodes,
                atoms,
                median,
                p95,
                iterations,
                note="the same atom re-added, so the space does not grow",
            )
        )


# --------------------------------------------------------------------- E5

def e5_commit_latency(report: Report, quick: bool) -> None:
    """What does MORK add to a commit?

    The same proposals are committed twice, against two backends that differ only
    in where state lands: `MemoryView` is dictionaries, `MorkView` is atoms. The
    journal, the validators and the ops are identical, so the difference between
    the two rows is MORK's contribution and nothing else.
    """
    count = 60 if quick else 300
    warmup = 10

    for backend in ("MemoryView", "MorkView"):
        rng = seeded()
        store = JournalStore(":memory:")
        view = MemoryView() if backend == "MemoryView" else MorkView(MorkSpace())
        gate = CommitGate(view, store)

        proof = f"commit-{backend.lower()}"
        token = store.acquire_lease(proof, "L1")
        batch = proposals(proof, count, rng, lease=("L1", token))

        # Every proposal is committed exactly once -- they carry ascending
        # base_revisions, so replaying one would be rejected as stale. The
        # iterator is sized to the warm-up plus the measured run for that reason.
        pending = iter(batch)
        rejected: list[str] = []

        def commit_next() -> None:
            result = gate.commit(next(pending))
            if not result.accepted:
                rejected.extend(r.reason.value for r in result.rejections)

        median, p95, iterations = measure(
            commit_next, iterations=count - warmup, warmup=warmup
        )
        atoms = _count(view._space) if backend == "MorkView" else len(view.nodes)
        report.add(
            Row(
                "E5",
                f"commit() {backend}",
                count,
                atoms,
                median,
                p95,
                iterations,
                note="REJECTED: " + ",".join(sorted(set(rejected)))
                if rejected
                else "all accepted",
            )
        )


# --------------------------------------------------------------------- E6

def e6_cold_recovery(report: Report, quick: bool) -> None:
    """How long does a restarted gate take to become current?

    The journal is filled through a gate that does not project, so the graph
    starts genuinely empty -- the state a process finds itself in after a crash.
    Then `catch_up` replays it. Timed once per size, since a replay cannot be
    repeated against the same space.
    """
    sizes = [50, 200] if quick else [100, 1000, 5000]

    for events in sizes:
        rng = seeded()
        store = JournalStore(":memory:")
        proof = f"replay{events}"

        filling = CommitGate(MemoryView(), store)
        token = store.acquire_lease(proof, "L1")
        for proposal in proposals(proof, events, rng, lease=("L1", token)):
            filling.commit(proposal)

        gate = CommitGate(MorkView(MorkSpace()), store)
        replayed, elapsed_ms = timed(lambda: gate.catch_up(proof))

        count = replayed.get(proof, 0)
        per_event = elapsed_ms / max(count, 1)
        report.add(
            Row(
                "E6",
                "catch_up()",
                events,
                _count(gate.view._space),
                per_event,
                per_event,
                1,
                unit="ms",
                note=f"per event; {elapsed_ms:.0f} ms total to replay {count} events",
            )
        )


# --------------------------------------------------------------------- E7

def e7_memory(report: Report, quick: bool) -> None:
    """Does the space give memory back?

    The space is filled, measured, then emptied of everything it holds, and
    measured again. Peak RSS never falls, so what the second measurement shows is
    whether *further* growth was needed -- if removal reclaimed nothing, a
    long-lived gate that keeps writing and retracting grows without bound.
    """
    rng = seeded()
    space = MorkSpace()

    sizes = [250, 1000] if quick else [1000, 5000, 20000]
    grown = 0

    for nodes in sizes:
        load(space, proof_atoms(TARGET, nodes, rng, start=grown))
        grown = nodes
        atoms = _count(space)
        gc.collect()
        rss = peak_rss_mb()
        report.add(
            Row(
                "E7",
                "peak RSS after load",
                nodes,
                atoms,
                rss,
                rss,
                1,
                unit="MiB",
                note=f"{rss * 1024 / max(atoms, 1):.2f} KiB per atom",
            )
        )

    held = space.atoms()
    _, elapsed_ms = timed(lambda: [space.remove(atom) for atom in held])
    remaining = _count(space)
    gc.collect()
    rss = peak_rss_mb()
    report.add(
        Row(
            "E7",
            "peak RSS after remove",
            grown,
            remaining,
            rss,
            rss,
            1,
            unit="MiB",
            note=f"removed {len(held)} atoms in {elapsed_ms:.0f} ms; "
            f"{remaining} left in the space",
        )
    )


EXPERIMENTS: dict[str, tuple[str, Callable[[Report, bool], None]]] = {
    "E1": ("unrelated proofs vs query time", e1_cross_proof),
    "E2": ("own proof size vs query time", e2_own_growth),
    "E3": ("cost per operation", e3_per_operation),
    "E4": ("cost of one FFI crossing", e4_ffi_floor),
    "E5": ("commit latency, MORK vs memory", e5_commit_latency),
    "E6": ("cold catch_up() after a crash", e6_cold_recovery),
    "E7": ("memory growth and reclaim", e7_memory),
}


def run(name: str, quick: bool = False) -> Report:
    """Run one experiment in this process. The caller must supply a fresh one."""
    report = Report()
    EXPERIMENTS[name][1](report, quick)
    return report
