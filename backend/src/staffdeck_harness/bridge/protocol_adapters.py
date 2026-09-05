"""Compatibility import; model protocol ownership lives in app.llm.tool_protocols."""
from app.llm.tool_protocols import *  # noqa: F403
from app.llm import tool_protocols as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
