"""Portable StaffDeck SOP transition runtime.

The package intentionally has no dependency on StaffDeck's ORM, AgentLoop,
or Harness process. A host owns persistence, authorization, model calls, and
tool execution; this package only computes validated SOP state transitions.
"""

from .original_runtime import SopRuntimeError, prepare, submit

__all__ = ["SopRuntimeError", "prepare", "submit"]
