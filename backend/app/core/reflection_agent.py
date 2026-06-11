from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.session.helpers import public_session
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "reflection_prompt.md"


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
    ) -> ReflectionDecision:
        if not action_needs_reflection(router_decision, step_result, tool_result):
            return ReflectionDecision()

        available_skill_ids = {skill.skill_id for skill in available_skills}
        payload = {
            "user_message": message,
            "conversation_context": conversation_context or {},
            "current_session": public_session(session).model_dump(),
            "active_skill": active_skill.content_json if active_skill else None,
            "router_decision": router_decision.model_dump(),
            "step_result": step_result.model_dump(),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "available_skills": [
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "trigger_intents": skill.content_json.get("trigger_intents", []),
                    "required_info": skill.content_json.get("required_info", []),
                    "nodes": [
                        {
                            "node_id": node.get("node_id"),
                            "type": node.get("type"),
                            "name": node.get("name"),
                            "allowed_actions": node.get("allowed_actions", []),
                        }
                        for node in skill.content_json.get("nodes", [])
                        if isinstance(node, dict)
                    ],
                    "steps": [
                        {
                            "step_id": step.get("step_id"),
                            "name": step.get("name"),
                            "allowed_actions": step.get("allowed_actions", []),
                        }
                        for step in skill.content_json.get("steps", [])
                        if isinstance(step, dict)
                    ],
                }
                for skill in available_skills
            ],
            "available_tools": self._available_tool_payload(available_tools, available_skill_ids),
        }
        try:
            raw = LLMClient(model_config).generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
            return ReflectionDecision.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Reflection agent returned invalid JSON schema: {exc}") from exc

    def _available_tool_payload(
        self,
        available_tools: list[Tool],
        available_skill_ids: set[str],
    ) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for tool in available_tools:
            if not tool.enabled:
                continue
            raw_allowed = [
                str(skill_id)
                for skill_id in (tool.allowed_skills_json or [])
                if str(skill_id).strip()
            ]
            allowed_skills = [skill_id for skill_id in raw_allowed if skill_id in available_skill_ids]
            if raw_allowed and not allowed_skills:
                continue
            payload.append(
                {
                    "name": tool.name,
                    "display_name": tool.display_name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "allowed_skills": allowed_skills,
                }
            )
        return payload


def action_needs_reflection(
    router_decision: RouterDecision,
    step_result: StepAgentResult,
    tool_result: ToolResult | None,
) -> bool:
    if router_decision.decision in {"clarify", "answer_only", "answer_chitchat_then_resume"}:
        return bool(tool_result or step_result.tool_call or step_result.knowledge_query)
    return bool(
        tool_result
        or step_result.tool_call
        or step_result.knowledge_query
        or step_result.knowledge_results
        or step_result.slot_updates
        or step_result.next_step_id
        or step_result.is_step_completed
        or step_result.handoff
    )


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
