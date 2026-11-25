"""Handoff for opt-in SOP writes whose provider outcome cannot be confirmed."""
from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlmodel import Session, select

from app.db.models import AgentEvent, ChatSession, HumanHandoffRequest
from staffdeck_harness.contracts.errors import ModuleSdkError

if TYPE_CHECKING:
    from staffdeck_harness.capabilities.host import ActivationSlot
    from staffdeck_harness.contracts.invocation import ModuleInvocation

HANDOFF_CODE = "EXECUTION_REQUIRES_HUMAN"
NOT_SENT_CODE = "GUARDED_HTTP_NOT_SENT"
WRITE_OPERATIONS = frozenset({"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"})


def protected_write(slot: ActivationSlot, inv: ModuleInvocation) -> bool:
    if not inv.side_effecting or inv.operation not in WRITE_OPERATIONS or not inv.context.task_frame_id:
        return False
    sop = slot.snapshot.sop(slot.active_sop_id) if slot.active_sop_id else None
    return bool(sop and (sop.content.get("interruption_policy") or {}).get("write_tool_failure") == "handoff")


def pending_execution_handoff(
    db: Session, session: ChatSession, task_id: str | None = None, *, sop_id: str | None = None,
) -> HumanHandoffRequest | None:
    rows = db.exec(select(HumanHandoffRequest).where(
        HumanHandoffRequest.tenant_id == session.tenant_id,
        HumanHandoffRequest.session_id == session.id,
        HumanHandoffRequest.agent_id == session.agent_id,
        HumanHandoffRequest.status.in_(["pending", "assigned"]),
    )).all()
    return next((row for row in rows if (row.metadata_json or {}).get("execution_failure")
                 and ((task_id is not None and (row.metadata_json or {}).get("task_frame_id") == task_id)
                      or (sop_id is not None and row.trigger_skill_id == sop_id))), None)


def stop_execution(
    db: Session, session: ChatSession, task_id: str, *, operation: str,
    sop_id: str | None, node_id: str | None = None,
    receipt_id: str | None = None, external_task_id: str | None = None,
) -> HumanHandoffRequest:
    from sqlalchemy import update
    from staffdeck_harness.handoff.core import WebInboxNotifier
    from staffdeck_harness.runtime.staff_ownership import handoff_staff, staff_owner_id, tenant_admin_id

    # Serialize stops for this session with competing callbacks and status polls.
    db.exec(update(ChatSession).where(
        ChatSession.id == session.id, ChatSession.tenant_id == session.tenant_id,
    ).values(id=ChatSession.id).execution_options(synchronize_session=False))
    key = sha256(f"{session.tenant_id}:{session.id}:{task_id}".encode()).hexdigest()[:24]
    handoff_id = f"handoff_write_{key}"
    existing = db.get(HumanHandoffRequest, handoff_id)
    if existing is not None:
        return existing

    question = "工具可能已执行业务操作。已暂停当前流程，请负责人核对原操作的结果。"
    row = HumanHandoffRequest(
        id=handoff_id, tenant_id=session.tenant_id, session_id=session.id, agent_id=session.agent_id,
        requester_user_id=session.user_id, trigger_skill_id=sop_id, trigger_step_id=node_id,
        pending_question=question, context_summary=f"需要核对的操作 {operation}",
        metadata_json={
            "execution_failure": True, "outcome_unknown": True, "task_frame_id": task_id,
            "operation": operation, "receipt_id": receipt_id, "external_task_id": external_task_id,
            "assignee_notify_channel": "web",
        },
    )
    try:
        staff = handoff_staff(db, session.tenant_id, session.agent_id)
        row.assignee_user_id = staff_owner_id(db, session.tenant_id, staff) or tenant_admin_id(
            db, session.tenant_id,
        )
    except (ModuleSdkError, HTTPException, ValueError):
        row.metadata_json = {**row.metadata_json, "routing_error": "staff_directory_unavailable"}

    if not row.assignee_user_id:
        row.pending_question += "负责人暂无法确认，请管理员查看站内接管记录。"
    session.status = "handoff"
    session.awaiting_input_json = {
        "type": "human_handoff", "handoff_id": row.id, "pending_question": row.pending_question,
    }
    db.add_all([row, session, AgentEvent(
        tenant_id=session.tenant_id, session_id=session.id, event_type="human_handoff_requested",
        payload_json={"handoff_id": row.id, "assignee_user_id": row.assignee_user_id,
                      "pending_question": row.pending_question},
    )])
    if row.assignee_user_id:
        WebInboxNotifier().notify(db, row, pending_question=question, context_summary=row.context_summary)
    else:
        db.add(AgentEvent(
            tenant_id=session.tenant_id, session_id=session.id, event_type="human_handoff_notify_failed",
            payload_json={"handoff_id": row.id, "error": "staff_directory_unavailable"},
        ))
    db.flush()
    return row
