"""Result contracts between the execution engine and SOP lifecycle. No engine imports."""

from __future__ import annotations
from typing import Any
from app.core.task_request_compiler import TaskExecutionResult
from app.db.models import ChatSession
from app.session.session_schema import StepAgentResult


def step_result(result: TaskExecutionResult) -> StepAgentResult:
    action = {
        "completed": "advance",
        "awaiting_user": "ask_user",
        "handoff": "handoff",
        "failed": "reply",
        "blocked": "reply",
        "action_budget": "reply",
    }.get(result.status, "reply")
    return StepAgentResult(
        action=action,  # type: ignore[arg-type]
        reply=result.reply_fragment,
        slot_updates=dict(result.slot_updates),
        knowledge_results=list(result.evidence_results),
        next_step_id=result.next_step_id,
        is_step_completed=result.status == "completed",
        handoff=result.status == "handoff",
        structured_result=result.structured_result,
    )


def enforce_required_slots(
    result: TaskExecutionResult,
    requirement: Any,
    session: ChatSession,
) -> TaskExecutionResult:
    if result.status != "completed" or not requirement.required_slots:
        return result
    merged = {
        **dict(session.slots_json or {}),
        **dict(result.slot_updates or {}),
    }
    missing = [
        field for field in requirement.required_slots if merged.get(field) in (None, "", [], {})
    ]
    if not missing:
        return result
    result.status = "awaiting_user"
    if not result.reply_fragment:
        result.reply_fragment = "还需要您补充：" + "、".join(missing) + "。"
    result.next_step_id = None
    return result


def append_session_handoff_artifact(
    result: TaskExecutionResult,
    session: ChatSession,
) -> None:
    awaiting = session.awaiting_input_json if isinstance(session.awaiting_input_json, dict) else {}
    handoff_id = str(awaiting.get("handoff_id") or "").strip()
    if not handoff_id:
        return
    result.artifacts.append(
        {
            "type": "human_handoff",
            "handoff_id": handoff_id,
        }
    )
