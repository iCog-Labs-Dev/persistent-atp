# Formal ATP backends

The coordinator uses the five methods on `FormalATPAdapter`. Its canonical
request and result formats remain the JSON schemas under `schemas/`; provider
APIs are translated at the adapter boundary.

There are two HTTP integration styles:

- `HTTPFormalATP` calls any provider that natively implements the canonical
  five-route persistent-atp JSON contract.
- A provider adapter translates a different API while still implementing the
  same Python boundary. `MathsAIHTTPFormalATP` is the first such adapter.

The coordinator depends only on `FormalATPAdapter`. Adding another prover
therefore means implementing or configuring an adapter; it does not require
changes to scheduling, persistence, CommitGate, or the canonical schemas.

## Canonical HTTP provider

`HTTPFormalATP("https://provider.example/api")` expects:

| Route | Canonical response |
| --- | --- |
| `POST /formal-search/start` | `formal-search-result` |
| `POST /formal-search/{run_id}/resume` | `formal-search-result` |
| `GET /formal-search/{run_id}` | `formal-search-result` |
| `POST /formal-search/{run_id}/cancel` | Cancelled run acknowledgement |
| `POST /formal-replay` | Environment-bound replay verdict |

The adapter validates response schemas, identities, graph soundness, and the
pinned environment before returning provider data to the coordinator.

## Adding another provider

A provider with a different API implements the same five methods and keeps its
wire translation inside its own module:

```python
from mathproof import FormalATPAdapter

class OtherFormalATP(FormalATPAdapter):
    def formal_search_start(self, request): ...
    def formal_search_resume(self, run_id, budget=None): ...
    def formal_search_status(self, run_id): ...
    def formal_search_cancel(self, run_id): ...
    def formal_replay(self, certificate, environment_hash): ...
```

The implementation must return canonical schema-shaped results and fail
closed on identity, environment, graph, or replay mismatches. Provider route
names, authentication, polling, and native status vocabularies do not escape
the adapter.

## Mathematics-ai-atp provider

`MathsAIHTTPFormalATP` translates Mathematics-ai-atp's asynchronous persistent
HTTP lifecycle into the canonical adapter contract:

| Mathematics-ai-atp route | Adapter operation |
| --- | --- |
| `POST /formal-search/start` | Start an asynchronous search |
| `GET /formal-search/{run_id}` | Read and translate the latest snapshot |
| `POST /formal-search/{run_id}/retry` | Restart an interrupted, failed, or cancelled run |
| `POST /formal-search/{run_id}/cancel` | Request cancellation |
| `POST /formal-replay` | Independently replay the stored proof trace in Lean |

Create it with:

```python
from mathproof import maths_ai_http_atp

atp = maths_ai_http_atp("http://localhost:8000")
```

The adapter sends the canonical `run_id` unchanged. It maps `goal_text` and
`hypotheses` into the provider's `problem` object and converts
`budget.wall_clock_ms` to the provider's timeout in seconds. Persistent-atp's
`max_steps` is not mapped to Mathematics-ai-atp's `max_nodes`: those bounds do
not have the same meaning.

The canonical environment hash and Lean source artifact are placed in the
provider request's `environment.variables`. Every status response must echo
that complete request before the adapter will accept or translate its result.

### Retry is not resume

The provider's retry operation restarts a failed, interrupted, or cancelled
search. It does not save and resume a search frontier.
`formal_search_resume` therefore:

- retries only a provider run in one of those three states;
- returns the current status for active or terminal runs;
- rejects any attempt to change the original budget.

### Replay binding

The provider returns a full proof trace when it finds a proof. The adapter
checks that trace against solved, kernel-only hypergraph edges, retains it for
replay, and exposes only its content hash as the canonical certificate. Replay
then sends the exact stored problem and proof trace back with the run ID. A
successful or rejected verdict is rebound to the request's pinned environment
hash before it is returned to persistent-atp.

A positive replay verdict must identify the run, certificate hash, and the
environment fingerprint computed by the provider's actual Lean runtime.
Mathematics-ai-atp versions that return only `verified: true` cannot establish
this binding; the adapter reports verification as unavailable. The provider
must not merely echo the requested environment hash.

If a start response is lost, the adapter looks up the run and checks its echoed
request before adopting it. If the provider confirms the run does not exist,
the caller may submit the same request again. If the lookup fails, admission
remains uncertain and the adapter will not submit a duplicate request. The
provider should make starts idempotent for identical run IDs and requests to
close the race between an absent lookup and a delayed original submission.

The current adapter keeps request-to-trace bindings in process. Recovering a
run in a newly started persistent-atp client requires restoring that local
binding or adding a provider endpoint that authenticates and returns the
canonical request metadata.

### Current provider limitation

The current Mathematics-ai-atp graph summary includes each goal expression but
not each node's local hypotheses. The adapter preserves root hypotheses from
the original request; child-state hashes are based on the expressions
available from the provider. The provider should eventually include complete
local contexts for exact state identity across all nodes.
