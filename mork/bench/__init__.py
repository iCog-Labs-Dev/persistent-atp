"""Benchmarks for the live MORK backend.

Run as a module, through the wrapper that preloads the library:

    scripts/with-mork.sh python -m mork.bench --quick

Not part of the test suite: `pyproject.toml` keeps this package out of
`testpaths`, because a timing sweep that runs on every `pytest` invocation stops
being a benchmark and becomes a flaky test. `README.md` explains what each
experiment measures and records the numbers from a full sweep.
"""
