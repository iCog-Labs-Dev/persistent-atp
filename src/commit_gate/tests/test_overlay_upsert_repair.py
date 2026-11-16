"""Journal overlay matches projection repair after a partial node write."""

from commit_gate.ops import UpsertNode
from commit_gate.state import JournalOverlayView, MemoryView


def test_overlay_repairs_fields_of_partially_projected_creation():
    base = MemoryView()
    base.add_node("p1/fr-1", "FormalRun", {"status": "searching"})
    event = UpsertNode("FormalRun", "p1/fr-1", {"status": "searching", "actor": "searcher", "environment_hash": "sha256:" + "a" * 64})
    overlay = JournalOverlayView(base, 1, (event,))
    assert overlay.node("p1/fr-1").fields == event.fields
    assert base.node("p1/fr-1").fields == {"status": "searching"}
