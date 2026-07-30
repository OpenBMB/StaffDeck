from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.db.models import ChatSession, GeneralSkill, ModelConfig
from app.general_skills.schema import GeneralSkillRunResponse, GeneralSkillSelection
from app.llm import LLMError
from app.session.session_schema import ChatTurnRequest
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


class LegacyGeneralSkillAction:
    def __init__(self, events: Any) -> None:
        self.events = events

    def execute_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        agent_id: str | None,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        *,
        tool_prefix: str,
        list_skills: Callable[[str, str | None], list[GeneralSkill]],
        model_resolver: Callable[[ChatTurnRequest, str | None], ModelConfig | None],
        precondition_error_type: type[Exception],
        validator: Callable[..., ToolResult | None],
        runner: Callable[..., GeneralSkillRunResponse],
    ) -> ToolResult:
        slug = tool_call.name.removeprefix(tool_prefix).strip()
        if not slug:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="INVALID_GENERAL_SKILL", message="通用技能名称为空。"),
            )
        skill = next(
            (item for item in list_skills(request.tenant_id, agent_id) if item.slug == slug),
            None,
        )
        if not skill:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(
                    code="GENERAL_SKILL_NOT_FOUND", message="通用技能不存在或未发布。"
                ),
            )
        try:
            model_config = model_resolver(request, agent_id)
        except Exception as exc:
            if not isinstance(exc, precondition_error_type):
                raise
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(
                    code=str(getattr(exc, "code", "")).upper(),
                    message=str(getattr(exc, "message", exc)),
                ),
            )
        if not model_config:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="MISSING_MODEL_CONFIG", message="没有默认模型配置。"),
            )
        query = str(tool_call.arguments.get("query") or request.message).strip()
        guard_result = validator(
            request,
            chat_session,
            tool_call,
            skill,
            query,
            model_config,
            agent_id,
            conversation_context,
            memory_context,
        )
        if guard_result is not None:
            return guard_result
        emitted_trace_keys: set[str] = set()

        def emit_trace(trace_item: dict[str, Any]) -> None:
            emitted_trace_keys.add(self.trace_key(trace_item))
            payload: dict[str, object] = {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "operation": str(tool_call.arguments.get("operation") or "execute"),
                **trace_item,
            }
            self.events.record(
                request.tenant_id, chat_session.id, "general_skill_trace", payload
            )
            if stream_events is not None:
                stream_events.append(("general_skill_trace", payload))

        try:
            response = runner(
                skill,
                query,
                model_config,
                request.user_id,
                event_sink=emit_trace,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        except Exception as exc:  # noqa: BLE001 - legacy runner isolation boundary
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="GENERAL_SKILL_EXECUTION_ERROR", message=str(exc)),
            )
        for trace_item in response.execution_trace:
            if self.trace_key(trace_item) not in emitted_trace_keys:
                emit_trace(trace_item)
        structured = (
            response.structured_result
            if isinstance(response.structured_result, dict)
            else {}
        )
        success = structured.get("success")
        is_success = True if success is None else bool(success)
        finished_payload: dict[str, object] = {
            "skill_slug": response.skill_slug,
            "operation": response.operation,
            "success": is_success,
            "stdout_preview": response.stdout[:600],
            "stderr_preview": response.stderr[:600],
            "structured_result": response.structured_result,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_run_finished",
            finished_payload,
        )
        if stream_events is not None:
            stream_events.append(("general_skill_run_finished", finished_payload))
        data = {
            "skill_slug": response.skill_slug,
            "operation": response.operation,
            "reply": response.reply,
            "structured_result": response.structured_result,
            "stdout": response.stdout,
            "stderr": response.stderr,
            "generated_code": response.generated_code,
            "execution_trace": response.execution_trace,
        }
        if is_success:
            return ToolResult(tool_name=tool_call.name, success=True, data=data, error=None)
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=data,
            error=ToolError(
                code=str(structured.get("error") or "GENERAL_SKILL_FAILED"),
                message=str(
                    structured.get("message")
                    or response.reply
                    or "通用技能执行失败。"
                ),
            ),
        )

    def validate_tool_match(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        requested_skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        agent_id: str | None,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
        *,
        validated_calls: set[tuple[str, str, str]],
        call_key: Callable[[str, str, str], tuple[str, str, str]],
        list_skills: Callable[[str, str | None], list[GeneralSkill]],
        selector: Callable[..., GeneralSkillSelection],
    ) -> ToolResult | None:
        key = call_key(chat_session.id, tool_call.name, query)
        if key in validated_calls:
            validated_calls.discard(key)
            return None
        if not query:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "通用技能调用缺少自然语言任务。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_MISMATCH",
                    message="通用技能调用缺少自然语言任务。",
                ),
            )
        candidates = list_skills(request.tenant_id, agent_id)
        if not candidates:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "当前员工没有可用通用技能。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_NOT_FOUND", message="当前员工没有可用通用技能。"
                ),
            )
        try:
            selection = selector(
                query,
                candidates,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError:
            return None
        selected_slug = selection.selected_slug if selection.use_general_skill else None
        if selected_slug == requested_skill.slug:
            return None
        payload = {
            "requested_slug": requested_skill.slug,
            "selected_slug": selected_slug,
            "reason": selection.reason,
            "query": query,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id, chat_session.id, "general_skill_guard_rejected", payload
        )
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=payload,
            error=ToolError(
                code="GENERAL_SKILL_MISMATCH",
                message="通用技能与当前子任务不匹配，已取消调用。",
            ),
        )

    @staticmethod
    def call_key(session_id: str, tool_name: str, query: str) -> tuple[str, str, str]:
        return session_id, tool_name.strip(), " ".join(query.split())

    @staticmethod
    def trace_key(trace_item: dict[str, Any]) -> str:
        return json.dumps(trace_item, ensure_ascii=False, sort_keys=True, default=str)
