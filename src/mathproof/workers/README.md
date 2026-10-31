# Proof workers and follow-up actions

These bounded LLM workers receive context through `mathproof.worker_context.compile_context`. Research context is selected and budgeted by the shared `context_compiler` package; the adapter adds lease and action metadata. Critic and formalizer context is assembled for their specific gate operations. The scheduler leases a proof-scoped task, and `run_cycle` sends the worker's structured result through the commit gate. Workers never write to MORK themselves.

- Research requires a theorem target and a leased `ResearchMove`. It proposes up to five testable moves, closes or exhausts the worked move, and records the model output and packet digest. Its output creates no verified claim.
- Critic reviews a leased `Claim` and returns a verdict, earliest gap, repair, and cited evidence IDs. An unsupported "supported" opinion is recorded as pending. A "supported" opinion never promotes the claim; only the explicit gate-reviewed `critic-accepted` path can do that.

Register the workers with the existing dispatcher seam:

```python
from mathproof.dispatch import ScriptedDispatcher
from mathproof.workers import CriticWorker, ResearchWorker
from shared.llm_client import GeminiLLMClient

client = GeminiLLMClient()
dispatcher = ScriptedDispatcher({
    "llm-research": ResearchWorker(client).run,
    "critic": CriticWorker(client).run,
})
```

Then pass `dispatcher` to `mathproof.cycle.run_cycle` with a `GlobalScheduler` and `CommitGate`. The cycle selects the matching frontier category, compiles context at the lease revision, validates the result, and releases the lease on failure.

The compiler uses `ReadView.list_nodes`, so memory and MORK receive the same proof-scoped packet shape. The theorem kernel includes committed `Proof` targets and aligned declarations when present, or the selected move's target claim when no `Proof` node exists. Missing theorem data is not invented. Broader retrieval, tool calls, artifact storage for large outputs, and coordinator policy remain separate work.

## Formalization before ATP

`FormalizerWorker` is a separate scheduled worker. It takes a stable claim with no unverified dependencies, proposes a Lean proposition, and journals a **draft** `FormalDeclaration` linked by `DRAFTS_CLAIM`. It cannot mark a declaration aligned or change the claim's proof status. A blocked or malformed translation writes nothing and releases the lease.

The next `alignment-reviewer` cycle checks that proposition with `LeanStatementChecker` and passes the draft to the existing `AlignmentWorker`. Supply `LLMAlignmentReviewer` (or a qualified human reviewer) explicitly; the heuristic rule-based reviewer is refused in this cycle. A successful Lean statement check and reviewed `aligned` verdict align the declaration; it becomes eligible for ATP only after an Environment is pinned with `PINNED_ENVIRONMENT`. A mismatch or elaboration error is journaled on the Alignment record and leaves the declaration a draft for manual correction. The temporary `sorry` used solely to elaborate a proposition is never stored as a proof. Run generated Lean in an OS sandbox in production.

```python
from alignment import AlignmentWorker, LLMAlignmentReviewer
from mathproof.lean_statement import LeanStatementChecker
from mathproof.workers import FormalizerWorker

dispatcher = ScriptedDispatcher({"formalizer": FormalizerWorker(client).run})
# run_cycle(..., worker_class="formalizer", dispatcher=dispatcher)
# run_cycle(..., worker_class="alignment-reviewer", actor="alignment-1",
#   adapters={"alignment-reviewer": AlignmentWorker(
#       actor="alignment-1", reviewer=LLMAlignmentReviewer(client)),
#       "lean-statement-checker": LeanStatementChecker("/path/to/pinned/lean/project")})
```

This validates a *statement*, not a proof. The existing `formal-atp` cycle and replay gate remain responsible for proof search and kernel-backed verification.

## Executable follow-up

Research moves and pending critic reviews may request one typed action:

```json
{"kind": "lean_statement_check", "declaration_id": "p1/fd-1"}
```

The declaration must already be linked to the same claim by `DRAFTS_CLAIM`. The scheduler offers the action as `statement-check` work to the `experiment` worker class. Run `run_cycle(..., worker_class="experiment", adapters={"lean-statement-checker": checker})` to elaborate it. The journal records an `Experiment` with the result and diagnostics; `CHECKS_CLAIM` and `CHECKS_DECLARATION` make the result visible to later research and critic packets. Executing a research action closes that move. A pending critic is offered again only when its claim or relevant evidence changes. New evidence also invalidates a previous `supported` opinion so the critic can reassess it. A successful statement check does not promote a claim or certify a proof.

Other natural-language `first_test` and `repair` suggestions remain guidance until their action type has an executor. A caller still decides which worker class to run next.
