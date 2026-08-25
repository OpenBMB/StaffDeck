from __future__ import annotations

import copy
import json
from typing import Any

from app import paths


UNIFIED_PROMPT_PATH = (
    paths.resource_dir() / "app" / "llm" / "prompts" / "unified_agent_prompt.md"
)
STAGE_PROTOCOL_KEY = "_agent_stage"
TURN_STAGE_MESSAGES_KEY = "_agent_turn_messages"


ROUTER_OUTPUT_SCHEMA: dict[str, Any] = {
    "decision": "continue_active | switch_to_pending | create_pending | update_pending | complete_task | start_new_task | answer_only | handoff_human | clarify",
    "selected_task_id": "string?",
    "target_skill_id": "string?",
    "target_step_id": "string?",
    "confidence": "number",
    "user_intent": "string?",
    "general_intent": "string?",
    "reason": "string?",
    "clarification_question": "string?",
    "slot_hints": "object?",
    "task_frames": [
        {
            "task_id": "string?",
            "status": "pending?",
            "decision": "start_new_task | continue_active?",
            "target_skill_id": "string",
            "target_step_id": "string?",
            "user_intent": "string?",
            "slot_hints": "object?",
        }
    ],
    "pending_tasks": [
        {
            "task_id": "string?",
            "status": "pending?",
            "decision": "start_new_task | continue_active?",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "confidence": "number?",
            "user_intent": "string?",
            "reason": "string?",
            "slot_hints": "object?",
        }
    ],
    "task_updates": [
        {
            "task_id": "string",
            "status": "string?",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "user_intent": "string?",
            "reason": "string?",
            "slot_hints": "object?",
            "remove": "boolean?",
        }
    ],
}

TURN_PLANNER_OUTPUT_SCHEMA: dict[str, Any] = {
    "decision": "continue_active | switch_to_pending | complete_task | start_new_task | answer_only | handoff_human | clarify",
    "selected_task_id": "string?",
    "confidence": "number",
    "user_intent": "string?",
    "reason": "string?",
    "clarification_question": "string?",
    "task_frames": [
        {
            "task_id": "string?",
            "kind": "sop | conversation",
            "decision": "continue_active | switch_to_pending | start_new_task | answer_only | clarify",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "user_intent": "string?",
            "requirements": ["string"],
            "slot_hints": "object (use {} when empty; never null)",
            "depends_on_task_ids": ["string"],
            "execution_target": "self | team_member",
            "assignee_agent_id": "exact agent_id from stage_data.team_context.members?",
            "activation_condition": "object (use {} when empty; never null)",
        }
    ],
    "task_updates": [
        {
            "task_id": "string",
            "status": "string?",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "user_intent": "string?",
            "slot_hints": "object (use {} when empty; never null)",
            "remove": "boolean?",
        }
    ],
}

STEP_AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "action": "ask_user | clarify | reply | advance | call_tool | query_knowledge | handoff",
    "reply": "string?",
    "slot_updates": "object",
    "tool_call": {"name": "string", "arguments": "object"},
    "knowledge_query": {
        "query": "string",
        "reason": "string?",
        "scope": "object?",
        "max_chunks": "integer?",
        "query_type": "answer | policy_check | tool_discovery | skill_discovery?",
    },
    "next_step_id": "string?",
    "is_step_completed": "boolean",
    "handoff": "boolean?",
}

REFLECTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "action": "pass | retry_tool | try_other_tool | ask_user | revise_step | stop",
    "needs_retry": "boolean",
    "reason": "string?",
    "target_skill_id": "string?",
    "target_step_id": "string?",
    "target_tool_name": "string?",
}


def unified_system_prompt() -> str:
    return UNIFIED_PROMPT_PATH.read_text(encoding="utf-8").strip()


def stage_payload(
    *,
    phase: str,
    user_message: str,
    conversation_context: dict[str, object] | None,
    memory_context: list[dict[str, object]] | str | None,
    instructions: str,
    stage_data: dict[str, Any],
    output_contract: dict[str, Any] | str,
) -> dict[str, Any]:
    metadata = (
        conversation_context.get("metadata", {})
        if isinstance(conversation_context, dict)
        else {}
    )
    turn_time = metadata.get("current_turn_time") if isinstance(metadata, dict) else None
    memory_text = ""
    if phase in {"Router", "TurnPlanner"}:
        memory_text = (
            _memory_text(memory_context)
            if isinstance(memory_context, list)
            else str(memory_context or "").strip()
        )
    return {
        STAGE_PROTOCOL_KEY: {
            "phase": phase,
            "instructions": instructions.strip(),
            "output_contract": output_contract,
            "memory": memory_text,
            "turn_time": str(turn_time or "未提供"),
        },
        "user_message": user_message,
        "conversation_context": (
            conversation_context if isinstance(conversation_context, dict) else {}
        ),
        **stage_data,
    }


def render_stage_user_message(
    user_payload: dict[str, Any], *, include_turn_header: bool = True
) -> str:
    """Render the exact stage input sent as the current user message."""
    payload = copy.deepcopy(
        {
            key: value
            for key, value in user_payload.items()
            if key != "conversation_context"
        }
    )
    stage = payload.pop(STAGE_PROTOCOL_KEY, {})
    user_message = str(payload.pop("user_message", "") or "").strip()
    projected = _drop_empty_values(payload)
    output_contract = stage.get("output_contract") if isinstance(stage, dict) else None
    if not isinstance(output_contract, str):
        output_contract = json.dumps(
            output_contract or {}, ensure_ascii=False, separators=(",", ":")
        )
    sections = []
    if include_turn_header:
        sections.extend(
            [
                f"用户记忆：\n{stage.get('memory') or '无'}",
                f"本轮时间：\n{stage.get('turn_time') or '未提供'}",
                f"本轮用户输入：\n{user_message or '（空）'}",
            ]
        )
    sections.extend(
        [
            f"当前阶段：\n{stage.get('phase') or '未指定'}",
            (
                "思考要求：\n保留完成当前阶段所需的简短思考；不要复述上下文、逐字段展开检查、"
                "罗列无关备选方案或反复验证已明确的信息。得到可靠结论后立即按输出约束作答。"
            ),
            f"阶段规则：\n{str(stage.get('instructions') or '').strip()}",
            "当前阶段独有内容：\n"
            + json.dumps(projected, ensure_ascii=False, separators=(",", ":")),
            f"输出约束：\n{output_contract}",
        ]
    )
    return "\n\n".join(sections)


def _memory_text(items: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split())
        if not content or content in lines:
            continue
        lines.append(content)
    return "\n".join(f"- {line}" for line in lines)


def _drop_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {key: _drop_empty_values(item) for key, item in value.items()}
        return {
            key: item
            for key, item in projected.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            projected
            for item in value
            if (projected := _drop_empty_values(item)) not in (None, "", [], {})
        ]
    return value
