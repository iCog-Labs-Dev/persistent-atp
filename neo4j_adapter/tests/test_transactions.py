"""Transaction boundaries and error translation for the Neo4j projection.

These run without a database: a fake driver records which statements land in
which transaction, which is exactly what issue #10 is about. Behavioural
coverage against a live Neo4j belongs to issue #11.

The repo's own ``neo4j/`` package shadows the driver distribution, so the
package is loaded by path under an alias (same trick as
``src/shared/tests/test_vocab_drift.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple

import pytest
from neo4j.exceptions import ClientError, ConstraintError, ServiceUnavailable

PKG_DIR = Path(__file__).resolve().parents[1]


def _load_graph_package(alias: str = "graphpkg"):
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(
        alias,
        PKG_DIR / "__init__.py",
        submodule_search_locations=[str(PKG_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


graphpkg = _load_graph_package()
errors = importlib.import_module("graphpkg.errors")
constants = importlib.import_module("graphpkg.constants")

Neo4jAdapter = graphpkg.Neo4jAdapter


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class Statement(NamedTuple):
    cypher: str
    params: Dict[str, Any]


class FakeResult:
    """Enough of a Result to satisfy the adapter's ``.single()`` / iteration."""

    def __init__(self, records: List[Dict[str, Any]]):
        self._records = records

    def single(self):
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class FakeTx:
    def __init__(self, log: List[Statement], results: Dict[str, List[Dict[str, Any]]]):
        self._log = log
        self._results = results

    def run(self, cypher: str, **params: Any) -> FakeResult:
        self._log.append(Statement(cypher, params))
        for fragment, records in self._results.items():
            if fragment in cypher:
                return FakeResult(records)
        # Counts default to zero so fixpoint loops terminate immediately.
        return FakeResult([{"n": 0, "c": 0, "open_subgoals": 0}])


class FakeSession:
    def __init__(self, driver: "FakeDriver"):
        self._driver = driver

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def _execute(self, work, **kwargs):
        if self._driver.raises is not None:
            raise self._driver.raises
        statements: List[Statement] = []
        self._driver.transactions.append(statements)
        return work(FakeTx(statements, self._driver.results), **kwargs)

    execute_write = _execute
    execute_read = _execute

    def run(self, cypher: str, **params: Any) -> FakeResult:  # ensure_constraints only
        return FakeResult([])


class FakeDriver:
    def __init__(self, results: Dict[str, List[Dict[str, Any]]] | None = None, raises=None):
        # One inner list per transaction, holding that transaction's statements.
        self.transactions: List[List[Statement]] = []
        self.results = results or {}
        self.raises = raises
        self.closed = False

    def session(self, database=None) -> FakeSession:
        return FakeSession(self)

    def close(self) -> None:
        self.closed = True


def adapter_for(driver: FakeDriver) -> Neo4jAdapter:
    """An adapter wired to a fake driver, without connecting to anything."""
    adapter = object.__new__(Neo4jAdapter)
    adapter._driver = driver
    adapter._database = None
    return adapter


def only_transaction(driver: FakeDriver) -> List[Statement]:
    assert len(driver.transactions) == 1, (
        f"expected one transaction, got {len(driver.transactions)}"
    )
    return driver.transactions[0]


# ----------------------------------------------------------------------
# Transaction boundaries
# ----------------------------------------------------------------------


def test_add_state_with_parent_is_one_transaction():
    driver = FakeDriver()
    adapter_for(driver).add_state("p", "s1", "desc", parent_id="s0", event_id="e1")
    statements = only_transaction(driver)
    assert len(statements) == 3
    assert any("HAS_STATE" in s.cypher for s in statements)
    assert any("CHILD_OF" in s.cypher for s in statements)


def test_add_required_subgoal_is_one_transaction():
    driver = FakeDriver()
    adapter_for(driver).add_required_subgoal(
        "p", "m1", "sg1", "desc", parent_state_id="s0", event_id="e1"
    )
    statements = only_transaction(driver)
    assert len(statements) == 4
    assert any("REQUIRES" in s.cypher for s in statements)


def test_close_state_closes_moves_and_propagates_in_one_transaction():
    driver = FakeDriver()
    adapter_for(driver).close_state("s1", "p", "done", "e1")
    statements = only_transaction(driver)
    # status update, PROPOSES closure, then the AND/OR fixpoint pass
    assert "SET st.status = $status" in statements[0].cypher
    assert statements[0].params["status"] == constants.STATE_CLOSED
    assert "PROPOSES" in statements[1].cypher
    assert statements[1].params["closed"] == constants.MOVE_CLOSED
    assert any("REQUIRES" in s.cypher for s in statements[2:])
    assert any("PROPOSES" in s.cypher for s in statements[3:])


def test_propagate_taint_is_one_transaction():
    driver = FakeDriver(
        results={
            "collect(DISTINCT d.id)": [{"tainted": ["c2", "c3"]}],
            "collect(DISTINCT st.id)": [{"reopened": ["s1"]}],
        }
    )
    summary = adapter_for(driver).propagate_taint("p", "c1", "e1", "why")
    statements = only_transaction(driver)
    assert len(statements) == 3
    assert summary == {
        "refuted": "c1",
        "tainted": ["c2", "c3"],
        "reopened_states": ["s1"],
    }


def test_propagate_taint_skips_reopen_when_nothing_tainted():
    driver = FakeDriver(results={"collect(DISTINCT d.id)": [{"tainted": []}]})
    summary = adapter_for(driver).propagate_taint("p", "c1")
    assert len(only_transaction(driver)) == 2
    assert summary["reopened_states"] == []


def test_add_claim_dependency_checks_cycle_inside_the_write_transaction():
    driver = FakeDriver(results={"RETURN path LIMIT 1": [{"path": object()}]})
    with pytest.raises(ValueError, match="cycle"):
        adapter_for(driver).add_claim_dependency("c1", "c2", "p", "e1")
    statements = only_transaction(driver)
    # the cycle check ran, and the MERGE never did
    assert len(statements) == 1
    assert "DEPENDS_ON*1.." in statements[0].cypher


def test_add_claim_dependency_merges_after_a_clean_cycle_check():
    driver = FakeDriver(results={"RETURN path LIMIT 1": []})
    adapter_for(driver).add_claim_dependency("c1", "c2", "p", "e1")
    statements = only_transaction(driver)
    assert len(statements) == 2
    assert "MERGE (a)-[r:DEPENDS_ON" in statements[1].cypher


def test_wipe_and_rebuild_replays_each_event_in_its_own_transaction():
    driver = FakeDriver()
    events = [
        {"id": "r1", "type": "project_init", "payload": {"theorem_kernel": "t"}},
        {
            "id": "r2",
            "type": "state_added",
            "payload": {"state": {"id": "s1", "description": "d"}},
        },
    ]
    adapter_for(driver).wipe_and_rebuild("p", events)
    assert [len(tx) for tx in driver.transactions] == [1, 1, 2]
    assert "DETACH DELETE" in driver.transactions[0][0].cypher


# ----------------------------------------------------------------------
# Error translation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "driver_error, expected",
    [
        (ServiceUnavailable("down"), "GraphUnavailable"),
        (ConstraintError("duplicate", "Neo.ClientError.Schema.ConstraintValidationFailed"),
         "GraphConstraintViolation"),
        (ClientError("bad cypher"), "GraphQueryError"),
    ],
)
def test_driver_errors_are_translated(driver_error, expected):
    adapter = adapter_for(FakeDriver(raises=driver_error))
    with pytest.raises(getattr(errors, expected)) as excinfo:
        adapter.init_proof("p", "thm")
    assert excinfo.value.__cause__ is driver_error
    assert isinstance(excinfo.value, errors.GraphError)


def test_reads_translate_errors_too():
    adapter = adapter_for(FakeDriver(raises=ServiceUnavailable("down")))
    with pytest.raises(errors.GraphUnavailable):
        adapter.get_state("s1", "p")


def test_value_errors_are_not_swallowed():
    adapter = adapter_for(FakeDriver())
    with pytest.raises(ValueError, match="state status"):
        adapter.update_state_status("p", "s1", "not-a-status")
    with pytest.raises(ValueError, match="whitelist"):
        adapter.add_relation("p", "DROP_EVERYTHING", "a", "b")


def test_context_manager_closes_the_driver():
    driver = FakeDriver()
    adapter = adapter_for(driver)
    with adapter as entered:
        assert entered is adapter
    assert driver.closed
