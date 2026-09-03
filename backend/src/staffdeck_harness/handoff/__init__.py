"""Handoff Core (T, not pluggable) and its three pluggable slots."""

from staffdeck_harness.handoff.core import (
    TRANSITIONS,
    AssignmentStrategy,
    ChannelCommandReplyResolver,
    ChannelNotifier,
    DefaultAssignment,
    HandoffCore,
    HandoffTransitionError,
    Notifier,
    ReplyResolver,
    WebInboxNotifier,
    WebReplyResolver,
    build_handoff_core,
)

__all__ = [
    "TRANSITIONS", "AssignmentStrategy", "ChannelCommandReplyResolver", "ChannelNotifier", "DefaultAssignment",
    "HandoffCore", "HandoffTransitionError", "Notifier", "ReplyResolver", "WebInboxNotifier", "WebReplyResolver",
    "build_handoff_core",
]
