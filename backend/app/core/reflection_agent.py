from __future__ import annotations

from pydantic import BaseModel

from app import paths
from app.core.base.context_projection import (
    compact_conversation_context,
    compact_current_step,
    compact_router_decision,
    compact_step_result,
)
from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    REFLECTION_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "reflection_prompt.md"


class ReflectionDecision(BaseModel):
    action: str = "pass"
    needs_retry: bool = False
    reason: str | None = None
    target_skill_id: str | None = None
    target_step_id: str | None = None
    target_tool_name: str | None = None


class ReflectionAgent:
    def review(
        self,
        message: str,
        session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        available_skills: list[Skill],
        available_tools: list[Tool],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ReflectionDecision:
        if not action_needs_reflection(router_decision, step_result, tool_result):
            return ReflectionDecision()

        stage_data = {
            "current_step": compact_current_step(
                active_skill.content_json if active_skill else None,
                session.active_step_id,
            ),
            "rules": {
                "response_rules": active_skill.content_json.get("response_rules", [])
                if active_skill
                else [],
            },
            "slots": session.slots_json or {},
            "router_decision": compact_router_decision(
                router_decision.model_dump(mode="json")
            ),
            "step_result": compact_step_result(step_result.model_dump(mode="json")),
            "tool_result": tool_result.model_dump() if tool_result else None,
        }
        payload = stage_payload(
            phase="Reflection",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=REFLECTION_OUTPUT_SCHEMA,
        )
        try:
            with llm_operation("reflection.review"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            return ReflectionDecision.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Reflection agent returned invalid JSON schema: {exc}") from exc

def action_needs_reflection(
    router_decision: RouterDecision,
    step_result: StepAgentResult,
    tool_result: ToolResult | None,
) -> bool:
    if router_decision.decision in {"clarify", "answer_only"}:
        return bool(tool_result or step_result.tool_call or step_result.knowledge_query)
    if (
        tool_result
        or step_result.tool_call
        or step_result.knowledge_query
        or step_result.knowledge_results
        or step_result.handoff
    ):
        return True
    # Advancing to a decided next node is normal skill graph progress.
    # Reflection is reserved for external actions or the overall skill completion.
    if step_result.next_step_id:
        return False
    return bool(step_result.is_step_completed)


def tool_result_needs_reflection(tool_result: ToolResult | None) -> bool:
    if tool_result is None:
        return False
    return not tool_result.success


def _data_indicates_unexpected_result(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    if not isinstance(value, dict):
        return False

    if value.get("found") is False or value.get("success") is False:
        return True
    for key in ("miss_reason", "not_found", "empty", "error", "error_code"):
        if value.get(key):
            return True
    for key in ("results", "items", "data"):
        nested = value.get(key)
        if isinstance(nested, list) and len(nested) == 0:
            return True
    return False
