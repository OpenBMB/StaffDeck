from __future__ import annotations

from pathlib import Path

from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.session.session_schema import StepAgentResult


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "step_agent_prompt.md"


class StepAgent:
    def run(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        repair_context: dict[str, object] | None = None,
    ) -> StepAgentResult:
        payload = {
            "user_message": message,
            "active_skill": skill.content_json if skill else None,
            "active_step": _active_step(skill, session.active_step_id),
            "slots": session.slots_json or {},
            "last_agent_question": session.last_agent_question,
            "skill_stack": session.skill_stack_json or [],
            "repair_context": repair_context,
            "available_tools": [
                {
                    "name": tool.name,
                    "display_name": tool.display_name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "allowed_skills": tool.allowed_skills_json,
                }
                for tool in tools
                if tool.enabled
            ],
        }
        try:
            raw = LLMClient(model_config).generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
            return StepAgentResult.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Step agent returned invalid JSON schema: {exc}") from exc


def _active_step(skill: Skill | None, active_step_id: str | None) -> dict[str, object] | None:
    if not skill or not active_step_id:
        return None
    for step in skill.content_json.get("steps", []):
        if isinstance(step, dict) and step.get("step_id") == active_step_id:
            return step
    return None
