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
    if result.status != "completed":
        return result
    from app.session.slot_policy import missing_step_slots

    missing = missing_step_slots(requirement, result.slot_updates, session.slots_json or {})
    if not missing:
        return result
    result.status = "awaiting_user"
    # Do not retain a model's "submitted/completed" claim after rejecting the completion.
    result.reply_fragment = "当前步骤尚未完成，还需要您补充：" + "、".join(missing) + "。"
    result.error = {"code": "REQUIRED_SLOT_MISSING", "message": result.reply_fragment,
                    "details": {"missing_slots": missing}}
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
