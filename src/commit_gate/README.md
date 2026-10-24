# Commit gate 

This directory contains the Python reference implementation of the **Commit Gate** for the Omega Math Dual-Loop Proof Architecture.

The system is designed to allow concurrent workers (ATPs, language models, human users) to build and verify formal proofs asynchronously. To prevent data corruption, race conditions, or logically invalid proofs from entering the database, all mutations must pass through the `commit_gate`.

## General Architecture

The architecture enforces a strict separation between proof search and proof commitment:

1. **Workers** explore proof spaces and construct `Proposal` objects. A proposal contains a sequence of immutable operations (`Op`) that represent the worker's intended changes to the graph.
2. **The Commit Gate** receives these proposals. It acts as an authoritative, synchronous bottleneck. It validates the operations against a revisioned read snapshot plus any journal events the projection has not applied, then compares that revision again during append.
3. **The Journal (SQL)**: If a proposal is accepted, the gate canonically serializes it, cryptographically chains it to the previous event (forming an immutable history), and appends it to a SQL journal.
4. **The Graph Projection (Neo4j)**: Finally, a projector consumes the journal sequentially and applies the operations to the Neo4j graph database, where workers can query the updated state.

### Where the Database Integrates

The system relies on three stores, handled behind clear boundaries:

- **SQL Journal Store**: Handles append-only logging of events and optimistic concurrency control (fencing tokens, base revisions). This is implemented in `commit_gate/store.py` (currently backed by `sqlite3` for local testing, but designed to be replaced with Postgres or similar).
- **Neo4j Graph Database**: Acts as a read-optimized projection of the journal. The `commit_gate/state.py` module defines a `ReadView` protocol. To integrate Neo4j, implement this protocol to query the live Neo4j database, allowing the gate's validators to read current state without being coupled to the Cypher syntax.
- **Artifact Store**: Content-addressed, byte-level storage for large objects (Lean traces, prompts, model outputs, source files) that the graph and journal reference only by `sha256:`-prefixed hash, never by value. `validate.py`'s `check_references` already treats such hashes as unscoped, content-addressed identities (see `UNSCOPED_LABELS`). See `src/artifacts/README.md` for the implementation.

## File Summary

The `commit_gate` module contains the following files:

### Foundation and Vocabulary
- **`vocab.py`**: Defines closed vocabularies (Enums) for all status strings, worker classes, and obstruction types the system may write.
- **`ops.py`**: Defines the four foundational graph mutations (`UpsertNode`, `SetField`, `AddEdge`, `RemoveEdge`) that make up a proposal.
- **`canon.py`**: Provides deterministic JSON serialization and SHA-256 cryptographic chaining for the event journal.
- **`transitions.py`**: Codifies the legal state machine rules (which status values can transition to which) and defines which fields are strictly immutable after creation.

### Validation Logic
- **`proposal.py`**: Data structure representing a worker's requested changes.
- **`reasons.py`**: Closed vocabulary of rejection reason codes returned when a proposal is invalid.
- **`validate.py`**: The core rules engine. Contains functions to verify subgoals, confirm executor results, check valid edge endpoints, enforce status transitions, and enforce `compare-and-set` field updates. Every mutation needs a base revision and a fenced lease. A Lean-verified claim needs a replay-accepted certificate with a valid artifact hash, an independent verified replay, a reviewed alignment to the same declaration, and one pinned environment shared by its producing run, certificate, and replay.

### State and Orchestration
- **`state.py`**: Defines the revisioned `ReadView` protocol (point reads plus proof-scoped enumeration: `list_nodes(proof_id, label=None)` and `list_edges(proof_id, rel_type=None)`, ordered by id and never crossing proofs), an in-memory implementation (`MemoryView`) used for testing, and a read-only overlay for unapplied journal events. An in-memory projector uses `MemoryView.apply_journal_event` to read the exact event from `JournalStore` and publish it with its revision atomically.
- **`apply.py`**: An engine that idempotently projects a sequence of operations onto a `MemoryView`. Used for local testing and replay.
- **`store.py`**: An append-only SQLite journal. Every mutation runs in one `BEGIN IMMEDIATE` transaction, so the head read, the base-revision check, the lease-fencing and expiry checks, and the insert cannot interleave with another writer. Leases expire after 300 seconds by default; acquisitions are recorded in `lease_audit` outside the hash chain. A writer that cannot take the lock within `busy_timeout_ms` gets a `journal-busy` rejection rather than a raw SQLite error. `verify_chain(proof_id)` recomputes every hash in a proof's history and raises `HashChainError` on the first row that does not chain; `append` runs the same check on the head alone, so a corrupt hash never gets a valid link built on top of it. Only the gate may call its mutating methods.
- **`gate.py`**: The `CommitGate` orchestrator, and the system's **sole writer**. It receives proposals, runs all validators via `validate.py`, and if accepted, chains and appends the event to the journal itself. A lost race (stale base revision, superseded fencing token) comes back as a `Rejection`, not an exception.

## Dispatch leases

The trusted scheduler calls `issue_lease(proof_id, lease_id, actor=..., worker_class=...,
selected_move_id=..., ttl_seconds=600)` to assign a move. It returns a `LeaseRow`
with the fencing token and current base revision. The worker's `Proposal` must
carry that lease ID, token, actor, worker class, and `selected_move_id`. A token
cannot be used for another worker or another declared move. Operation authority
continues to be checked by the existing gate validators.

Distinct moves may hold leases concurrently; reissuing the same move supersedes
its old lease. `acquire_lease` retains its existing API for an exclusive proof
lease with a 300 second default TTL. It supersedes active dispatch leases and
blocks new dispatches until it is released or expires. Both APIs require a
finite TTL between 0 and 86400 seconds, exclusive of 0.

After work finishes, the scheduler calls
`release_lease(proof_id, lease_id, fencing_token=...)`. Releasing an old generation
cannot cancel a renewal. A successful commit does not itself release a lease.
A still-live lease also does not bypass revision checks: after another worker
commits, the proposal must be revalidated against the new revision.

`active_leases` and commit checks detect expiry when called; expiry does not use
a background timer. `read_lease_events` exposes issuance, supersession, expiry,
and release from `lease_audit`. These operations never advance proof revisions
or the hash chain. Existing lease rows and audit history are migrated atomically,
including the highest fencing token; legacy leases without a bound holder or
expiry remain unusable.

### Global scheduler

`GlobalScheduler.lease_next(proof_id, worker_class, ttl_seconds=600, actor=...)`
uses the existing weighted scoring policy and worker preferences. Worker
matching remains advisory, not a capability guarantee or a new eligibility
restriction. The caller supplies the actual worker identity and checks that
the selected task can be executed; results still go through `CommitGate`.

Selection reads a snapshot plus unapplied journal events. `claim_next` reserves
the highest-ranked free target atomically, without replacing a live assignment;
a declaration and its related runs share that reservation. Lease IDs survive
restarts. A concurrent journal change raises `STALE_BASE_REVISION`; callers
may retry selection. `None` means no eligible work is free, not proof completion.
Release with `scheduler.release(proof_id, lease_id, fencing_token=...)`.

Node enumeration still requires a dictionary-backed snapshot. Backend-neutral
MORK enumeration and worker execution remain outside this scheduler repair.
