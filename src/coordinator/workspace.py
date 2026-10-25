"""One proof as the coordinator sees it: its journal, its view, its workers.

The journal is the authority (Section 2.3, invariant 6). The view is a
projection of it that the coordinator catches up before every read, so a
coordinator restarted with an empty view rebuilds exactly what was committed.
Every caller opens its own store connection: a SQLite connection belongs to
the thread that made it, and proofs are worked on in parallel.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from commit_gate.state import MemoryView
from commit_gate.store import JournalStore
from mathproof.dispatch import Dispatcher
from mathproof.scheduler import SchedulerStatistics

__all__ = ["ProofWorkspace", "project"]


@dataclass
class ProofWorkspace:
    """What one proof is worked through; nothing here is proof state."""

    proof_id: str
    open_store: Callable[[], JournalStore]
    dispatcher: Dispatcher
    adapters: Mapping[str, Any] = field(default_factory=dict)
    view: MemoryView = field(default_factory=MemoryView)
    statistics: SchedulerStatistics = field(default_factory=SchedulerStatistics)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def project(view: MemoryView, store: JournalStore, proof_id: str) -> int:
    """Apply every journal event the view has not seen; return the head."""
    head = store.head(proof_id)[0]
    revision = view.snapshot(proof_id)[0]
    while revision < head:
        revision += 1
        view.apply_journal_event(store, proof_id, revision)
    return head
