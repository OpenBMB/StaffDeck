from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.api.chat import message_read, session_read
from app.db import get_session
from app.db.models import AgentEvent, AgentProfile, ChatSession, Message, User, utc_now
from app.security.auth import get_current_user
from app.security.permissions import agent_owned_by_user, is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/sessions", tags=["enterprise:sessions"])


@router.get("")
def list_sessions(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    conditions = [ChatSession.tenant_id == tenant_id]
    view_all = False
    if agent_id:
        conditions.append(ChatSession.agent_id == agent_id)
        view_all = _can_view_all_agent_sessions(db, tenant_id, agent_id, current_user)
    if not view_all:
        conditions.append(ChatSession.user_id == current_user.id)
    rows = db.exec(
        select(ChatSession).where(*conditions).order_by(ChatSession.updated_at.desc())
    ).all()
    return _session_payloads(db, rows)


@router.get("/{session_id}")
def get_session_detail(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_visible_chat_session(db, tenant_id, session_id, current_user)
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    return {
        "session": _session_payloads(db, [row])[0],
        "messages": [message_read(message).model_dump() for message in messages],
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "payload": event.payload_json,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
    }


@router.post("/{session_id}/reset")
def reset_session(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_visible_chat_session(db, tenant_id, session_id, current_user)
    row.active_skill_id = None
    row.active_step_id = None
    row.slots_json = {}
    row.skill_stack_json = []
    row.pending_tasks_json = []
    row.resume_after_answer_json = None
    row.summary = None
    row.last_agent_question = None
    row.status = "active"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _session_payloads(db, [row])[0]


def _can_view_all_agent_sessions(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> bool:
    if is_admin_user(current_user):
        return True
    if not agent_id:
        return False
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        # is_overall 员工只有 admin 可看全部，创建者永不匹配
        return False
    return agent_owned_by_user(agent, current_user)


def _get_visible_chat_session(
    db: Session,
    tenant_id: str,
    session_id: str,
    current_user: User,
) -> ChatSession:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if row.user_id == current_user.id or is_admin_user(current_user):
        return row
    agent = db.get(AgentProfile, row.agent_id) if row.agent_id else None
    if (
        agent
        and agent.tenant_id == tenant_id
        and not agent.is_overall
        and agent_owned_by_user(agent, current_user)
    ):
        return row
    raise HTTPException(status_code=404, detail="Session not found")


def _session_payloads(db: Session, rows: list[ChatSession]) -> list[dict]:
    """session_read 共享函数不动，这里 dump 后 augment 渠道与对话人展示字段。"""
    user_ids = {row.user_id for row in rows if row.user_id}
    users = (
        {row.id: row for row in db.exec(select(User).where(User.id.in_(user_ids))).all()}
        if user_ids
        else {}
    )
    payloads: list[dict] = []
    for row in rows:
        user = users.get(row.user_id)
        payloads.append(
            {
                **session_read(row).model_dump(),
                "channel": row.channel,
                "session_username": user.username if user else None,
                "session_display_name": user.display_name if user else None,
            }
        )
    return payloads


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
