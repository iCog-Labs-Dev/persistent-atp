"""Supervisor recovery test: kill -9, restart, rebuild, readiness.

The child fixture seeds a three-event journal, catches up, and writes a
readiness file. The test kills it with SIGKILL: the supervisor must restart
it, the journal must be preserved with no duplicate append, and the new
child must rebuild its projection to the journal head before reporting
ready again.
"""

import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mork.backend.ffi import MorkSpace, MorkUnavailable
from mork.supervisor import Supervisor

try:
    _SPACE = MorkSpace()
except MorkUnavailable as exc:
    _SPACE = None
    _WHY = str(exc)

needs_mork = unittest.skipIf(_SPACE is None, "MORK library unavailable")

CHILD = Path(__file__).with_name("supervised_child.py")


@needs_mork
class TestSupervisorRecovery(unittest.TestCase):
    def test_kill9_restarts_and_rebuilds_before_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "journal.sqlite")
            ready = str(Path(tmp) / "ready.json")
            sup = Supervisor([sys.executable, str(CHILD), db, ready, "3"], ready)
            thread = threading.Thread(target=sup.run, daemon=True)
            thread.start()
            try:
                sup.wait_ready(timeout=20)
                first = json.loads(Path(ready).read_text())
                self.assertEqual((first["head"], first["watermark"]), (3, 3))

                os.kill(first["pid"], signal.SIGKILL)

                sup.wait_ready(timeout=20)  # the restarted child's generation
                second = json.loads(Path(ready).read_text())
                self.assertNotEqual(second["pid"], first["pid"])
                self.assertEqual((second["head"], second["watermark"]), (3, 3))
            finally:
                sup.stop()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())


    def test_snapshot_restart_restores_all_proofs_before_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "journal.sqlite")
            ready = str(Path(tmp) / "ready.json")
            bundle = str(Path(tmp) / "bundle")
            sup = Supervisor([sys.executable, str(CHILD), db, ready, "3", bundle], ready)
            thread = threading.Thread(target=sup.run, daemon=True)
            thread.start()
            try:
                sup.wait_ready(timeout=20)
                first = json.loads(Path(ready).read_text())
                os.kill(first["pid"], signal.SIGKILL)
                sup.wait_ready(timeout=20)
                second = json.loads(Path(ready).read_text())
                self.assertNotEqual(first["pid"], second["pid"])
                self.assertEqual(second["proofs"], {"sup": [3, 3], "sup-other": [1, 1]})
                self.assertEqual(first["atoms_sha256"], second["atoms_sha256"])
            finally:
                sup.stop()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
