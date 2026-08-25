from __future__ import annotations

import re
from collections.abc import Iterator

from app import paths
from app.core.context_projection import (
    compact_citation_hints,
    compact_current_step,
    compact_knowledge_context,
    compact_response_step_result,
)
from app.db.models import ChatSession, ModelConfig, Skill
from app.knowledge.citations import knowledge_citations_from_results
from app.llm import LLMClient
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "response_generator_prompt.md"
FALLBACK_REPLY = "抱歉，我暂时无法处理这个问题。您可以换个说法，或者我可以帮您转人工。"
MODEL_FAILURE_SUGGESTION = "请检查模型配置、API Key、网络或模型服务状态后重试。"
TOOL_FAILURE_SUGGESTION = "请检查工具配置、调用参数或外部服务状态后重试。"


def public_error_detail(value: object, fallback: str = "未知原因") -> str:
    detail = re.sub(r"\s+", " ", str(value or "")).strip()
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", detail)
    detail = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", detail)
    if not detail:
        detail = fallback
    return detail[:500]


def format_runtime_failure_reply(
    title: str,
    detail: object,
    code: str | None = None,
    suggestion: str | None = None,
) -> str:
    normalized_detail = public_error_detail(detail)
    normalized_code = public_error_detail(code, "").strip()
    code_part = f"（{normalized_code}）" if normalized_code else ""
    normalized_detail = normalized_detail.rstrip("。.!！")
    suffix = (suggestion or "请稍后重试，或联系管理员查看执行记录。").strip()
    return f"{title}{code_part}：{normalized_detail}。{suffix}"


def model_failure_suggestion(detail: object) -> str:
    return MODEL_FAILURE_SUGGESTION


def tool_failure_reply(tool_result: ToolResult) -> str:
    error = tool_result.error
    code = error.code if error else None
    detail = error.message if error else "工具未返回可用结果"
    return format_runtime_failure_reply(
        f"工具调用失败：{tool_result.tool_name}",
        detail,
        code,
        TOOL_FAILURE_SUGGESTION,
    )


class ResponseGenerator:
    def generate(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> str:
        if self._can_use_step_reply_directly(step_result, tool_result, task_results):
            return step_result.reply.strip()
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            if tool_result and not tool_result.success and not task_results:
                return tool_failure_reply(tool_result)
            with llm_operation("response.generate"):
                text = LLMClient(model_config).generate_text(
                    unified_system_prompt(), payload
                )
            reply = text.strip() or step_result.reply or self._minimal_fallback(router_decision)
            return self._visible_reply_or_fallback(
                reply, session, router_decision, step_result, tool_result, skill
            )
        except Exception as exc:
            return format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))

    def generate_stream(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> Iterator[str]:
        if self._can_use_step_reply_directly(step_result, tool_result, task_results):
            yield from self.chunk_text(step_result.reply or "")
            return
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            if tool_result and not tool_result.success and not task_results:
                yield from self.chunk_text(tool_failure_reply(tool_result))
                return
            if router_decision.decision == "clarify" and step_result.reply:
                yield from self.chunk_text(step_result.reply)
                return
            with llm_operation("response.generate_stream"):
                stream = LLMClient(model_config).generate_text_stream(
                    unified_system_prompt(), payload
                )
                reply_parts: list[str] = []
                has_streamed = False
                for chunk in stream:
                    if not chunk:
                        continue
                    reply_parts.append(chunk)
                    if not has_streamed:
                        preview = "".join(reply_parts).strip()
                        if not preview:
                            continue
                        has_streamed = True
                    yield chunk
            if has_streamed:
                return
            reply = self._visible_reply_or_fallback(
                "".join(reply_parts).strip() or step_result.reply or self._minimal_fallback(router_decision),
                session,
                router_decision,
                step_result,
                tool_result,
                skill,
            )
            yield from self.chunk_text(reply)
            return
        except Exception as exc:
            yield from self.chunk_text(
                format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))
            )

    def chunk_text(self, text: str, chunk_size: int = 8) -> Iterator[str]:
        stripped = text.strip()
        if not stripped:
            return
        for index in range(0, len(stripped), chunk_size):
            yield stripped[index : index + chunk_size]

    def _can_use_step_reply_directly(
        self,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        task_results: list[dict[str, object]] | None = None,
    ) -> bool:
        return bool(
            not task_results
            and str(step_result.reply or "").strip()
            and step_result.action in {"ask_user", "clarify"}
            and tool_result is None
            and step_result.tool_call is None
            and step_result.knowledge_query is None
            and not step_result.knowledge_results
        )

    def _payload(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        projected_task_results = self._project_task_results(task_results)
        if projected_task_results:
            return {
                "user_message": message,
                "conversation_context": (
                    conversation_context if isinstance(conversation_context, dict) else {}
                ),
                "task_results": projected_task_results,
            }
        knowledge_context = self._current_knowledge_context(message, session, step_result)
        compact_knowledge = compact_knowledge_context(knowledge_context)
        return {
            "user_message": message,
            "conversation_context": (
                conversation_context if isinstance(conversation_context, dict) else {}
            ),
            "current_step": compact_current_step(
                skill.content_json if skill else None, session.active_step_id
            ),
            "progress": self._progress_payload(session, skill, step_result, tool_result),
            "slots": session.slots_json or {},
            "step_summary": compact_response_step_result(
                step_result.model_dump(mode="json")
            ),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "retrieved_knowledge": compact_knowledge,
            "knowledge_citation_hints": compact_citation_hints(
                knowledge_citations_from_results(knowledge_context)
            ),
            "response_rules": skill.content_json.get("response_rules", []) if skill else [],
        }

    def _project_task_results(
        self, task_results: list[dict[str, object]] | None
    ) -> list[dict[str, object]]:
        if not isinstance(task_results, list):
            return []
        projected: list[dict[str, object]] = []
        for item in task_results:
            if not isinstance(item, dict):
                continue
            skill_content = item.get("skill_content")
            content = skill_content if isinstance(skill_content, dict) else {}
            raw_step_result = item.get("step_result")
            step_result = raw_step_result if isinstance(raw_step_result, dict) else {}
            knowledge_context = (
                step_result.get("knowledge_results")
                if isinstance(step_result.get("knowledge_results"), list)
                else []
            )
            compact_knowledge = compact_knowledge_context(knowledge_context)
            task_citations = (
                item.get("knowledge_citations")
                if isinstance(item.get("knowledge_citations"), list)
                else knowledge_citations_from_results(knowledge_context)
            )
            projected.append(
                {
                    "task": item.get("task") or "当前任务",
                    "task_frame_id": item.get("task_frame_id"),
                    "status": item.get("status"),
                    "task_summary": item.get("task_summary"),
                    "structured_result": item.get("structured_result"),
                    "current_step": compact_current_step(
                        content, str(item.get("current_step_id") or "") or None
                    ),
                    "slots": item.get("slots") if isinstance(item.get("slots"), dict) else {},
                    "step_summary": compact_response_step_result(step_result),
                    "tool_result": item.get("tool_result"),
                    "artifacts": (
                        item.get("artifacts")
                        if isinstance(item.get("artifacts"), list)
                        else []
                    ),
                    "retrieved_knowledge": compact_knowledge,
                    "knowledge_citation_hints": compact_citation_hints(
                        task_citations
                    ),
                    "response_rules": content.get("response_rules", []),
                    "handoff_info": item.get("handoff_info"),
                }
            )
        return projected

    def _current_knowledge_context(
        self,
        message: str,
        session: ChatSession,
        step_result: StepAgentResult,
    ) -> list[dict[str, object]]:
        if step_result.knowledge_results:
            return list(step_result.knowledge_results)
        return []

    def _visible_reply_or_fallback(
        self,
        reply: str,
        session: ChatSession,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        skill: Skill | None = None,
    ) -> str:
        completion_ready = self._skill_completion_ready(session, skill, step_result, tool_result)
        completion_fallback = self._completion_fallback() if completion_ready else ""
        prefer_step_reply = bool(step_result.reply and router_decision.decision == "clarify")
        candidates = self._reply_candidates(
            reply,
            step_result.reply or "",
            completion_fallback,
            self._minimal_fallback_for_session(session),
            tool_result,
            completion_ready,
            prefer_step_reply,
        )
        for candidate in candidates:
            stripped = candidate.strip()
            if not stripped:
                continue
            return stripped
        return FALLBACK_REPLY

    def _reply_candidates(
        self,
        model_reply: str,
        step_reply: str,
        completion_fallback: str,
        session_fallback: str,
        tool_result: ToolResult | None,
        completion_ready: bool,
        prefer_step_reply: bool,
    ) -> tuple[str, ...]:
        if prefer_step_reply:
            return (
                step_reply,
                model_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        if completion_ready:
            return (
                model_reply,
                completion_fallback,
                step_reply,
                session_fallback,
                FALLBACK_REPLY,
            )
        if tool_result is not None:
            return (
                model_reply,
                step_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        return (
            model_reply,
            step_reply,
            completion_fallback,
            session_fallback,
            FALLBACK_REPLY,
        )

    def _progress_payload(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> dict[str, object]:
        if not skill:
            return {
                "missing_current_step_info": [],
                "missing_required_info": [],
                "skill_completion_ready": False,
            }
        return {
            "missing_current_step_info": self._missing_current_step_info(session, skill),
            "missing_required_info": self._missing_required_info(session, skill),
            "skill_completion_ready": self._skill_completion_ready(session, skill, step_result, tool_result),
            "step_completed": step_result.is_step_completed,
        }

    def _skill_completion_ready(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        return not self._missing_current_step_info(session, skill) and not self._missing_required_info(session, skill)

    def _missing_current_step_info(self, session: ChatSession, skill: Skill) -> list[str]:
        step = self._current_step(session, skill)
        if not step:
            return []
        return [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _missing_required_info(self, session: ChatSession, skill: Skill) -> list[str]:
        return [
            str(field)
            for field in (skill.content_json or {}).get("required_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _current_step(self, session: ChatSession, skill: Skill) -> dict | None:
        for node in (skill.content_json or {}).get("nodes", []):
            if isinstance(node, dict) and node.get("node_id") == session.active_step_id:
                return {
                    "step_id": node.get("node_id"),
                    "node_id": node.get("node_id"),
                    "name": node.get("name"),
                    "instruction": node.get("instruction"),
                    "expected_user_info": node.get("expected_user_info", []),
                    "allowed_actions": node.get("allowed_actions", []),
                }
        return None

    def _slot_has_value(self, slots: dict, field: str) -> bool:
        value = slots.get(field)
        return value is not None and value != ""

    def _completion_fallback(self) -> str:
        return "已记录完整信息。请问还有其他需要帮助的吗？"

    def _minimal_fallback_for_session(self, session: ChatSession) -> str:
        return "请您再补充一下具体诉求，我会继续帮您处理。"

    def _minimal_fallback(self, router_decision: RouterDecision) -> str:
        if router_decision.decision == "clarify" and router_decision.clarification_question:
            return router_decision.clarification_question
        return FALLBACK_REPLY

    def _system_prompt(self, persona_prompt: str | None) -> str:
        return unified_system_prompt()

    def _stage_payload(
        self, payload: dict[str, object], persona_prompt: str | None
    ) -> dict[str, object]:
        stage_data = {
            key: value
            for key, value in payload.items()
            if key not in {"user_message", "conversation_context"}
        }
        if persona_prompt:
            stage_data = {"employee_identity": persona_prompt.strip(), **stage_data}
        return stage_payload(
            phase="Response Generator",
            user_message=str(payload.get("user_message") or ""),
            conversation_context=payload.get("conversation_context")
            if isinstance(payload.get("conversation_context"), dict)
            else {},
            memory_context=None,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=(
                "只输出最终用户可见的 Markdown 正文，不输出 JSON 外壳、分析过程或内部状态；"
                "除非用户明确请求代码，否则不要使用 Markdown 代码围栏。"
            ),
        )
