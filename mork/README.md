# MORK

MORK contains the MeTTa proof-graph projection and query layer.

## Contents

- `event_journals/` - Source event journals used to build proof graphs.
- `projector/` - Python code that projects journal events into MORK atoms.
- `backend/` - Live `libmork_ffi.so` bridge and commit-gate graph view.
- `atoms.py` - Canonical atom builders shared by file and live projection.
- `bench/` - Opt-in live-backend benchmarks (not part of the default tests).
- `proofs/` - Generated or checked-in proof graph projections.
- `rules/` - Lazy MeTTa queries for indexes, frontiers, dependencies, taint,
  routes, and duplicate candidates. See [`rules/README.md`](rules/README.md).

Both projection paths emit the same committed graph vocabulary: `node`,
`field`, `edge`, `rev-edge`, `efield`, and `layer`. The live backend also keeps
a proof-scoped `projected` revision so journal replay can repair an interrupted
projection.

## Running with the real FFI

The Python wrapper expects the length-delimited `RustBuffer` ABI provided by
the sibling `patham9/mork_ffi` checkout. The library must be preloaded because
of its static thread-local storage:

```bash
scripts/with-mork.sh .venv/bin/pytest -q mork/tests \
  src/commit_gate/tests/test_gate_projection.py
```

`scripts/with-mork.sh` discovers a nearby Cargo build or accepts an explicit
`MORK_LIBRARY=/absolute/path/to/libmork_ffi.so`. Without the library, ordinary
CI runs the codec and projector coverage and reports real-FFI cases as skipped.

