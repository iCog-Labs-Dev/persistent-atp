"""Route a scheduler lease to its matching worker."""

from __future__ import annotations

from typing import Any, Mapping

from .critic import CriticWorker
from .research import ResearchWorker


class WorkerDispatcher:
    def __init__(self, research: ResearchWorker, critic: CriticWorker):
        self.workers = {
            research.worker_class: research,
            critic.worker_class: critic,
        }

    def run(self, lease: Any, context: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            worker = self.workers[lease.worker_class]
        except KeyError as exc:
            raise ValueError(f"no worker for {lease.worker_class!r}") from exc
        return worker.run(lease, context)
