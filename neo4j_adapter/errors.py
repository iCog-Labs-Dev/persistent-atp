"""Adapter-level exceptions.

Callers of the projection should never have to import ``neo4j.exceptions``:
every driver error raised inside the adapter is translated into one of the
exceptions below, so the graph stays an implementation detail.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base class for every failure raised by the Neo4j projection."""


class GraphUnavailable(GraphError):
    """The database could not be reached, authenticated with, or kept a session.

    Retryable at the caller's level: the journal is the durability authority,
    so the projection can always be rebuilt by replay.
    """


class GraphConstraintViolation(GraphError):
    """A write violated a uniqueness/existence constraint (schema drift or a
    duplicate ``(proof_id, id)`` key)."""


class GraphQueryError(GraphError):
    """The database rejected or failed a statement (bad Cypher, bad parameter,
    or a server-side error that outlived the driver's own retries)."""
