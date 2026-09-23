"""The mathproof protocol layer.

Phase 0 modules that sit above the commit gate:

* :mod:`mathproof.ids` -- the identifier namespace and allocator.
* :mod:`mathproof.schemas` -- JSON Schema loading for ``schemas/``.
* :mod:`mathproof.formal_atp` -- the FormalATPAdapter boundary + fake ATP.
* :mod:`mathproof.maths_ai_atp` -- the production ATP backend over maths-ai.
* :mod:`mathproof.soundness` -- structural validation of search results.
"""

from .formal_atp import (
    EMITTABLE_DISPOSITIONS,
    FakeFormalATP,
    FormalATPAdapter,
    build_result,
    build_state,
    build_tactic_edge,
    missing_request_fields,
    stub_replay,
)
from .ids import (
    DERIVED_ID_TYPES,
    ID_PREFIXES,
    LOCAL_ID_RE,
    IdAllocator,
    IdType,
    full_id,
    is_valid_local_id,
    local_id,
    parse_local_id,
)
from .maths_ai_atp import MathsAIFormalATP
from .soundness import (
    SoundnessReason,
    SoundnessViolation,
    validate_formal_search_result,
    violation_counts,
)

__all__ = [
    # ids
    "DERIVED_ID_TYPES",
    "ID_PREFIXES",
    "LOCAL_ID_RE",
    "IdAllocator",
    "IdType",
    "full_id",
    "is_valid_local_id",
    "local_id",
    "parse_local_id",
    # formal_atp
    "EMITTABLE_DISPOSITIONS",
    "FakeFormalATP",
    "FormalATPAdapter",
    "MathsAIFormalATP",
    "build_result",
    "build_state",
    "build_tactic_edge",
    "missing_request_fields",
    "stub_replay",
    # soundness
    "SoundnessReason",
    "SoundnessViolation",
    "validate_formal_search_result",
    "violation_counts",
]
