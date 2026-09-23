"""The identifier namespace every committed proof object carries (E1).

Appendix A of the Unified Dual-Loop Architecture v3 names eleven core object
types. Each gets a reserved two-to-four letter prefix here, *before* any
producer exists, so schemas, fixtures and later phases mint IDs in final
shape instead of migrating placeholder identities later.

ID grammar
----------

A local ID is ``<prefix>-<serial>`` where serial is a positive decimal
integer with no leading zeros::

    c-1  fs-42  cert-7

A full ID prefixes the proof scope the commit gate requires
(``check_namespace`` in ``commit_gate.validate`` rejects identities that do
not start with ``{proof_id}/``)::

    p1/c-1

Artifacts are content-addressed (``sha256:...``) and take no allocator ID.
The types in :data:`DERIVED_ID_TYPES` have no Appendix A name but do appear
as gate labels; they get derived prefixes so every node the gate knows can
carry an allocator-issued ID.

Serials are allocated per ``(proof_id, type)``, monotonically from 1, with no
gaps introduced by the allocator itself. Allocation is deterministic and
local; uniqueness comes from the (proof scope, prefix, serial) triple.
"""

from __future__ import annotations

import re
from collections import defaultdict
from enum import StrEnum

__all__ = [
    "IdType",
    "ID_PREFIXES",
    "DERIVED_ID_TYPES",
    "LOCAL_ID_RE",
    "local_id",
    "full_id",
    "parse_local_id",
    "is_valid_local_id",
    "IdAllocator",
]


class IdType(StrEnum):
    """Reserved ID-type namespace (E1, Appendix A object types)."""

    RESEARCH_STATE = "rs"
    RESEARCH_MOVE = "rm"
    CLAIM = "c"
    FORMAL_DECLARATION = "fd"
    FORMAL_RUN = "fr"
    FORMAL_STATE = "fs"
    TACTIC_APPLICATION = "ta"
    ALIGNMENT = "al"
    OBSTRUCTION = "obs"
    CERTIFICATE = "cert"
    LEAN_REPLAY = "lr"

    # Derived types: gate labels outside Appendix A that still need stable,
    # allocator-issued identities inside a proof scope.
    ENVIRONMENT = "env"
    FORMAL_CHECKPOINT = "fc"
    SPECULATIVE_HYPOTHESIS = "sh"
    ATTEMPT = "at"


ID_PREFIXES = frozenset(
    {
        IdType.RESEARCH_STATE,
        IdType.RESEARCH_MOVE,
        IdType.CLAIM,
        IdType.FORMAL_DECLARATION,
        IdType.FORMAL_RUN,
        IdType.FORMAL_STATE,
        IdType.TACTIC_APPLICATION,
        IdType.ALIGNMENT,
        IdType.OBSTRUCTION,
        IdType.CERTIFICATE,
        IdType.LEAN_REPLAY,
    }
)
"""The eleven Appendix A prefixes reserved now, before producers exist."""

DERIVED_ID_TYPES = frozenset(IdType) - ID_PREFIXES
"""Gate-label coverage beyond Appendix A (environment, checkpoint, ...)."""

_LOCAL_ID_PATTERN = r"(?P<prefix>" + "|".join(
    sorted((t.value for t in IdType), key=len, reverse=True)
) + r")-(?P<serial>[1-9][0-9]*)"

LOCAL_ID_RE = re.compile(r"^" + _LOCAL_ID_PATTERN + r"$")
"""Matches ``<prefix>-<serial>``, e.g. ``fs-12``."""

_BY_PREFIX: dict[str, IdType] = {t.value: t for t in IdType}


def local_id(id_type: IdType | str, serial: int) -> str:
    """Render ``<prefix>-<serial>``, e.g. ``local_id(IdType.CLAIM, 3) == 'c-3'``."""
    prefix = _resolve(id_type)
    if serial < 1:
        raise ValueError(f"serial must be >= 1, got {serial}")
    return f"{prefix}-{serial}"


def full_id(proof_id: str, id_type: IdType | str, serial: int) -> str:
    """Proof-scoped ID the commit gate accepts, e.g. ``p1/fs-12``."""
    return f"{proof_id}/{local_id(id_type, serial)}"


def parse_local_id(raw: str) -> tuple[IdType, int]:
    """Split a local ID into its type and serial; raise ``ValueError`` otherwise.

    Accepts only canonical local IDs -- not proof-scoped ones and not
    content addresses.
    """
    match = LOCAL_ID_RE.match(raw)
    if match is None:
        raise ValueError(f"not a local id: {raw!r}; expected {LOCAL_ID_RE.pattern!r}")
    return _BY_PREFIX[match["prefix"]], int(match["serial"])


def is_valid_local_id(raw: str) -> bool:
    """True when `parse_local_id` would accept `raw`."""
    return LOCAL_ID_RE.match(raw) is not None


def _resolve(id_type: IdType | str) -> str:
    if isinstance(id_type, IdType):
        return id_type.value
    try:
        return _BY_PREFIX[id_type].value
    except KeyError:
        raise ValueError(
            f"unknown id type {id_type!r}; reserved prefixes: "
            f"{sorted(_BY_PREFIX)}"
        ) from None


class IdAllocator:
    """Mints sequential, proof-scoped IDs per type.

    One allocator per proof. Serial counters are independent across types,
    so ``c-1`` and ``fs-1`` may both exist under the same proof.
    """

    def __init__(self, proof_id: str, start: dict[IdType, int] | None = None):
        self.proof_id = proof_id
        self._counters: defaultdict[IdType, int] = defaultdict(int)
        for id_type, last_used in (start or {}).items():
            self._counters[_resolve_typed(id_type)] = last_used

    def next(self, id_type: IdType | str) -> str:
        """Allocate the next ID of `id_type`; never repeats within this proof."""
        prefix = _resolve(id_type)
        typed = _BY_PREFIX[prefix]
        self._counters[typed] += 1
        return full_id(self.proof_id, typed, self._counters[typed])

    def used(self, id_type: IdType | str) -> int:
        """How many IDs of this type the allocator has issued."""
        return self._counters[_resolve_typed(id_type)]

    def state(self) -> dict[IdType, int]:
        """The counters, e.g. for persisting an allocation checkpoint."""
        return dict(self._counters)


def _resolve_typed(id_type: IdType | str) -> IdType:
    return id_type if isinstance(id_type, IdType) else _BY_PREFIX[id_type]
