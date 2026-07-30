from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.tools.tool_schema import ToolCall, ToolResult

StatusCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True)
class LegacyToolActionCallbacks:
    max_actions: Callable[[str], int]
    decision_payload: Callable[..., dict[str, object]]
    new_id: Callable[[str], str]
    is_general_skill_tool: Callable[[str], bool]
    call_signature: Callable[[ToolCall], str]
    emit_tool_status: Callable[..., None]
    execute_tool_call: Callable[..., ToolResult]
    record_result: Callable[[ChatSession, ToolCall, ToolResult], None]
    activity_payload: Callable[..., dict[str, object]]
    emit_thinking_status: Callable[..., None]
    run_step: Callable[..., StepAgentResult]
    continuation_context: Callable[..., dict[str, object]]
    apply_result: Callable[..., None]
    advance_after_tool: Callable[..., None]


class LegacyToolAction:
    def __init__(self, db: Session, events: Any) -> None:
        self.db = db
        self.events = events

    def execute_cycle(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig | None,
        step_result: StepAgentResult,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        status_callback: StatusCallback | None,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        callbacks: LegacyToolActionCallbacks,
    ) -> tuple[StepAgentResult, ToolResult | None]:
        tool_result: ToolResult | None = None
        current_knowledge = list(step_result.knowledge_results or [])
        seen_calls: set[str] = set()
        max_actions = callbacks.max_actions(request.tenant_id)
        for iteration in range(max_actions):
            tool_call = step_result.tool_call
            if not tool_call:
                break
            tool_call_id = callbacks.new_id("toolcall")
            signature = callbacks.call_signature(tool_call)
            if signature in seen_calls:
                if tool_result and tool_result.success and step_result.reply:
                    step_result = step_result.model_copy(
                        update={"tool_call": None, "is_step_completed": True}
                    )
                    payload = callbacks.decision_payload(
                        iteration + 1, "respond_after_duplicate"
                    )
                    self.events.record(
                        request.tenant_id, chat_session.id, "agent_loop_completed", payload
                    )
                    if stream_events is not None:
                        stream_events.append(("agent_loop_completed", payload))
                    break
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "agent_loop_stopped",
                    {"reason": "duplicate_tool_call", "tool_call": tool_call.model_dump()},
                )
                break
            seen_calls.add(signature)
            callbacks.emit_tool_status(
                tool_call, tool_call_id, stream_events, status_callback
            )
            tool_result = callbacks.execute_tool_call(
                request,
                chat_session,
                tool_call,
                tool_call_id,
                stream_events=stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
            is_read_only_general_skill = (
                callbacks.is_general_skill_tool(tool_call.name)
                and str(tool_call.arguments.get("operation") or "execute").strip().lower()
                == "read"
            )
            if not is_read_only_general_skill:
                callbacks.record_result(chat_session, tool_call, tool_result)
            if stream_events is not None:
                stream_events.append(
                    (
                        "tool_result",
                        callbacks.activity_payload(
                            request.tenant_id,
                            tool_call.name,
                            tool_result,
                            tool_call,
                            tool_call_id,
                        ),
                    )
                )
            self.db.commit()
            self.db.refresh(chat_session)
            if is_read_only_general_skill:
                data = tool_result.data if isinstance(tool_result.data, dict) else {}
                reply = str(data.get("reply") or "").strip()
                if not reply and tool_result.error is not None:
                    reply = tool_result.error.message
                step_result = StepAgentResult(
                    reply=reply or "已完成 Skill 内容读取。",
                    knowledge_results=current_knowledge,
                    is_step_completed=False,
                )
                payload = callbacks.decision_payload(
                    iteration + 1, "respond_after_read"
                )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "agent_loop_completed",
                    payload,
                )
                if stream_events is not None:
                    stream_events.append(("agent_loop_completed", payload))
                break
            if not tool_result.success:
                if (
                    model_config
                    and callbacks.is_general_skill_tool(tool_call.name)
                    and active_skill is not None
                ):
                    callbacks.emit_thinking_status(
                        chat_session, iteration + 1, stream_events, status_callback
                    )
                    continuation_result = callbacks.run_step(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        repair_reason="tool_continuation",
                        repair_context=callbacks.continuation_context(
                            request.tenant_id,
                            tool_call,
                            tool_result,
                            chat_session,
                            iteration + 1,
                        ),
                        memory_context=memory_context,
                        conversation_context=conversation_context,
                        current_knowledge=current_knowledge,
                        allow_general_skill_selection=False,
                    )
                    callbacks.apply_result(
                        request.tenant_id,
                        chat_session,
                        continuation_result,
                        active_skill,
                    )
                    self.db.commit()
                    self.db.refresh(chat_session)
                    step_result = continuation_result
                break
            if not model_config:
                callbacks.advance_after_tool(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    step_result,
                    tool_result,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                break
            callbacks.emit_thinking_status(
                chat_session, iteration + 1, stream_events, status_callback
            )
            continuation_result = callbacks.run_step(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                repair_reason="tool_continuation",
                repair_context=callbacks.continuation_context(
                    request.tenant_id,
                    tool_call,
                    tool_result,
                    chat_session,
                    iteration + 1,
                ),
                memory_context=memory_context,
                conversation_context=conversation_context,
                current_knowledge=current_knowledge,
                allow_general_skill_selection=False,
            )
            if current_knowledge and not continuation_result.knowledge_results:
                continuation_result.knowledge_results = current_knowledge
            callbacks.apply_result(
                request.tenant_id, chat_session, continuation_result, active_skill
            )
            self.db.commit()
            self.db.refresh(chat_session)
            step_result = continuation_result
            if step_result.tool_call:
                payload = callbacks.decision_payload(
                    iteration + 1, "model_tool_call", step_result.tool_call
                )
                self.events.record(
                    request.tenant_id, chat_session.id, "agent_loop_continued", payload
                )
                if stream_events is not None:
                    stream_events.append(("agent_loop_continued", payload))
                continue
            payload = callbacks.decision_payload(iteration + 1, "respond")
            self.events.record(
                request.tenant_id, chat_session.id, "agent_loop_completed", payload
            )
            if stream_events is not None:
                stream_events.append(("agent_loop_completed", payload))
            callbacks.advance_after_tool(
                request.tenant_id,
                chat_session,
                active_skill,
                StepAgentResult(
                    tool_call=tool_call,
                    next_step_id=step_result.next_step_id,
                    is_step_completed=True,
                ),
                tool_result,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            break
        return step_result, tool_result
