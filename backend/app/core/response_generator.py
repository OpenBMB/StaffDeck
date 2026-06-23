from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from app.db.models import ChatSession, ModelConfig, Skill
from app.knowledge.citations import knowledge_citations_from_results
from app.llm import LLMClient, LLMError
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "response_generator_prompt.md"
FALLBACK_REPLY = "抱歉，我暂时无法处理这个问题。您可以换个说法，或者我可以帮您转人工。"


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
    ) -> str:
        payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
        )
        try:
            text = LLMClient(model_config).generate_text(self._system_prompt(persona_prompt), payload)
            reply = text.strip() or step_result.reply or self._minimal_fallback(router_decision)
            return self._visible_reply_or_fallback(reply, session, step_result, tool_result, skill)
        except Exception:
            return self._visible_reply_or_fallback(
                step_result.reply or self._minimal_fallback(router_decision),
                session,
                step_result,
                tool_result,
                skill,
            )

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
    ) -> Iterator[str]:
        payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
        )
        try:
            stream = LLMClient(model_config).generate_text_stream(self._system_prompt(persona_prompt), payload)
            chunks = [chunk for chunk in stream]
            reply = self._visible_reply_or_fallback(
                "".join(chunks).strip() or step_result.reply or self._minimal_fallback(router_decision),
                session,
                step_result,
                tool_result,
                skill,
            )
            yield from self.chunk_text(reply)
            return
        except Exception:
            yield from self.chunk_text(
                self._visible_reply_or_fallback(
                    step_result.reply or self._minimal_fallback(router_decision),
                    session,
                    step_result,
                    tool_result,
                    skill,
                )
            )

    def chunk_text(self, text: str, chunk_size: int = 8) -> Iterator[str]:
        stripped = text.strip()
        if not stripped:
            return
        for index in range(0, len(stripped), chunk_size):
            yield stripped[index : index + chunk_size]

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
    ) -> dict[str, object]:
        knowledge_context = self._current_knowledge_context(message, session, step_result)
        return {
            "user_message": message,
            "conversation_context": conversation_context or {},
            "session": {
                "active_skill_id": session.active_skill_id,
                "active_step_id": session.active_step_id,
                "slots": session.slots_json or {},
                "awaiting_input": session.awaiting_input_json,
                "pending_tasks": session.pending_tasks_json or [],
                "knowledge_context": knowledge_context,
            },
            "active_skill": skill.content_json if skill else None,
            "progress": self._progress_payload(session, skill, step_result, tool_result),
            "router_decision": router_decision.model_dump(),
            "step_result": step_result.model_dump(),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "memory_context": memory_context or [],
            "knowledge_citation_hints": knowledge_citations_from_results(knowledge_context),
            "response_rules": skill.content_json.get("response_rules", []) if skill else [],
        }

    def _current_knowledge_context(
        self,
        message: str,
        session: ChatSession,
        step_result: StepAgentResult,
    ) -> list[dict[str, object]]:
        if step_result.knowledge_results:
            return list(step_result.knowledge_results)
        for item in reversed(session.knowledge_context_json or []):
            if isinstance(item, dict) and item.get("source_message") == message:
                return [item]
        return []

    def _is_user_safe(self, text: str) -> bool:
        internal_terms = (
            "当前用户消息",
            "会话状态",
            "技能进度",
            "可用技能",
            "路由决策",
            "Router",
            "router",
            "Step Agent",
            "step agent",
            "decision",
            "JSON",
            "tool_call",
            "session_state",
        )
        return not any(term in text for term in internal_terms)

    def _visible_reply_or_fallback(
        self,
        reply: str,
        session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        skill: Skill | None = None,
    ) -> str:
        completion_ready = self._skill_completion_ready(session, skill, step_result, tool_result)
        completion_fallback = self._completion_fallback() if completion_ready else ""
        prefer_step_reply = self._prefer_step_reply_for_knowledge(step_result)
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
            if not self._is_user_safe(stripped):
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

    def _prefer_step_reply_for_knowledge(self, step_result: StepAgentResult) -> bool:
        step_reply = (step_result.reply or "").strip()
        if not step_reply:
            return False
        if self._is_generic_step_reply(step_reply):
            return False
        return (
            "[1]" in step_reply
            or "根据业务资料" in step_reply
            or ("业务资料" in step_reply and "引用" in step_reply)
        )

    def _is_generic_step_reply(self, reply: str) -> bool:
        generic_terms = (
            "已记录完整信息",
            "请问还有其他需要帮助的吗",
            "请您再补充一下具体诉求",
            "我会继续帮您处理",
        )
        return any(term in reply for term in generic_terms)

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
        base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        if not persona_prompt:
            return base_prompt
        return f"{persona_prompt.strip()}\n\n{base_prompt}"
