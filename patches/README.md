# Companion Mathematics-ai backend patch

`mathematics-ai-provider.patch` contains the backend changes required by the
`mathematics-ai-http` receiver profile. It is based on upstream Mathematics-ai
commit `cf97980b6ace56b2783c45020da2764a9dda9d70` and excludes `docs/` paths.

The local companion checkout is on `fix/persistent-atp-provider-integration` at
`808abf7325269b44cee4a631f95aec5d9fbf9729`.
To reproduce it in another clean backend checkout:

```bash
git switch -c fix/persistent-atp-provider-integration cf97980b6ace56b2783c45020da2764a9dda9d70
git apply --check /absolute/path/to/persistent-atp/patches/mathematics-ai-provider.patch
git apply /absolute/path/to/persistent-atp/patches/mathematics-ai-provider.patch
```

The patch preserves all Lean-produced goals and removes heuristic proof closure.
It adds complete contexts, enforced step budgets, atomic/idempotent run records,
measured environment identities, configurable shared imports, optional token
authentication, and exact replay evidence with a compiler/axiom check.

See [the integration guide](../src/omegaclaw/MATHEMATICS_AI.md) for configuration,
test environments and the separate replay/promotion command. The existing optional
Python backend dependency remains pinned to its earlier published revision; this
integration uses HTTP and requires the patched backend deployment. No unpublished
local commit is inserted into `uv.lock`.
