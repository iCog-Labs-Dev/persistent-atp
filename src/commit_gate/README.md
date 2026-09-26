# Commit gate 

This directory contains the Python reference implementation of the **Commit Gate** for the Omega Math Dual-Loop Proof Architecture.

The system is designed to allow concurrent workers (ATPs, language models, human users) to build and verify formal proofs asynchronously. To prevent data corruption, race conditions, or logically invalid proofs from entering the database, all mutations must pass through the `commit_gate`.

## General Architecture

The architecture enforces a strict separation between proof search and proof commitment:

1. **Workers** explore proof spaces and construct `Proposal` objects. A proposal contains a sequence of immutable operations (`Op`) that represent the worker's intended changes to the graph.
2. **The Commit Gate** receives these proposals. It acts as an authoritative, synchronous bottleneck. It validates the operations against the current state of the database to ensure no invariants are violated (e.g., status transition rules, correct endpoint types for edges, compare-and-set concurrency control).
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
- **`validate.py`**: The core rules engine. Contains functions to verify subgoals, confirm executor results, check valid edge endpoints, enforce status transitions, and enforce `compare-and-set` field updates. `check_concurrency_tokens` runs first and unconditionally: a proposal naming no `base_revision`, or changing a status without holding the lease, is rejected rather than committed unchecked.

### State and Orchestration
- **`state.py`**: Defines the `ReadView` protocol required to read from the database, along with an in-memory implementation (`MemoryView`) used for testing.
- **`apply.py`**: An engine that idempotently projects a sequence of operations onto a `MemoryView`. Used for local testing and replay.
- **`store.py`**: An append-only SQLite journal. Every mutation runs in one `BEGIN IMMEDIATE` transaction, so the head read, the base-revision check, the lease-fencing check, and the insert cannot interleave with another writer. A writer that cannot take the lock within `busy_timeout_ms` gets a `journal-busy` rejection rather than a raw SQLite error. `verify_chain(proof_id)` recomputes every hash in a proof's history and raises `HashChainError` on the first row that does not chain; `append` runs the same check on the head alone, so a corrupt hash never gets a valid link built on top of it. Only the gate may call its mutating methods.
- **`gate.py`**: The `CommitGate` orchestrator, and the system's **sole writer**. It receives proposals, runs all validators via `validate.py`, and if accepted, chains and appends the event to the journal itself. A lost race (stale base revision, superseded fencing token) comes back as a `Rejection`, not an exception.
