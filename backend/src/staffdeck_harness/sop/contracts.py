"""Host-owned SOP lifecycle SPI; replaced at worker startup, never mid-execution.

Implementations mutate an isolated SopState value. The Host validates and applies
the transition in the existing transaction. No ORM or database is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable, TYPE_CHECKING
from staffdeck_harness.contracts.sop import SopState as ChatSession, SopDefinition as Skill
if TYPE_CHECKING:
    from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
    from app.session.session_schema import RouterDecision, StepAgentResult
    from types import SimpleNamespace as HarnessTaskFrameRecord


class SopEventSink(Protocol):
    def record(
        self, tenant_id: str, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> Any: ...


@dataclass(frozen=True)
class SopDependencies:
    events: SopEventSink
    create_handoff: Callable[[str, ChatSession, Skill | None, StepAgentResult], Any]


@dataclass(frozen=True)
class SopAdvance:
    step_result: StepAgentResult
    continue_execution: bool


@runtime_checkable
class SopRuntimePort(Protocol):
    def list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]: ...
    def get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None: ...
    def drop_unavailable_skill_state(
        self, tenant_id: str, session: ChatSession, skills: list[Skill]
    ) -> bool: ...
    def activate_frame(
        self, session: ChatSession, row: HarnessTaskFrameRecord, skills: list[Skill]
    ) -> Skill | None: ...
    def restore_task_frame(self, session: ChatSession, frame: dict[str, Any]) -> ChatSession: ...
    def complete_current_skill(self, session: ChatSession) -> ChatSession: ...
    def after_execution(
        self,
        tenant_id: str,
        session: ChatSession,
        skill: Skill | None,
        requirement: TaskRequirement,
        result: TaskExecutionResult,
        router_decision: RouterDecision,
        *,
        remaining_actions: int,
    ) -> SopAdvance: ...
