"""Explicit StaffDeck domain ports consumed by the engine-neutral turn coordinator.

The compatibility adapter is the only place that knows AgentLoop's historical private
method names. Alternative coordinators/tests may supply these collaborators directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class TurnServices:
    db: Any
    events: Any
    runtime: Any
    stream_sink: Any
    append_message: Callable[..., Any]
    apply_step_result: Callable[..., Any]
    conversation_context: Callable[..., Any]
    create_human_handoff_request: Callable[..., Any]
    default_next_step: Callable[..., Any]
    drop_unavailable_skill_state: Callable[..., Any]
    finalize_execution_after_reply: Callable[..., Any]
    finalize_turn: Callable[..., Any]
    get_active_skill: Callable[..., Any]
    get_agent_loop_max_actions: Callable[..., Any]
    get_persona_prompt: Callable[..., Any]
    get_request_model: Callable[..., Any]
    list_published_skills: Callable[..., Any]
    mark_session_running: Callable[..., Any]
    user_message_metadata: Callable[..., Any]
    get_or_create_session: Callable[..., Any]
    stream_finished: Callable[[bool], None]

    @classmethod
    def from_agent_loop(cls, owner: Any) -> "TurnServices":
        def missing(*args, **kwargs):
            raise NotImplementedError("required turn service was not supplied")

        names = set(cls.__dataclass_fields__) - {
            "db",
            "events",
            "runtime",
            "stream_sink",
            "stream_finished",
        }
        return cls(
            db=owner.db,
            events=owner.events,
            runtime=getattr(owner, "runtime", None),
            stream_sink=getattr(owner, "stream_sink", None),
            stream_finished=lambda success: setattr(owner, "stream_delivery_succeeded", success),
            **{name: getattr(owner, "_" + name, missing) for name in names},
        )
