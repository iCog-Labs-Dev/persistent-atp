# Context compilation

The context compiler reads one fixed revision of proof memory and builds a
small, worker-facing context packet. Its purpose is to give a worker the
information needed for one task without sending the entire proof history.

It supports three packet types:

| Type | Consumer | Content |
| --- | --- | --- |
| `formal` | Formal ATP | The theorem, Lean declaration, root state, environment, usable premises, prior runs, and execution limits. |
| `research` | Research worker | The active research state, selected move, required claims, failures, feedback, and alternatives. |
| `obstruction` | Coordinator or obstruction worker | Failed formal states, attempts, diagnostics, suspected causes, and research questions. |

## How it fits into the system

```text
Scheduler/coordinator
  │ creates a request and chooses task-specific source IDs
  ▼
Commit-gate ReadView at the requested revision
  ▼
collect_sources()
  │ retrieves graph records and diagnostic artifacts
  ▼
compile_formal(), compile_research(), or compile_obstruction()
  ▼
Context packet + selection manifest + packet digest
  ▼
Worker
```

The context compiler only reads committed state. It does not write directly to
the commit gate. Any result produced by a worker must later be submitted as a
proposal through the commit gate.

Runtime scheduler-to-compiler and compiler-to-worker wiring is not implemented
in this module yet.

## Main concepts

### Compile request

`CompileRequest` identifies the task, proof, revision, worker, packet kind, and
budgets:

```python
from context_compiler import CompileRequest, PacketKind, TextBudget
from shared.vocab import WorkerClass

request = CompileRequest(
    task_id="task-42",
    proof_id="p1",
    base_revision=17,
    packet_kind=PacketKind.RESEARCH,
    worker_class=WorkerClass.LLM_RESEARCH,
    text_budget=TextBudget(4_000),
)
```

`base_revision` is important: context must be compiled from the same proof
revision against which the task was planned.

### Proof snapshot

The caller obtains an immutable snapshot from the commit gate's `ReadView` and
wraps it in `ProofSnapshot`:

```python
from context_compiler.snapshot import ProofSnapshot

revision, read_view = gate.view.snapshot(request.proof_id)
snapshot = ProofSnapshot(request.proof_id, revision, read_view)
```

If the snapshot proof or revision does not match the request,
`StaleSnapshotError` is raised. A stale task should be re-planned instead of
silently compiled against newer state.

Tests use `MemoryView`. A production configuration may use `MorkView`; the
compiler only depends on the shared `ReadView` interface.

### Retrieval plan

The request does not identify every record needed for the task. A coordinator
or retriever must provide the theorem and task-specific starting points:

```python
from context_compiler.sources import RetrievalPlan

plan = RetrievalPlan(
    kernel_id="p1/c-1",
    seed_ids=("p1/rs-3", "p1/rm-8"),
    max_depth=3,
    max_nodes=200,
)
```

Typical seed IDs are:

- research: active research state and selected move;
- formal: selected run/declaration and premise declarations;
- obstruction: failed run and relevant attempts.

`collect_sources()` performs a bounded walk over explicit graph relationships.
It does not rank the whole graph or decide which task should run.

## End-to-end use

First collect the sources:

```python
from context_compiler.sources import collect_sources

collection = collect_sources(
    request=request,
    snapshot=snapshot,
    artifacts=artifact_store,
    plan=plan,
)
```

The result provides:

- `kernel`: canonical theorem statement and assumptions;
- `bundle`: typed and versioned source references;
- `fetch(source_id)`: source content used by the compilers;
- `missing`: sources or artifacts that could not be retrieved;
- `truncations`: graph records not expanded because traversal limits were hit;
- `is_complete`: whether retrieval had no missing or truncated data.

A production caller should check the result before compiling:

```python
if collection.kernel is None or collection.bundle is None:
    raise RuntimeError("required context could not be collected")

if not collection.is_complete:
    raise RuntimeError(
        f"incomplete retrieval: {collection.missing}, "
        f"{collection.truncations}"
    )
```

The caller then constructs the task object for the selected compiler and passes
the collection as both bundle and artifact reader:

```python
compilation = compile_research(
    request,
    collection.bundle,
    collection,
    research_task,
)

worker_context = compilation.packet.content
manifest = compilation.manifest
packet_digest = compilation.packet_digest
```

Complete executable examples for all three compilers are in
`tests/test_sources_integration.py`.

## Compiler behavior

### Research compiler

`compile_research()` requires a `ResearchTask` containing the theorem kernel,
active research state, selected move, worker instructions, output format, and
tool policy.

The active state, selected move, required claims, instructions, output schema,
and tool policy are mandatory. Optional context is ordered as:

1. failures;
2. critic feedback;
3. reusable lemmas;
4. alternative moves.

Duplicates are removed and optional sections that exceed the text budget are
excluded with a recorded reason.

### Formal compiler

`compile_formal()` requires a `FormalTask`, formal execution limits, and a
`PremiseAccessibility` report from the ATP environment.

The theorem kernel, selected declaration, root state, pinned environment, and
resumed run are mandatory. Optional premises, prior runs, and accepted
transpositions can be limited with `FormalContextLimits`.

Verified premises remain separate from unverified suggestions. A Lean name
that is unavailable in the pinned environment is not sent to the ATP.

Use:

- `compilation.payload.to_schema_dict()` for the formal-search wire request;
- `compilation.payload.to_dict()` for the complete selected context and audit
  representation.

### Obstruction compiler

`compile_obstruction()` requires an `ObstructionTask` containing the theorem
kernel. It validates failed states, attempts, diagnostics, formal-run data, and
any recorded obstruction.

It selects relevant failed states, connects attempts and diagnostics to them,
separates observed facts from suspected causes, and derives research questions.
Under text-budget pressure, optional source listings and diagnostic/attempt
details are dropped before mandatory theorem and failure identity.

Use `compilation.payload.to_dict()` for the full obstruction or
`to_schema_dict()` for the obstruction schema representation.

## Outputs and auditability

Every compilation produces:

| Output | Purpose |
| --- | --- |
| `packet` | Content sent to the worker, with proof/task/revision identity. |
| `selection_decisions` | Whether each candidate was included and the reason. |
| `manifest` | Sources, versions, budgets, compiler versions, and digest. |
| `packet_digest` | SHA-256 digest of the canonical context packet. |

The manifest makes selection explainable: sources that are irrelevant,
duplicates, invalid for the current status, or over budget are recorded rather
than silently discarded.

The caller should persist the manifest and packet digest with the task or run,
then require the worker result to reference that identity.

## Current selection policy

Selection is deterministic and currently uses:

- explicit graph relationships and caller-selected seeds;
- lifecycle and verification status;
- relationship to the active state or selected move;
- fixed category ordering;
- duplicate removal;
- text and item-count limits.

The architecture paper's full weighted formula—dependency, ancestry, semantic
similarity, verification, failure, PLN, novelty, redundancy, and token cost—is
not implemented. MMR-style diversity percentages are also not implemented.
These should be added only when the team defines their score producers and
auditable representation.

## Source files

| File | Responsibility |
| --- | --- |
| `contracts.py` | Requests, budgets, packets, source references, and results. |
| `snapshot.py` | Proof/revision validation. |
| `sources.py` | Graph traversal and artifact retrieval. |
| `formal.py` | Formal ATP compilation. |
| `research.py` | Research context compilation. |
| `obstruction.py` | Formal-failure and obstruction compilation. |
| `budgeting.py` / `selection.py` | Shared budgeting and selection helpers. |
| `finalize.py` | Packet digest and provenance manifest. |

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
UV_CACHE_DIR=/tmp/persistent-atp-uv-cache \
uv run --extra dev pytest -q src/context_compiler/tests
```

The request/result fixtures are in `fixtures/context_compilation/`.

## Remaining integration work

- Connect the scheduler/coordinator to snapshot acquisition, retrieval,
  compiler selection, worker dispatch, and manifest persistence.
- Define the production response to incomplete retrieval.
- Decide which advanced scoring and diversity features from the paper are
  required and which are intentionally omitted.
- Run MORK- and PostgreSQL-dependent integration tests in an environment that
  provides those services.
