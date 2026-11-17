"""Durable, task-scoped stop and web handoff after execution failure.

The model cannot choose a replacement executor. Background receipt tracking is
independent of this gate and continues while a human investigates.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session, select

from app.db.models import AgentEvent, ChatSession, HumanHandoffRequest
from staffdeck_harness.contracts.errors import ModuleSdkError


def blocked_handoff(db: Session, session_id: str, task_id: str | None):
    rows = db.exec(select(HumanHandoffRequest).where(
        HumanHandoffRequest.session_id == session_id,
        HumanHandoffRequest.status.in_(["pending", "assigned"]),
    )).all()
    return next((row for row in rows
                 if (row.metadata_json or {}).get("execution_failure")
                 and (row.metadata_json or {}).get("task_frame_id") == task_id), None)


def stop_execution(db: Session, session: ChatSession, task_id: str | None,
                   *, operation: str, reason: str, outcome_unknown: bool = False):
    existing = blocked_handoff(db, session.id, task_id)
    if existing is not None:
        return existing
    from staffdeck_harness.runtime.staff_ownership import (
        handoff_staff,
        staff_owner_id,
        tenant_admin_id,
    )

    # Persist the stop before resolving ownership. A directory outage must never
    # allow the next model call to fall back to another executor.
    question = ("任务执行结果尚未确认，保留原任务追踪，禁止重发或切换执行方式。"
                if outcome_unknown else "任务调用失败，已停止执行，禁止自行切换执行方式。")
    question += "请负责人核查后通过人工接管处理。"
    row = HumanHandoffRequest(
        tenant_id=session.tenant_id, session_id=session.id, agent_id=session.agent_id,
        requester_user_id=session.user_id, pending_question=question,
        context_summary=f"失败操作：{operation}\n原因：{reason[:600]}",
        trigger_skill_id=session.active_skill_id, trigger_step_id=session.active_step_id,
        resume_payload_json={"active_skill_id": session.active_skill_id,
                             "active_step_id": session.active_step_id,
                             "slots": dict(session.slots_json or {}),
                             "pending_tasks": list(session.pending_tasks_json or []),
                             "channel": session.channel,
                             "channel_binding_id": session.channel_binding_id,
                             "channel_account_key": session.channel_account_key,
                             "channel_target": dict(session.channel_target_json or {})},
        metadata_json={"execution_failure": True, "task_frame_id": task_id,
                       "operation": operation, "outcome_unknown": outcome_unknown,
                       "assignee_notify_channel": "web"},
    )
    db.add(row)
    session.status = "handoff"
    session.awaiting_input_json = {"type": "human_handoff", "handoff_id": row.id,
                                   "pending_question": question}
    db.add(session)
    db.flush()
    # A valid owner wins; only an absent owner falls back to the tenant admin.
    # An unavailable identity source remains an explicit routing error.
    try:
        staff = handoff_staff(db, session.tenant_id, session.agent_id)
        row.assignee_user_id = (staff_owner_id(db, session.tenant_id, staff)
                                or tenant_admin_id(db, session.tenant_id))
        if not row.assignee_user_id:
            raise ValueError("未找到有效的数字员工负责人或 StaffDeck 管理员")
    except (ModuleSdkError, HTTPException, ValueError) as exc:
        row.metadata_json = {**row.metadata_json, "routing_error": str(exc)[:600]}
        row.pending_question += "接管通知未送达：负责人或管理员身份无法确认，请管理员检查站内接管记录。"
        session.awaiting_input_json = {**session.awaiting_input_json,
                                      "pending_question": row.pending_question}
        db.add(AgentEvent(tenant_id=session.tenant_id, session_id=session.id,
                         event_type="human_handoff_notify_failed",
                         payload_json={"handoff_id": row.id, "error": str(exc)[:600]}))
    db.add(row)
    db.add(AgentEvent(tenant_id=session.tenant_id, session_id=session.id,
                     event_type="human_handoff_requested",
                     payload_json={"handoff_id": row.id,
                                   "assignee_user_id": row.assignee_user_id,
                                   "pending_question": question}))
    if row.assignee_user_id:
        from staffdeck_harness.handoff.core import WebInboxNotifier
        WebInboxNotifier().notify(db, row, pending_question=question,
                                 context_summary=row.context_summary)
    db.flush()
    return row


def escalate_external_failure(db, task):
    if task.status in {"failed", "expired", "outcome_unknown"}:
        owner = db.get(ChatSession, task.session_id) if task.session_id else None
        if owner is not None:
            if (owner.tenant_id, owner.user_id, owner.agent_id) != (
                task.tenant_id, task.user_id, task.agent_id
            ):
                raise ValueError("External task failure does not belong to its chat")
            stop_execution(db, owner, task.task_frame_id, operation="external_task",
                           reason=str((task.error_json or {}).get("message") or task.status),
                           outcome_unknown=task.status != "failed")
