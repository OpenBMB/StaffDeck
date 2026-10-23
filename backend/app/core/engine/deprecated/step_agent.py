from __future__ import annotations

from app import paths
from app.core.base.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_deferred_intents,
    compact_knowledge_context,
    compact_step_router_decision,
    compact_step_skill_context,
)
from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    STEP_AGENT_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_prompt.md"
RULE_PATHS = {
    "repair": paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_repair_rules.md",
    "tool_continuation": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_tool_continuation_rules.md",
    "knowledge": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_knowledge_rules.md",
    "awaiting_input": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_awaiting_input_rules.md",
    "general_skill": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_general_skill_rules.md",
    "tools": paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_tool_rules.md",
}
INTERNAL_SCHEDULER_SLOT_KEYS = {"_graph_pending_steps"}
GENERAL_SKILL_TOOL_PREFIX = "general_skill."


class StepAgent:
    def run(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision | None = None,
        repair_context: dict[str, object] | None = None,
        recent_messages: list[dict[str, str]] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        current_knowledge: list[dict[str, object]] | None = None,
    ) -> StepAgentResult:
        compact_knowledge = compact_knowledge_context(current_knowledge)
        compact_repair = _compact_repair_context(repair_context)
        active_skill = (
            compact_step_skill_context(
                skill.content_json,
                session.active_step_id,
                skill_id=skill.skill_id,
                name=skill.name,
                description=skill.description,
            )
            if skill
            else None
        )
        available_tools = _available_tools_for_step(
            active_skill,
            session.slots_json,
            tools,
        )
        deferred_intents = compact_deferred_intents(
            session.pending_tasks_json,
            selected_task_id=router_decision.selected_task_id if router_decision else None,
        )
        stage_data = {
            "active_skill": active_skill,
            "retrieved_knowledge": compact_knowledge,
            "router_decision": compact_step_router_decision(
                router_decision.model_dump(mode="json") if router_decision else None
            ),
            "slots": _step_agent_slots(session.slots_json),
            "awaiting_input": compact_awaiting_input(session.awaiting_input_json),
            "deferred_intents": deferred_intents,
            "repair_context": compact_repair,
            "available_tools": available_tools,
        }
        payload = stage_payload(
            phase="Step Agent",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=None,
            instructions=_step_instructions(
                repair_context=compact_repair,
                retrieved_knowledge=compact_knowledge,
                awaiting_input=stage_data["awaiting_input"],
                available_tools=available_tools,
            ),
            stage_data=stage_data,
            output_contract=STEP_AGENT_OUTPUT_SCHEMA,
        )
        try:
            operation = "step_agent.repair" if repair_context else "step_agent.run"
            repair_reason = str((repair_context or {}).get("reason") or "") or None
            with llm_operation(operation, repair_reason=repair_reason):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            result = StepAgentResult.model_validate(raw)
            if not result.action:
                result.action = _infer_action(result, router_decision)
            return result
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Step agent returned invalid JSON schema: {exc}") from exc


def _step_agent_slots(slots: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(slots, dict):
        return {}
    return {
        key: value
        for key, value in slots.items()
        if str(key) not in INTERNAL_SCHEDULER_SLOT_KEYS
    }


def _compact_repair_context(
    repair_context: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(repair_context, dict):
        return None
    projected = dict(repair_context)
    if projected.get("reason") == "knowledge_continuation":
        projected.pop("knowledge_results", None)
        projected["knowledge_results_available_in"] = "retrieved_knowledge"
    return projected


def _step_instructions(
    *,
    repair_context: dict[str, object] | None,
    retrieved_knowledge: list[dict[str, object]],
    awaiting_input: dict[str, object] | None,
    available_tools: list[dict[str, object]],
) -> str:
    sections = [PROMPT_PATH.read_text(encoding="utf-8").strip()]
    repair_reason = str((repair_context or {}).get("reason") or "")
    if repair_context and repair_reason not in {"tool_continuation", "knowledge_continuation"}:
        sections.append(RULE_PATHS["repair"].read_text(encoding="utf-8").strip())
    if repair_reason == "tool_continuation":
        sections.append(
            RULE_PATHS["tool_continuation"].read_text(encoding="utf-8").strip()
        )
    if retrieved_knowledge or repair_reason == "knowledge_continuation":
        sections.append(RULE_PATHS["knowledge"].read_text(encoding="utf-8").strip())
    if awaiting_input:
        sections.append(RULE_PATHS["awaiting_input"].read_text(encoding="utf-8").strip())
    if available_tools:
        sections.append(RULE_PATHS["tools"].read_text(encoding="utf-8").strip())
    if any(
        str(tool.get("name") or "").startswith(GENERAL_SKILL_TOOL_PREFIX)
        for tool in available_tools
    ):
        sections.append(RULE_PATHS["general_skill"].read_text(encoding="utf-8").strip())
    return "\n\n".join(section for section in sections if section)


def _available_tools_for_step(
    active_skill: dict[str, object] | None,
    slots: dict[str, object] | None,
    tools: list[Tool],
) -> list[dict[str, object]]:
    if not isinstance(active_skill, dict):
        return []
    current_step = active_skill.get("current_step")
    if not isinstance(current_step, dict):
        return []
    current_expected = [
        str(field)
        for field in current_step.get("expected_user_info") or []
        if str(field).strip()
    ]
    slot_values = slots if isinstance(slots, dict) else {}
    if any(not _slot_has_value(slot_values, field) for field in current_expected):
        candidate_steps = [current_step]
    else:
        candidate_steps = [
            current_step,
            *[
                step
                for step in active_skill.get("next_steps") or []
                if isinstance(step, dict)
            ],
        ]
    actions = {
        str(action).strip()
        for step in candidate_steps
        for action in step.get("allowed_actions") or []
        if str(action).strip()
    }
    explicit_names = {
        action.split(":", 1)[1]
        for action in actions
        if action.startswith("call_tool:") and ":" in action
    }
    allow_any = "call_tool" in actions
    projected: list[dict[str, object]] = []
    for tool in tools:
        if not getattr(tool, "enabled", False):
            continue
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        if not name.startswith(GENERAL_SKILL_TOOL_PREFIX) and (
            not allow_any and name not in explicit_names
        ):
            continue
        projected.append(
            {
                "name": name,
                "description": str(getattr(tool, "description", "") or "").strip(),
                "input_schema": getattr(tool, "input_schema", None) or {},
            }
        )
    return projected


def _slot_has_value(slots: dict[str, object], field: str) -> bool:
    value = slots.get(field)
    return value is not None and value != "" and value != [] and value != {}


def _infer_action(
    result: StepAgentResult,
    router_decision: RouterDecision | None,
) -> str:
    if result.tool_call:
        return "call_tool"
    if result.knowledge_query:
        return "query_knowledge"
    if result.handoff:
        return "handoff"
    if result.next_step_id or result.is_step_completed:
        return "advance"
    if result.reply:
        return "clarify" if router_decision and router_decision.decision == "clarify" else "ask_user"
    return "reply"
