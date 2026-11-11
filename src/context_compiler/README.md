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

### Scheduler runtime integration

For research work, the coordinator-facing entry point is
`ResearchLeaseCompiler`. It owns the read view, journal, artifact source, and
runtime policy, so callers cannot accidentally compile a lease against the
wrong proof revision:

```python
from context_compiler import ResearchLeaseCompiler, ResearchRuntimeConfig

compiler = ResearchLeaseCompiler(
    view=commit_gate.view,
    journal=commit_gate.store,
    artifacts=artifact_store,
    config=ResearchRuntimeConfig(
        instructions="Advance the selected proof strategy.",
        output_schema="Return a structured research result.",
        tool_policy="Use only the supplied context and allowed tools.",
    ),
)

lease = scheduler.lease_next(
    proof_id,
    "llm-research",
    actor="research-worker-1",
)
if lease is not None:
    dispatch = compiler.compile(lease)
    worker_input = dispatch.worker_input
```

`compile()` performs the complete context handoff:

1. verifies that the journal head still equals the lease's `base_revision`;
2. combines the projected read view with any unapplied journal events;
3. resolves the selected move's active state, required claims, and theorem;
4. retrieves bounded graph and artifact context;
5. compiles the research packet and provenance manifest;
6. binds them to the lease ID and fencing token in `CompiledDispatch`.

Compilation fails closed when the lease is stale, the graph identities are
ambiguous, required context is missing or truncated, or the lease is not for an
`llm-research` worker. The coordinator should discard or re-plan stale work; it
must not silently compile against the latest revision.

`CompiledDispatch` contains the worker-facing input together with the context
packet, packet digest, selection manifest, lease identity, fencing token, and
base revision. The coordinator is responsible for sending `worker_input` to the
worker and persisting the digest/manifest with the run. Worker results should
reference that digest and still pass the commit gate's revision and fencing
checks before they can change proof state.

The runtime boundary is intentionally independent of LangGraph. A LangGraph or
other coordinator can invoke `ResearchLeaseCompiler.compile()` from its worker
dispatch node without changing context selection semantics.

Formal work uses the matching `FormalLeaseCompiler` boundary:

```python
from context_compiler import (
    FormalLeaseCompiler,
    FormalRuntimeConfig,
)
from context_compiler.formal import CheckpointPolicy, PremiseAccessibility
from context_compiler.contracts import FormalExecutionLimits

formal_compiler = FormalLeaseCompiler(
    view=commit_gate.view,
    journal=commit_gate.store,
    artifacts=artifact_store,
    config=FormalRuntimeConfig(
        execution_limits=FormalExecutionLimits(500, 60_000),
        accessibility=PremiseAccessibility(frozenset()),
        checkpoint_policy=CheckpointPolicy(every_steps=100),
        search_policy="best-first",
    ),
)

dispatch = formal_compiler.compile(formal_lease)
formal_search_request = dispatch.worker_input
```

For an existing `FormalRun`, this resolves its unique `SEARCHES`, `HAS_ROOT`,
and `RAN_UNDER` relationships, validates the declaration, theorem claim, root
state, and pinned environment, and returns the formal-search schema mapping as
`worker_input`. Optional premise declarations are explicit configuration seeds;
the compiler still filters them through `PremiseAccessibility` before use.

The scheduler can also lease an aligned `FormalDeclaration` before its run
exists. Such a declaration does not yet identify the run ID, root state, or
execution environment required for a reproducible formal request. The runtime
therefore raises `FormalRunPreparationRequired` instead of inventing those
records. The coordinator must first commit the prepared `FormalRun`, root
`FormalState`, environment pin, and their relationships, then schedule or
compile that run.

Formal failures use the `obstruction-task` scheduler category. A committed
`Obstruction` becomes eligible when it has one `RAISED_OBSTRUCTION` source run,
that run is `stagnated` or `counterexample`, it identifies formal states through
`AT_STATE`, has one theorem `Claim` through `RESOLVES`, and the run identifies
one environment through `RAN_UNDER`.

The default consumer is the coordinator, but it is an explicit runtime policy:

```python
from context_compiler import (
    ObstructionLeaseCompiler,
    ObstructionRuntimeConfig,
)

obstruction_compiler = ObstructionLeaseCompiler(
    view=commit_gate.view,
    journal=commit_gate.store,
    artifacts=artifact_store,
    config=ObstructionRuntimeConfig(),
)

lease = scheduler.lease_next(
    proof_id,
    "coordinator",
    actor="coordinator-1",
)
if lease is not None:
    dispatch = obstruction_compiler.compile(lease)
    obstruction_request = dispatch.worker_input
```

Compilation resolves the failed run, states, attempts, diagnostics, theorem,
and pinned environment from the exact lease revision. It returns the
obstruction schema mapping as `worker_input`, together with the complete text
packet, digest, manifest, lease ID, and fencing token.

An obstruction lease conflicts with its run, formal states, and sibling
obstructions, preventing concurrent analysis of the same failure. Permanent
completion is deliberately policy-owned: `GlobalScheduler` accepts an
`obstruction_is_handled(view, obstruction_id)` predicate. Until the coordinator
defines and persists its handled marker, the default predicate returns false,
so releasing a lease can make the obstruction eligible again. This avoids
inventing an unapproved obstruction lifecycle while keeping the final rule in
one replaceable boundary.

### Unified scheduler handoff

`ContextLeaseCompiler` is the single integration point for coordinators and
future LangGraph nodes. It routes from the selected committed graph label, so
worker-class reuse cannot accidentally choose the wrong compiler:

```python
from context_compiler import ContextLeaseCompiler

context_runtime = ContextLeaseCompiler(
    view=commit_gate.view,
    journal=commit_gate.store,
    artifacts=artifact_store,
    research=research_runtime_config,
    formal=formal_runtime_config,
    obstruction=obstruction_runtime_config,
)

dispatch = context_runtime.lease_and_compile(
    scheduler,
    proof_id,
    requested_worker_class,
    actor=worker_actor,
)
```

Routing is explicit:

| Selected graph label | Compiler |
| --- | --- |
| `ResearchMove` | research context compiler |
| `FormalRun` | formal context compiler |
| `FormalDeclaration` | formal preparation-required path |
| `Obstruction` | obstruction context compiler |

Other scheduler work, such as provisional-claim critic tasks, is rejected as
unsupported by this context runtime instead of being sent to the wrong
compiler.

`lease_and_compile()` returns `None` when the scheduler is idle. A successfully
compiled lease remains active because the worker result must carry its lease ID
and fencing token. If context compilation fails before worker handoff, the
method releases the lease immediately so malformed or incomplete work does not
block the frontier until TTL expiry. The original compilation error remains the
reported failure even if lease release also fails.



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
