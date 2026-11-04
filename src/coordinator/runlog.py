"""Thread-safe runtime events for coordinator CLI runs.

These events explain execution; they are not proof state. Accepted proof
changes remain authoritative only in the SQLite journal.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from uuid import uuid4

__all__ = ["EventSink", "RunLogger"]


EventSink = Callable[[str, Mapping[str, Any]], None]


class RunLogger:
    """Write concise progress to stderr and optional structured JSONL."""

    def __init__(
        self,
        *,
        verbose: bool = False,
        log_file: str | Path | None = None,
        run_id: str | None = None,
        stream: TextIO | None = None,
    ) -> None:
        self.run_id = run_id or uuid4().hex
        self.verbose = verbose
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()
        self._file = None
        if log_file is not None:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    def emit(self, event: str, fields: Mapping[str, Any]) -> None:
        record = {
            **dict(fields),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()
            if self.verbose:
                print(self._human_line(record), file=self.stream, flush=True)

    @staticmethod
    def _human_line(record: Mapping[str, Any]) -> str:
        order = (
            "proof_id", "backend", "round", "slot", "worker_class", "role",
            "move_id", "base_revision", "revision", "status", "outcome",
            "result", "verdict", "statement_checked", "success", "lean_name",
            "imports", "proposed_moves", "move_status", "evidence_count",
            "created", "status_changes", "progress", "frontier", "escalations",
            "status_reason", "duration_ms", "reason", "error_type", "message",
        )
        details = " ".join(
            f"{name}={record[name]}" for name in order if record.get(name) is not None
        )
        return f"[{record['event']}]" + (f" {details}" if details else "")

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
