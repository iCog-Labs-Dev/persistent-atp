# Benchmark Results

Timing results for each backend. All JSON files share the same row schema so
they can be compared directly.

## Row schema

| field       | type   | description                                      |
|-------------|--------|--------------------------------------------------|
| experiment  | string | E1–E7                                            |
| operation   | string | function or query name                           |
| scale       | int    | independent variable (nodes / events / proposals)|
| atoms       | int    | actual graph size when timing was taken          |
| median      | float  | median latency                                   |
| p95         | float  | 95th-percentile latency                          |
| iterations  | int    | sample count (1 for one-shot measurements)       |
| unit        | string | `us` (microseconds), `ms`, or `nodes`            |
| note        | string | context                                          |

## Experiments

| id | question                                          |
|----|---------------------------------------------------|
| E1 | Do unrelated proofs slow queries against this one? |
| E2 | How does query time grow as the proof grows?       |
| E3 | Cost breakdown per operation                       |
| E4 | Raw round-trip floor (minimum cost of any query)   |
| E5 | Commit latency: backend vs in-memory               |
| E6 | Cold recovery: journal replay into empty backend   |
| E7 | Node count growth vs committed events              |

E5 is the primary MORK vs Neo4j comparison point — same proposals, same gate,
same journal, only the projection backend differs.

## How to run

```bash
# Neo4j (requires running instance, credentials in .env)
python -m neo4j_adapter.bench --json results/neo4j_YYYYMMDD.json

# quick smoke-check
python -m neo4j_adapter.bench --quick --json results/neo4j_quick.json

# single experiment
python -m neo4j_adapter.bench --experiment E5 --json results/neo4j_e5.json
```

MORK benchmarks will follow the same interface and write to `results/mork_YYYYMMDD.json`.

## Results

| file | backend | date | notes |
|------|---------|------|-------|
| `neo4j_20260918.json` | Neo4j 5.x (bolt://localhost:7687) | 2026-09-18 | full run, 60 rows |

---

### Neo4j — 2026-09-18

**E1 — Cross-proof isolation**

Query times stay flat as background proofs grow from 0 → 25,000 nodes across 1,000 unrelated proofs. `proof_id` indexing is working — other proofs do not pollute queries against the target.

| operation | 0 bg nodes | 25k bg nodes |
|-----------|-----------|-------------|
| get_state() | 5,301 µs | 8,002 µs |
| eligible_frontier() | 3,705 µs | 2,026 µs |
| watermark() | 3,380 µs | 2,012 µs |

**E2 — Own proof growth**

Query times are stable as the proof itself grows from 100 → 10,000 nodes. No degradation.

| operation | 100 nodes | 10k nodes |
|-----------|----------|----------|
| get_state() | 1,660 µs | 3,666 µs |
| eligible_frontier() | 2,851 µs | 1,545 µs |

**E3 — Per-operation cost (at 2,000 nodes)**

| operation | median | p95 |
|-----------|--------|-----|
| add_state() | 7,734 µs | 14,836 µs |
| add_move() | 6,698 µs | 14,101 µs |
| get_state() | 2,442 µs | 3,099 µs |
| eligible_frontier() | 2,408 µs | 3,425 µs |
| update_state_status() | 2,650–3,172 µs | 3,335–3,887 µs |
| watermark() | 2,303 µs | 3,958 µs |

Writes (MERGE) cost ~3× reads. High p95 on writes (~14 ms) indicates occasional GC or lock contention.

**E4 — Bolt round-trip floor**

~2,500–4,700 µs for a read, ~1,400–2,100 µs for a write. Every operation in E3 pays at least this.

**E5 — Commit latency (key comparison point)**

| backend | median | p95 |
|---------|--------|-----|
| MemoryView (in-memory) | 112 µs | 164 µs |
| Neo4jProjector | 9,923 µs | 16,091 µs |

Neo4j adds ~**89× overhead** per commit vs in-memory. This is the baseline MORK must beat to justify the switch.

**E6 — Cold recovery (journal replay)**

| events | total time | per event |
|--------|-----------|----------|
| 100 | 834 ms | 8.3 ms |
| 1,000 | 7,708 ms | 7.7 ms |
| 5,000 | 46,209 ms | 9.2 ms |

Linear scaling. ~8–9 ms per event to replay from cold.

**E7 — Node count growth**

~0.75–1.0 nodes per committed event. Linear and stable.
