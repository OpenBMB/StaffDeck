"""Compatibility imports for the v2 runtime. Shared orchestration is engine-neutral."""
from app.core.turn_coordinator import HarnessV2Engine, TurnCoordinator  # noqa: F401
from app.core import turn_coordinator as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
