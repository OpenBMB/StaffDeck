"""Narrow model-phase transport interface, free of ORM and AgentLoop internals."""

from __future__ import annotations
from typing import Any, Callable, Protocol


class PhaseRunner(Protocol):
    def prompt(
        self,
        *,
        phase: str,
        model_config: Any,
        system_text: str,
        user_text: str,
        engine_session: str,
        on_text: Callable[[str], None] | None = None,
    ) -> str: ...


class EnginePhaseError(RuntimeError):
    """The engine reported a turn-level error during a tool-less phase (plan / reply)."""

    def __init__(self, phase: str, detail: str):
        super().__init__(f"{phase} 阶段引擎报错：{detail}")
        self.phase = phase
        self.detail = detail
