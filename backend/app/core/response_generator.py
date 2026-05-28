from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from app.db.models import ChatSession, ModelConfig, Skill
from app.llm import LLMClient, LLMError
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "response_generator_prompt.md"
FALLBACK_REPLY = "抱歉，我暂时无法处理这个问题。您可以换个说法，或者我可以帮您转人工。"
PENDING_ACTION_TERMS = (
    "请稍候",
    "请稍等",
    "稍候",
    "稍等",
    "稍后",
    "正在为您",
    "正在帮您",
    "正在处理",
    "正在创建",
    "正在查询",
    "正在核实",
    "处理中",
    "创建中",
    "查询中",
    "核实中",
    "稍后反馈",
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
    ) -> str:
        static_reply = self._static_reply(message, session, router_decision, step_result, tool_result)
        if static_reply:
            return static_reply

        payload = self._payload(message, session, skill, router_decision, step_result, tool_result, memory_context)
        try:
            text = LLMClient(model_config).generate_text(self._system_prompt(persona_prompt), payload)
            reply = text.strip() or step_result.reply or FALLBACK_REPLY
            return self._visible_reply_or_fallback(reply, session, step_result, tool_result)
        except LLMError:
            return self._visible_reply_or_fallback(step_result.reply or "", session, step_result, tool_result)

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
    ) -> Iterator[str]:
        static_reply = self._static_reply(message, session, router_decision, step_result, tool_result)
        if static_reply:
            yield from self.chunk_text(static_reply)
            return

        payload = self._payload(message, session, skill, router_decision, step_result, tool_result, memory_context)
        try:
            emitted = False
            stream = LLMClient(model_config).generate_text_stream(self._system_prompt(persona_prompt), payload)
            if tool_result is None:
                chunks = [chunk for chunk in stream]
                reply = self._visible_reply_or_fallback("".join(chunks).strip(), session, step_result, tool_result)
                yield from self.chunk_text(reply)
                return
            for chunk in stream:
                emitted = True
                yield chunk
            if not emitted:
                yield from self.chunk_text(
                    self._visible_reply_or_fallback(step_result.reply or "", session, step_result, tool_result)
                )
        except LLMError:
            yield from self.chunk_text(
                self._visible_reply_or_fallback(step_result.reply or "", session, step_result, tool_result)
            )

    def chunk_text(self, text: str, chunk_size: int = 8) -> Iterator[str]:
        stripped = text.strip()
        if not stripped:
            return
        for index in range(0, len(stripped), chunk_size):
            yield stripped[index : index + chunk_size]

    def _static_reply(
        self,
        message: str,
        session: ChatSession,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> str | None:
        if router_decision.decision == "handoff_human":
            return "好的，我会为您转接人工客服，请稍等。"
        if router_decision.decision == "clarify" and router_decision.clarification_question:
            if self._is_user_safe(router_decision.clarification_question):
                return router_decision.clarification_question
            if step_result.reply and self._is_user_safe(step_result.reply):
                return step_result.reply
            return self._fallback_for_session(session)
        if tool_result and not tool_result.success:
            return "抱歉，我现在暂时无法查询到相关信息。您可以稍后再试，或我帮您转人工处理。"
        return None

    def _payload(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "user_message": message,
            "session": {
                "active_skill_id": session.active_skill_id,
                "active_step_id": session.active_step_id,
                "slots": session.slots_json or {},
                "last_agent_question": session.last_agent_question,
            },
            "active_skill": skill.content_json if skill else None,
            "router_decision": router_decision.model_dump(),
            "step_result": step_result.model_dump(),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "memory_context": memory_context or [],
            "response_rules": skill.content_json.get("response_rules", []) if skill else [],
        }

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

    def _is_unverified_pending_reply(self, text: str, tool_result: ToolResult | None) -> bool:
        if tool_result is not None:
            return False
        normalized = text.replace(" ", "")
        return any(term in normalized for term in PENDING_ACTION_TERMS)

    def _visible_reply_or_fallback(
        self,
        reply: str,
        session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> str:
        candidates = (
            reply,
            step_result.reply or "",
            session.last_agent_question or "",
            self._fallback_for_session(session),
            FALLBACK_REPLY,
        )
        for candidate in candidates:
            stripped = candidate.strip()
            if not stripped:
                continue
            if not self._is_user_safe(stripped):
                continue
            if self._is_unverified_pending_reply(stripped, tool_result):
                continue
            return stripped
        return FALLBACK_REPLY

    def _fallback_for_session(self, session: ChatSession) -> str:
        return "请您再补充一下具体诉求，我会继续帮您处理。"

    def _system_prompt(self, persona_prompt: str | None) -> str:
        base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        if not persona_prompt:
            return base_prompt
        return f"{persona_prompt.strip()}\n\n{base_prompt}"
