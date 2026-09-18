# Neo4j Benchmark Runbook

This package benchmarks the Neo4j projection backend with the same commit gate,
journal, and synthetic proof workload used by the in-memory baseline.

## Requirements

- Python 3.11 or newer. The project `.venv` currently works.
- A reachable Neo4j instance with Bolt enabled.
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and optionally
  `NEO4J_DATABASE` set in the shell or in `.env`.
- Run commands from the repository root with `PYTHONPATH=src:.` so both
  `neo4j_adapter` and the `src/` packages are importable.

The benchmark creates schema constraints on connect. Each experiment wipes only
its own proof namespaces, but it should still be run against a disposable Neo4j
database when collecting publishable numbers.

## Quick Smoke Run

```bash
PYTHONPATH=src:. .venv/bin/python -m neo4j_adapter.bench --quick
```

Run only the direct commit-latency comparison:

```bash
PYTHONPATH=src:. .venv/bin/python -m neo4j_adapter.bench --quick --experiment E5
```

Write machine-readable output:

```bash
mkdir -p neo4j_adapter/bench/results
PYTHONPATH=src:. .venv/bin/python -m neo4j_adapter.bench --quick \
  --json neo4j_adapter/bench/results/neo4j-quick.json
```

Use `--json -` when another script should consume JSON from stdout.

## Full Run

```bash
mkdir -p neo4j_adapter/bench/results
PYTHONPATH=src:. .venv/bin/python -m neo4j_adapter.bench \
  --json neo4j_adapter/bench/results/neo4j-full.json
```

The full run is intentionally larger. Keep the machine quiet while it runs, and
record the Neo4j version, storage location, memory settings, CPU, and whether
Neo4j was local or remote.

## Experiments

| ID | Question | Main comparison signal |
| --- | --- | --- |
| E1 | Does unrelated proof data slow one proof's queries? | Flat timings mean `proof_id` indexing isolates proofs well. |
| E2 | What happens as the queried proof grows? | Shows per-proof scaling. |
| E3 | Which adapter operation dominates? | Breaks down reads, writes, frontier, and watermark cost. |
| E4 | What is the Bolt round-trip floor? | Gives the lower bound for every Neo4j operation. |
| E5 | What does Neo4j add to commit latency? | Direct `MemoryView` vs `Neo4jProjector` comparison. |
| E6 | How expensive is cold replay after a crash? | Reports `catch_up()` replay time per event. |
| E7 | How many Neo4j nodes are stored per committed event? | Tracks storage growth shape. |

## Result Columns

- `experiment`: experiment ID, such as `E5`.
- `operation`: operation being timed.
- `scale`: experiment input size. Its unit depends on the experiment.
- `atoms`: actual stored graph size observed during the measurement.
- `median`: median sample value.
- `p95`: 95th-percentile sample value.
- `iterations`: measured iterations after warm-up.
- `unit`: `us`, `ms`, or `nodes`.
- `note`: extra context for the row.

## Comparing With MORK

Use the same row shape for other backends so comparisons can be joined by
`experiment`, `operation`, and `scale`.

Recommended backend labels:

- `neo4j`: rows from `python -m neo4j_adapter.bench`.
- `mork`: rows from the MORK-native benchmark when it exists.
- `memory`: in-process `MemoryView` rows already produced by E5.

For a fair Neo4j vs MORK comparison:

- Use the same synthetic workload seed and proposal generator.
- Compare medians and p95s, not means.
- Keep warm-up policy explicit.
- Record actual stored atoms/nodes with every timing.
- Run both backends on the same machine when possible.
- Treat E5 as the first direct commit-latency comparison, then use E1-E4 and
  E6-E7 to explain why the gap exists.

See `results/comparison-template.md` for a copyable results note.
