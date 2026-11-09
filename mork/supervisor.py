"""Minimal crash-only supervisor for a MORK-backed host process.

The journal is authoritative; recovery is restart + replay, never resubmit.
The supervisor only decides when to (re)start the child. A child declares
readiness by atomically replacing `readiness_file` with
``{"ready": true, ...}``, which it must write only after every projection is
level with the journal — so a restarted child serves nothing until the
rebuild is complete.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence


class Supervisor:
    """Spawn `argv`, restart it whenever it dies, until `stop()`.

    One readiness generation is counted per spawn that reports ready;
    `wait_ready()` blocks for the next generation so callers can observe
    individual (re)starts.
    """

    def __init__(
        self,
        argv: Sequence[str],
        readiness_file: str | Path,
        *,
        poll_interval: float = 0.02,
        restart_delay: float = 0.05,
    ):
        self._argv = list(argv)
        self._readiness_file = Path(readiness_file)
        self._poll_interval = poll_interval
        self._restart_delay = restart_delay
        self._stop = threading.Event()
        self._state = threading.Condition()
        self._generation = 0
        self._child: subprocess.Popen | None = None
        self.spawn_count = 0

    @property
    def child_pid(self) -> int | None:
        with self._state:
            return self._child.pid if self._child is not None else None

    def wait_ready(self, timeout: float = 10.0) -> int:
        """Block until a child reports ready; returns the readiness generation."""
        with self._state:
            target = self._generation + 1
            deadline = time.monotonic() + timeout
            while self._generation < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "supervised child did not report readiness"
                    )
                self._state.wait(remaining)
            return self._generation

    def stop(self) -> None:
        self._stop.set()
        with self._state:
            child = self._child
        self._reap(child)
        self._readiness_file.unlink(missing_ok=True)

    def run(self) -> None:
        """The restart loop. Blocks until `stop()`; run it in its own thread."""
        while not self._stop.is_set():
            self._readiness_file.unlink(missing_ok=True)
            child = subprocess.Popen(self._argv)
            with self._state:
                self._child = child
                self.spawn_count += 1
                self._state.notify_all()

            ready_seen = False
            while not self._stop.is_set():
                if child.poll() is not None:
                    self._readiness_file.unlink(missing_ok=True)
                    break
                if not ready_seen and self._reports_ready():
                    with self._state:
                        self._generation += 1
                        self._state.notify_all()
                    ready_seen = True
                time.sleep(self._poll_interval)

            if self._stop.is_set():
                self._reap(child)
                break
            time.sleep(self._restart_delay)

        with self._state:
            self._child = None

    def _reports_ready(self) -> bool:
        try:
            return bool(
                json.loads(self._readiness_file.read_text()).get("ready")
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _reap(child: subprocess.Popen | None) -> None:
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
