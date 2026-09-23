# MORK

MORK contains the MeTTa proof-graph projection and query layer.

## Contents

- `event_journals/` - Source event journals used to build proof graphs.
- `projector/` - Python code that projects journal events into MORK atoms.
- `proofs/` - Generated or checked-in proof graph projections.
- `rules/` - Lazy MeTTa queries for indexes, frontiers, dependencies, taint,
  routes, and duplicate candidates. See [`rules/README.md`](rules/README.md).


