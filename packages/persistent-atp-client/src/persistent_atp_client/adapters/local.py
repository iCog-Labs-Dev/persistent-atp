"""Trusted in-process receiver adapter; caller must already be authenticated."""
from __future__ import annotations


class LocalReceiverAdapter:
    def __init__(self, receiver):
        self.receiver = receiver

    def __getattr__(self, name):
        return getattr(self.receiver, name)

    async def aclose(self):
        # The receiver belongs to its service host, not to a caller's client.
        return None
