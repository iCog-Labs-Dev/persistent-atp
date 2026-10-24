"""Scripted-test receiver for interface tests only.

This module has no proof executor, commit gate, journal, or Lean replay and
must never be used as evidence of formal or production readiness.
"""
from .scripted import ScriptedReceiverPort

__all__ = ["ScriptedReceiverPort"]
