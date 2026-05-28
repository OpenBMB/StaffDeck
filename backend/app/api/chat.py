from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.core import AgentLoop
from app.db import engine, get_session
from app.db.models import AgentEvent, ChatSession, Message, MessageFeedback, Skill, User, new_id, utc_now
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant
from app.session.session_schema import (
    ChatSessionCreateRequest,
    ChatSessionRead,
    ChatSessionUpdateRequest,
    ChatTurnRequest,
    ChatTurnResponse,
    MessageFeedbackRequest,
    MessageRead,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def session_read(row: ChatSession) -> ChatSessionRead:
    return ChatSessionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        title=row.title,
        active_skill_id=row.active_skill_id,
        active_step_id=row.active_step_id,
        status=row.status,
        summary=row.summary,
        last_agent_question=row.last_agent_question,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def message_read(row: Message, feedback_rating: str | None = None) -> MessageRead:
    return MessageRead(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        created_at=row.created_at.isoformat(),
        feedback_rating=feedback_rating,
    )


@router.post("/turn", response_model=ChatTurnResponse)
def chat_turn(
    request: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatTurnResponse:
    _ensure_request_tenant(request.tenant_id, current_user)
    request = request.model_copy(update={"user_id": current_user.id})
    if request.session_id:
        _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
    ensure_tenant(db, request.tenant_id)
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return AgentLoop(db).handle_turn(request)


@router.post("/stream")
def chat_stream(
    request: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    _ensure_request_tenant(request.tenant_id, current_user)
    request = request.model_copy(update={"user_id": current_user.id})
    ensure_tenant(db, request.tenant_id)
    if request.session_id:
        _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    def stream_events() -> Iterator[str]:
        with Session(engine) as db:
            ensure_tenant(db, request.tenant_id)
            for item in AgentLoop(db).handle_turn_stream(request):
                yield _sse(item["event"], item["data"])

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _sse(event: object, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/sessions", response_model=ChatSessionRead)
def create_chat_session(
    request: ChatSessionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatSessionRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    title = _normalize_title(request.title)
    row = ChatSession(
        id=new_id("session"),
        tenant_id=request.tenant_id,
        user_id=current_user.id,
        title=title,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return session_read(row)


@router.get("/sessions", response_model=list[ChatSessionRead])
def list_chat_sessions(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChatSessionRead]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    return [session_read(row) for row in rows]


@router.put("/sessions/{session_id}", response_model=ChatSessionRead)
def rename_chat_session(
    session_id: str,
    request: ChatSessionUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatSessionRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    row = _get_user_chat_session(db, request.tenant_id, current_user.id, session_id)
    row.title = _normalize_title(request.title)
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return session_read(row)


@router.delete("/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_user_chat_session(db, tenant_id, current_user.id, session_id)
    messages = db.exec(
        select(Message).where(Message.tenant_id == tenant_id, Message.session_id == session_id)
    ).all()
    events = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
    ).all()
    feedback_rows = db.exec(
        select(MessageFeedback).where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.session_id == session_id)
    ).all()
    for message in messages:
        db.delete(message)
    for event in events:
        db.delete(event)
    for feedback in feedback_rows:
        db.delete(feedback)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/messages", response_model=list[MessageRead])
def list_chat_messages(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[MessageRead]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    chat_session = db.get(ChatSession, session_id)
    if not chat_session or chat_session.tenant_id != tenant_id or chat_session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    feedback_by_message = _feedback_by_message(db, tenant_id, current_user.id, [row.id for row in rows])
    return [message_read(row, feedback_by_message.get(row.id)) for row in rows]


@router.post("/messages/{message_id}/feedback")
def upsert_message_feedback(
    message_id: str,
    request: MessageFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(request.tenant_id, current_user)
    message_row = _get_feedback_target_message(db, request.tenant_id, current_user.id, message_id)
    existing = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == request.tenant_id,
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
    ).first()
    now = utc_now()
    if existing:
        existing.rating = request.rating
        existing.updated_at = now
        row = existing
    else:
        row = MessageFeedback(
            tenant_id=request.tenant_id,
            session_id=message_row.session_id,
            message_id=message_row.id,
            user_id=current_user.id,
            rating=request.rating,
            created_at=now,
            updated_at=now,
        )
    db.add(row)
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=message_row.session_id,
            event_type="message_feedback_changed",
            payload_json={"message_id": message_row.id, "rating": request.rating, "user_id": current_user.id},
        )
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "message_id": row.message_id,
        "rating": row.rating,
        "updated_at": row.updated_at.isoformat(),
    }


@router.delete("/messages/{message_id}/feedback")
def delete_message_feedback(
    message_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    _ensure_request_tenant(tenant_id, current_user)
    message_row = _get_feedback_target_message(db, tenant_id, current_user.id, message_id)
    existing = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == tenant_id,
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
    ).first()
    if existing:
        db.delete(existing)
        db.add(
            AgentEvent(
                tenant_id=tenant_id,
                session_id=message_row.session_id,
                event_type="message_feedback_changed",
                payload_json={"message_id": message_row.id, "rating": None, "user_id": current_user.id},
            )
        )
        db.commit()
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/trace")
def list_chat_session_trace(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    _ensure_request_tenant(tenant_id, current_user)
    _get_user_chat_session(db, tenant_id, current_user.id, session_id)
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
    skills = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    skill_names = {skill.skill_id: skill.name for skill in skills}
    return _build_turn_traces(messages, events, skill_names)


def _get_user_chat_session(db: Session, tenant_id: str, user_id: str, session_id: str) -> ChatSession:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if not row or row.tenant_id != tenant_id or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def _ensure_chat_session_available(db: Session, tenant_id: str, user_id: str, session_id: str) -> None:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if row and (row.tenant_id != tenant_id or row.user_id != user_id):
        raise HTTPException(status_code=404, detail="Session not found")


def _get_feedback_target_message(db: Session, tenant_id: str, user_id: str, message_id: str) -> Message:
    ensure_tenant(db, tenant_id)
    row = db.get(Message, message_id)
    if not row or row.tenant_id != tenant_id or row.role != "assistant":
        raise HTTPException(status_code=404, detail="Message not found")
    chat_session = db.get(ChatSession, row.session_id)
    if not chat_session or chat_session.tenant_id != tenant_id or chat_session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Message not found")
    return row


def _feedback_by_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_ids: list[str],
) -> dict[str, str]:
    if not message_ids:
        return {}
    rows = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == tenant_id,
            MessageFeedback.user_id == user_id,
            MessageFeedback.message_id.in_(message_ids),  # type: ignore[attr-defined]
        )
    ).all()
    return {row.message_id: row.rating for row in rows}


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _normalize_title(value: str | None) -> str | None:
    if value is None:
        return None
    title = value.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Session title cannot be empty")
    return title[:80]


def _build_turn_traces(
    messages: list[Message],
    events: list[AgentEvent],
    skill_names: dict[str, str],
) -> list[dict]:
    user_messages = [message for message in messages if message.role == "user"]
    traces: list[dict] = []
    current: dict | None = None
    user_index = 0

    for event in events:
        if event.event_type == "user_message_received":
            if current:
                _finish_trace_if_needed(current, event.created_at)
                traces.append(current)
            user_message = _matching_user_message(user_messages, user_index, event.payload_json)
            if user_message:
                user_index = user_messages.index(user_message) + 1
            current = {
                "turn_id": user_message.id if user_message else event.id,
                "user_message_id": user_message.id if user_message else None,
                "started_at": event.created_at.isoformat(),
                "completed_at": None,
                "lines": [
                    {
                        "id": "thinking",
                        "kind": "thinking",
                        "text": "已完成思考",
                        "state": "completed",
                    }
                ],
            }
            continue

        if not current:
            continue

        line = _event_trace_line(event, skill_names)
        if line:
            _upsert_trace_line(current["lines"], line)
        if event.event_type == "assistant_message_created":
            current["completed_at"] = event.created_at.isoformat()
            _complete_trace_lines(current["lines"])

    if current:
        _finish_trace_if_needed(current, events[-1].created_at if events else None)
        traces.append(current)

    return traces


def _matching_user_message(
    user_messages: list[Message],
    start_index: int,
    payload: dict,
) -> Message | None:
    text = str(payload.get("message") or "")
    for index in range(start_index, len(user_messages)):
        if user_messages[index].content == text:
            return user_messages[index]
    if start_index < len(user_messages):
        return user_messages[start_index]
    return None


def _event_trace_line(event: AgentEvent, skill_names: dict[str, str]) -> dict | None:
    payload = event.payload_json or {}
    if event.event_type == "router_decision_created":
        intent = str(payload.get("user_intent") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": f"decision_{event.id}",
            "kind": "decision",
            "text": f"判断意图 {intent}" if intent else "完成技能判断",
            "detail": reason or None,
            "state": "completed",
        }
    if event.event_type in {"skill_started", "skill_suspended", "skill_resumed", "skill_step_changed"}:
        to_skill_id = str(payload.get("to_skill_id") or "")
        from_skill_id = str(payload.get("from_skill_id") or "")
        skill_id = to_skill_id or from_skill_id
        if not skill_id:
            return None
        label = {
            "skill_started": "选择技能",
            "skill_suspended": "切换技能",
            "skill_resumed": "恢复技能",
            "skill_step_changed": "推进技能",
        }[event.event_type]
        detail_parts = []
        if from_skill_id and from_skill_id != to_skill_id:
            detail_parts.append(f"from {skill_names.get(from_skill_id, from_skill_id)}")
        if payload.get("to_step_id"):
            detail_parts.append(f"step {payload['to_step_id']}")
        return {
            "id": f"skill_{event.id}",
            "kind": "skill",
            "text": f"{label} {skill_names.get(skill_id, skill_id)}",
            "detail": " · ".join(detail_parts) or None,
            "state": "completed",
        }
    if event.event_type == "skill_completed":
        skill_id = str(payload.get("skill_id") or "")
        return {
            "id": f"skill_{event.id}",
            "kind": "skill",
            "text": f"完成技能 {skill_names.get(skill_id, skill_id)}" if skill_id else "完成技能",
            "detail": str(payload.get("reason") or "") or None,
            "state": "completed",
        }
    if event.event_type == "tool_call_started":
        name = str(payload.get("name") or "")
        if not name:
            return None
        return {
            "id": f"tool_{name}",
            "kind": "tool",
            "text": f"调用工具 {name}",
            "detail": None,
            "state": "running",
        }
    if event.event_type == "tool_call_finished":
        name = str(payload.get("tool_name") or "")
        success = bool(payload.get("success"))
        return {
            "id": f"tool_{name or event.id}",
            "kind": "tool",
            "text": f"{'调用工具' if success else '工具调用失败'} {name}",
            "detail": _tool_trace_detail(payload),
            "state": "completed" if success else "failed",
        }
    if event.event_type == "reflection_decision_created":
        needs_retry = bool(payload.get("needs_retry"))
        return {
            "id": f"decision_{event.id}",
            "kind": "decision",
            "text": "反思后继续尝试" if needs_retry else "反思通过",
            "detail": _reflection_trace_detail(payload),
            "state": "completed",
        }
    if event.event_type == "reflection_retry_started":
        mode = str(payload.get("mode") or "").strip()
        target_tool = str(payload.get("target_tool_name") or "").strip()
        target_skill = str(payload.get("target_skill_id") or "").strip()
        target = target_tool or skill_names.get(target_skill, target_skill)
        return {
            "id": f"decision_{event.id}",
            "kind": "decision",
            "text": f"重试{ '工具' if mode == 'tool' else '技能' } {target}".strip(),
            "detail": str(payload.get("reason") or "") or None,
            "state": "completed",
        }
    if event.event_type == "error_occurred":
        return {
            "id": f"error_{event.id}",
            "kind": "thinking",
            "text": "思考遇到问题",
            "detail": str(payload.get("message") or "") or None,
            "state": "failed",
        }
    return None


def _tool_trace_detail(payload: dict) -> str | None:
    data = payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    parts = [
        str(data_dict.get("source") or "").strip(),
        "未命中" if data_dict.get("found") is False else "已命中" if data_dict.get("found") is True else "",
        str(data_dict.get("miss_reason") or "").strip(),
        str(data_dict.get("recommendation") or "").strip(),
    ]
    text = " · ".join(part for part in parts if part)
    return text or None


def _reflection_trace_detail(payload: dict) -> str | None:
    parts = [
        str(payload.get("reason") or "").strip(),
        f"工具 {payload['target_tool_name']}" if payload.get("target_tool_name") else "",
        f"技能 {payload['target_skill_id']}" if payload.get("target_skill_id") else "",
        f"步骤 {payload['target_step_id']}" if payload.get("target_step_id") else "",
    ]
    text = " · ".join(part for part in parts if part)
    return text or None


def _upsert_trace_line(lines: list[dict], line: dict) -> None:
    for index, item in enumerate(lines):
        if item.get("id") == line.get("id"):
            lines[index] = line
            return
    lines.append(line)


def _complete_trace_lines(lines: list[dict]) -> None:
    for line in lines:
        if line.get("state") == "running":
            line["state"] = "completed"
    thinking = next((line for line in lines if line.get("id") == "thinking"), None)
    if thinking:
        thinking["text"] = "已完成思考"
        thinking["state"] = "completed"


def _finish_trace_if_needed(trace: dict, fallback_time) -> None:
    if not trace.get("completed_at") and fallback_time:
        trace["completed_at"] = fallback_time.isoformat()
    _complete_trace_lines(trace["lines"])
