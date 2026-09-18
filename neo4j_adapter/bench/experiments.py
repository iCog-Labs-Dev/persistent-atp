"""The seven experiments, rewritten for the Neo4j backend.

Each experiment wipes its proof namespace(s) from Neo4j before running, so
experiments can share a process -- Neo4j is external state, not process-scoped
like MORK's atom space.

E1 is the one the rest exists to contextualise. It asks whether nodes belonging
to other proofs slow down queries against this one -- the question raised by a
shared Neo4j database. A flat curve means proof_id indexing isolates proofs. A
rising curve means per-proof databases are a requirement, not an optimisation.

E5 is the direct MORK comparison: same proposals committed through the same gate
and journal, but the view is MemoryView (dicts) vs Neo4jProjector (Bolt+Cypher).
The difference is the Neo4j contribution to commit latency.
"""

from __future__ import annotations

import gc
import os
from typing import Callable

from commit_gate.gate import CommitGate
from commit_gate.state import MemoryView
from commit_gate.store import JournalStore

from neo4j_adapter.adapter import Neo4jAdapter
from neo4j_adapter.projector import Neo4jProjector

from commit_gate.apply import apply_ops

from .harness import Report, Row, measure, peak_rss_mb, timed
from .workload import load, node_id, proposals, seeded

__all__ = ["EXPERIMENTS", "run"]

TARGET = "target"

_TARGET_NODES = 50
_BG_NODES_EACH = 25


def _neo4j() -> Neo4jAdapter:
    """Open a fresh adapter from environment variables."""
    return Neo4jAdapter(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", ""),
    )


def _wipe(adapter: Neo4jAdapter, *proof_ids: str) -> None:
    """Delete all nodes for the given proof namespaces from Neo4j."""
    for pid in proof_ids:
        adapter._write_all(
            "_wipe",
            [
                ("MATCH (n) WHERE n.proof_id = $pid DETACH DELETE n", {"pid": pid}),
                (
                    "MATCH (w:ProjectionWatermark {proof_id: $pid}) DELETE w",
                    {"pid": pid},
                ),
            ],
        )


def _count(adapter: Neo4jAdapter, proof_id: str) -> int:
    """Nodes in Neo4j for this proof. Analog of MORK's space.atoms()."""
    return adapter._read_value(
        "_count",
        "MATCH (n {proof_id: $pid}) RETURN count(n) AS c",
        {"pid": proof_id},
        "c",
        default=0,
    )


def _count_all(adapter: Neo4jAdapter) -> int:
    """Total nodes across all proofs in the database."""
    return adapter._read_value(
        "_count_all",
        "MATCH (n) WHERE n.proof_id IS NOT NULL RETURN count(n) AS c",
        {},
        "c",
        default=0,
    )


def _probes(
    adapter: Neo4jAdapter, projector: Neo4jProjector, proof_id: str, probe: str
) -> list[tuple[str, Callable[[], object]]]:
    """The reads a validator performs, as timeable closures.

    Shared by E1 and E2 so the two sweeps differ only in their load; a
    difference in probes would make the curves incomparable.
    """
    return [
        ("get_state()", lambda: adapter.get_state(probe, proof_id)),
        (
            "get_moves_for_state()",
            lambda: adapter.get_moves_for_state(probe, proof_id),
        ),
        ("eligible_frontier()", lambda: adapter.eligible_frontier(proof_id)),
        ("watermark()", lambda: projector.watermark(proof_id)),
    ]


# --------------------------------------------------------------------- E1

def e1_cross_proof(report: Report, quick: bool) -> None:
    """Does unrelated data slow this proof down?

    The target proof is built once and never changes. Background proofs are
    added in rounds, and after each round the same four queries run again.
    Only the background differs between rounds, so any change in timing is
    the background's doing.
    """
    rng = seeded()
    adapter = _neo4j()
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)
    projector = Neo4jProjector(adapter, store)

    bg_proof_ids: list[str] = []

    try:
        _wipe(adapter, TARGET)

        token = store.acquire_lease(TARGET, "L1")
        load(gate, projector, proposals(TARGET, _TARGET_NODES, rng, lease=("L1", token)))

        rounds = [0, 250, 1000] if quick else [0, 500, 2500, 10000, 25000]
        probe = node_id(TARGET, _TARGET_NODES // 2)
        background_nodes = 0
        background_proofs = 0

        for wanted_background in rounds:
            added = (wanted_background - background_nodes) // _BG_NODES_EACH
            if added > 0:
                for i in range(added):
                    bg_id = f"bg{background_proofs + i}"
                    bg_store = JournalStore(":memory:")
                    bg_view = MemoryView()
                    bg_gate = CommitGate(bg_view, bg_store)
                    bg_proj = Neo4jProjector(adapter, bg_store)
                    bg_token = bg_store.acquire_lease(bg_id, "L1")
                    load(
                        bg_gate,
                        bg_proj,
                        proposals(bg_id, _BG_NODES_EACH, rng, lease=("L1", bg_token)),
                    )
                    bg_proof_ids.append(bg_id)
                background_proofs += added
                background_nodes += added * _BG_NODES_EACH

            nodes = _count_all(adapter)
            for name, call in _probes(adapter, projector, TARGET, probe):
                median, p95, iterations = measure(call, iterations=30 if quick else 50)
                report.add(
                    Row(
                        "E1",
                        name,
                        background_nodes,
                        nodes,
                        median,
                        p95,
                        iterations,
                        note=f"{background_proofs} background proofs; target fixed at "
                        f"{_TARGET_NODES} nodes",
                    )
                )
    finally:
        _wipe(adapter, TARGET, *bg_proof_ids)
        adapter.close()


# --------------------------------------------------------------------- E2

def e2_own_growth(report: Report, quick: bool) -> None:
    """What does one proof cost as it grows?

    Same probes as E1, but now the growth is inside the proof being queried.
    """
    rng = seeded()
    adapter = _neo4j()
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)
    projector = Neo4jProjector(adapter, store)

    try:
        _wipe(adapter, TARGET)
        sizes = [50, 250, 1000] if quick else [100, 500, 2500, 10000]
        committed = 0

        for nodes in sizes:
            token = store.acquire_lease(TARGET, "L1")
            load(gate, projector, proposals(TARGET, nodes - committed, rng, lease=("L1", token)))
            committed = nodes

            node_count = _count(adapter, TARGET)
            probe = node_id(TARGET, nodes // 2)
            for name, call in _probes(adapter, projector, TARGET, probe):
                median, p95, iterations = measure(call, iterations=30 if quick else 50)
                report.add(Row("E2", name, nodes, node_count, median, p95, iterations))
    finally:
        _wipe(adapter, TARGET)
        adapter.close()


# --------------------------------------------------------------------- E3

def e3_per_operation(report: Report, quick: bool) -> None:
    """Which operation dominates, at one fixed size?"""
    rng = seeded()
    adapter = _neo4j()
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)
    projector = Neo4jProjector(adapter, store)

    try:
        _wipe(adapter, TARGET)
        nodes = 250 if quick else 2000
        token = store.acquire_lease(TARGET, "L1")
        load(gate, projector, proposals(TARGET, nodes, rng, lease=("L1", token)))
        node_count = _count(adapter, TARGET)
        probe = node_id(TARGET, nodes // 2)
        iterations = 30 if quick else 50

        counter = iter(range(1_000_000))

        cases: list[tuple[str, Callable[[], object], str]] = [
            (
                "get_state()",
                lambda: adapter.get_state(probe, TARGET),
                "one index lookup by proof_id+id",
            ),
            (
                "get_moves_for_state()",
                lambda: adapter.get_moves_for_state(probe, TARGET),
                "grows with moves proposed from this state",
            ),
            (
                "add_state() new",
                lambda: adapter.add_state(
                    TARGET,
                    node_id(TARGET, 900_000 + next(counter)),
                    f"bench state {next(counter)}",
                ),
                "MERGE + HAS_STATE edge; grows the graph as it runs",
            ),
            (
                "update_state_status() new field",
                lambda: adapter.update_state_status(TARGET, probe, "open"),
                "SET on matched node",
            ),
            (
                "update_state_status() overwrite",
                lambda: adapter.update_state_status(TARGET, probe, "expanded"),
                "SET overwriting existing status",
            ),
            (
                "add_move() new",
                lambda: adapter.add_move(
                    TARGET,
                    node_id(TARGET, 800_000 + next(counter)),
                    probe,
                    f"bench move {next(counter)}",
                ),
                "MERGE Move + PROPOSES edge",
            ),
            (
                "eligible_frontier()",
                lambda: adapter.eligible_frontier(TARGET),
                "filtered MATCH across all moves for this proof",
            ),
            (
                "watermark()",
                lambda: projector.watermark(TARGET),
                "single node lookup",
            ),
        ]

        for name, call, note in cases:
            median, p95, count = measure(call, iterations=iterations)
            report.add(Row("E3", name, nodes, node_count, median, p95, count, note=note))
    finally:
        _wipe(adapter, TARGET)
        adapter.close()


# --------------------------------------------------------------------- E4

def e4_bolt_floor(report: Report, quick: bool) -> None:
    """What does one Bolt round-trip cost at all?

    A MATCH that cannot succeed, against a database of a given size. Everything
    in E3 is at least this expensive; this is what makes E3's numbers
    attributable: a slow operation is either many round-trips or one slow query.
    """
    rng = seeded()
    adapter = _neo4j()
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)
    projector = Neo4jProjector(adapter, store)

    try:
        _wipe(adapter, TARGET)
        sizes = [0, 250, 1000] if quick else [0, 500, 2500, 10000]
        committed = 0

        for nodes in sizes:
            if nodes > committed:
                token = store.acquire_lease(TARGET, "L1")
                load(gate, projector, proposals(TARGET, nodes, rng, lease=("L1", token)))
                committed = nodes

            node_count = _count_all(adapter)

            median, p95, iterations = measure(
                lambda: adapter._read_value(
                    "e4_no_match",
                    "MATCH (n {proof_id: 'nosuchproof'}) RETURN count(n) AS c",
                    {},
                    "c",
                    default=0,
                ),
                iterations=50 if quick else 100,
            )
            report.add(
                Row(
                    "E4",
                    "MATCH, no results",
                    nodes,
                    node_count,
                    median,
                    p95,
                    iterations,
                    note="floor for every read; one Bolt round-trip",
                )
            )

            median, p95, iterations = measure(
                lambda: adapter._write_all(
                    "e4_noop_write",
                    [("MERGE (n:BenchNoop {id: 'noop'})", {})],
                ),
                iterations=50 if quick else 100,
            )
            report.add(
                Row(
                    "E4",
                    "MERGE noop write",
                    nodes,
                    node_count,
                    median,
                    p95,
                    iterations,
                    note="floor for every write; re-merges same node so graph does not grow",
                )
            )
    finally:
        _wipe(adapter, TARGET)
        adapter._write_all("_wipe_noop", [("MATCH (n:BenchNoop) DELETE n", {})])
        adapter.close()


# --------------------------------------------------------------------- E5

def e5_commit_latency(report: Report, quick: bool) -> None:
    """What does Neo4j add to a commit?

    The same proposals are committed twice, against two backends that differ
    only in where state lands: `MemoryView` is dictionaries, `Neo4jProjector`
    is Bolt+Cypher. The journal, the validators and the ops are identical, so
    the difference between the two rows is Neo4j's contribution.
    """
    count = 60 if quick else 300
    warmup = 10

    for backend in ("MemoryView", "Neo4jProjector"):
        rng = seeded()
        store = JournalStore(":memory:")
        proof = f"commit-{backend.lower()}"

        gate_view = MemoryView()
        if backend == "MemoryView":
            gate = CommitGate(gate_view, store)
            projector = None
            adapter = None
        else:
            adapter = _neo4j()
            _wipe(adapter, proof)
            gate = CommitGate(gate_view, store)
            projector = Neo4jProjector(adapter, store)

        token = store.acquire_lease(proof, "L1")
        batch = proposals(proof, count, rng, lease=("L1", token))
        pending = iter(batch)
        rejected: list[str] = []

        def commit_next() -> None:
            proposal = next(pending)
            result = gate.commit(proposal)
            if not result.accepted:
                rejected.extend(r.reason.value for r in result.rejections)
            else:
                apply_ops(gate_view, list(proposal.ops))
                if projector is not None:
                    projector.catch_up(proof)

        median, p95, iterations = measure(
            commit_next, iterations=count - warmup, warmup=warmup
        )

        if backend == "MemoryView":
            node_count = len(gate_view.nodes)
        else:
            node_count = _count(adapter, proof)

        report.add(
            Row(
                "E5",
                f"commit() {backend}",
                count,
                node_count,
                median,
                p95,
                iterations,
                note="REJECTED: " + ",".join(sorted(set(rejected)))
                if rejected
                else "all accepted",
            )
        )

        if adapter is not None:
            _wipe(adapter, proof)
            adapter.close()


# --------------------------------------------------------------------- E6

def e6_cold_recovery(report: Report, quick: bool) -> None:
    """How long does a cold catch_up take to become current?

    The journal is filled through a MemoryView gate (no projection), so Neo4j
    starts genuinely empty -- the state after a crash. Then `catch_up` replays
    it. Timed once per size since a replay cannot be repeated against the same
    graph without wiping first.
    """
    sizes = [50, 200] if quick else [100, 1000, 5000]

    for event_count in sizes:
        rng = seeded()
        store = JournalStore(":memory:")
        proof = f"replay{event_count}"

        filling_view = MemoryView()
        filling_gate = CommitGate(filling_view, store)
        token = store.acquire_lease(proof, "L1")
        for proposal in proposals(proof, event_count, rng, lease=("L1", token)):
            result = filling_gate.commit(proposal)
            if result.accepted:
                apply_ops(filling_view, list(proposal.ops))

        adapter = _neo4j()
        _wipe(adapter, proof)
        projector = Neo4jProjector(adapter, store)

        try:
            applied, elapsed_ms = timed(lambda: projector.catch_up(proof))
            per_event = elapsed_ms / max(applied, 1)
            node_count = _count(adapter, proof)
            report.add(
                Row(
                    "E6",
                    "catch_up()",
                    event_count,
                    node_count,
                    per_event,
                    per_event,
                    1,
                    unit="ms",
                    note=f"per event; {elapsed_ms:.0f} ms total to replay {applied} events",
                )
            )
        finally:
            _wipe(adapter, proof)
            adapter.close()


# --------------------------------------------------------------------- E7

def e7_node_growth(report: Report, quick: bool) -> None:
    """Does Neo4j node count grow linearly with committed events?

    RSS is server-side and not measurable from Python, so this experiment
    tracks node count instead: how many nodes does Neo4j hold per committed
    event, and does that ratio stay stable as the proof grows?
    """
    rng = seeded()
    adapter = _neo4j()
    store = JournalStore(":memory:")
    view = MemoryView()
    gate = CommitGate(view, store)
    projector = Neo4jProjector(adapter, store)

    try:
        _wipe(adapter, TARGET)
        token = store.acquire_lease(TARGET, "L1")
        sizes = [250, 1000] if quick else [1000, 5000, 20000]
        committed = 0

        for nodes in sizes:
            load(gate, projector, proposals(TARGET, nodes - committed, rng, lease=("L1", token)))
            token = store.acquire_lease(TARGET, "L1")
            committed = nodes

            node_count = _count(adapter, TARGET)
            gc.collect()
            rss = peak_rss_mb()
            report.add(
                Row(
                    "E7",
                    "node count after load",
                    nodes,
                    node_count,
                    node_count,
                    node_count,
                    1,
                    unit="nodes",
                    note=f"{node_count / max(committed, 1):.2f} nodes per committed event; "
                    f"client RSS {rss:.1f} MiB",
                )
            )
    finally:
        _wipe(adapter, TARGET)
        adapter.close()


EXPERIMENTS: dict[str, tuple[str, Callable[[Report, bool], None]]] = {
    "E1": ("unrelated proofs vs query time", e1_cross_proof),
    "E2": ("own proof size vs query time", e2_own_growth),
    "E3": ("cost per operation", e3_per_operation),
    "E4": ("cost of one Bolt round-trip", e4_bolt_floor),
    "E5": ("commit latency, Neo4j vs memory", e5_commit_latency),
    "E6": ("cold catch_up() after a crash", e6_cold_recovery),
    "E7": ("node count growth", e7_node_growth),
}


def run(name: str, quick: bool = False) -> Report:
    """Run one experiment in this process."""
    report = Report()
    EXPERIMENTS[name][1](report, quick)
    return report
