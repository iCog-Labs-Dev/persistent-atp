"""The live MORK space, as the commit gate sees it.

`ffi` wraps the C boundary; `view` implements the gate's `GraphView` on top of
it, so a gate reads and writes MORK atoms directly.
"""

from .ffi import MalformedAtom, MorkError, MorkSpace, MorkUnavailable
from .view import MorkView

__all__ = [
    "MorkSpace",
    "MorkView",
    "MorkError",
    "MorkUnavailable",
    "MalformedAtom",
]
