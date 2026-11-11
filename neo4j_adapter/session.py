"""Transaction helpers shared by the adapter and its mixins.

Every public operation runs inside exactly one *managed* transaction
(``Session.execute_write`` / ``Session.execute_read``), so a multi-statement
operation is all-or-nothing and transient failures (leader switch, lost
connection, deadlock) are retried by the driver instead of surfacing as a
half-applied write.

Driver exceptions never escape: they are translated into the adapter-level
exceptions in :mod:`neo4j.errors`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from neo4j import ManagedTransaction
from neo4j.exceptions import (
    AuthError,
    ConfigurationError,
    ConstraintError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
)

from .errors import (
    GraphConstraintViolation,
    GraphQueryError,
    GraphUnavailable,
)

# A Cypher statement plus its parameters.
Statement = Tuple[str, Dict[str, Any]]


@contextmanager
def translate_errors(operation: str) -> Iterator[None]:
    """Re-raise driver exceptions as :class:`~neo4j.errors.GraphError` ones."""
    try:
        yield
    except (ServiceUnavailable, SessionExpired, AuthError, ConfigurationError) as exc:
        raise GraphUnavailable(f"{operation}: {exc}") from exc
    except ConstraintError as exc:
        raise GraphConstraintViolation(f"{operation}: {exc}") from exc
    except (Neo4jError, DriverError) as exc:
        raise GraphQueryError(f"{operation}: {exc}") from exc


def run_all(tx: ManagedTransaction, statements: Iterable[Statement]) -> None:
    """Run `statements` in order inside a single transaction."""
    for cypher, params in statements:
        tx.run(cypher, **params)


class TransactionMixin:
    """Managed-transaction plumbing.

    Expects the host class to provide ``self._driver`` (a ``neo4j.Driver``) and
    ``self._database`` (the target database name, or ``None`` for the default).
    """

    @contextmanager
    def _session(self, operation: str):
        with translate_errors(operation):
            with self._driver.session(database=self._database) as session:
                yield session

    # ------------------------------------------------------------------
    # Arbitrary unit of work — use when the operation needs to read a value
    # and then write based on it (that read must share the write's transaction).
    # ------------------------------------------------------------------

    def _write(
        self,
        operation: str,
        work: Callable[..., Any],
        **kwargs: Any,
    ) -> Any:
        with self._session(operation) as session:
            return session.execute_write(work, **kwargs)

    def _read(
        self,
        operation: str,
        work: Callable[..., Any],
        **kwargs: Any,
    ) -> Any:
        with self._session(operation) as session:
            return session.execute_read(work, **kwargs)

    # ------------------------------------------------------------------
    # Shorthands for the common shapes
    # ------------------------------------------------------------------

    def _write_all(self, operation: str, statements: Sequence[Statement]) -> None:
        """Run several statements atomically (one transaction, no result)."""
        self._write(operation, run_all, statements=statements)

    def _read_one(
        self,
        operation: str,
        cypher: str,
        params: Dict[str, Any],
        key: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the first record's `key` value as a dict, or None."""

        def work(tx: ManagedTransaction) -> Optional[Dict[str, Any]]:
            record = tx.run(cypher, **params).single()
            return dict(record[key]) if record else None

        return self._read(operation, work)

    def _read_many(
        self,
        operation: str,
        cypher: str,
        params: Dict[str, Any],
        key: str,
    ) -> List[Dict[str, Any]]:
        """Return every record's `key` value as a list of dicts."""

        def work(tx: ManagedTransaction) -> List[Dict[str, Any]]:
            return [dict(record[key]) for record in tx.run(cypher, **params)]

        return self._read(operation, work)

    def _read_value(
        self,
        operation: str,
        cypher: str,
        params: Dict[str, Any],
        key: str,
        default: Any = None,
    ) -> Any:
        """Return a scalar/collection value from the first record."""

        def work(tx: ManagedTransaction) -> Any:
            record = tx.run(cypher, **params).single()
            return record[key] if record else default

        return self._read(operation, work)
