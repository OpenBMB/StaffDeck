from __future__ import annotations

import json
import queue
import threading
import time
from datetime import timedelta
from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, select

from app.agents.branching import model_for_agent
from app.core import AgentLoop
from app.core.cancellation import cancel_chat_turn
from app.db import engine, get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    HumanHandoffRequest,
    Message,
    MessageFeedback,
    Skill,
    SkillFeedback,
    User,
    new_id,
    utc_now,
)
from app.feedback import enqueue_feedback_analysis
from app.llm import LLMClient, LLMError
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant
from app.scheduled_tasks.schema import ScheduledTaskDraftRead
from app.scheduled_tasks.service import detect_scheduled_task_draft
from app.session.attachments import parse_chat_attachment
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatAttachmentRead,
    ChatSessionCreateRequest,
    ChatSessionRead,
    ChatSessionUpdateRequest,
    ChatTurnRequest,
    ChatTurnResponse,
    MessageFeedbackRequest,
    MessageRead,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])
CANCELLED_ASSISTANT_REPLY = "已停止生成"
CHAT_ADMIN_USERNAMES = {"admin", "admin_demo"}
STREAM_REPLY_CHUNK_SIZE = 96
MAX_CHAT_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_CHAT_ATTACHMENTS = 8
SESSION_TITLE_SUMMARY_EVENT = "session_title_summarized"
EVENT_PAYLOAD_META_KEYS = {"id", "event", "type", "event_type", "created_at", "data"}
SESSION_TITLE_PROMPT = """你是任务派发台的会话标题编辑器。

根据首轮用户需求和员工回复，生成一个简短、可读、具体的中文标题。

要求：
- 输出 JSON object，格式为 {"title": "..."}。
- 标题 4 到 18 个中文字符优先，最多 24 个字符。
- 不要使用“新任务”“任务记录”“用户咨询”等空泛标题。
- 不要包含标点符号、引号、编号、员工名或用户称呼。
- 如果无法判断，就返回最能概括用户需求的短语。
"""
_session_title_summary_jobs: set[str] = set()
_session_title_summary_jobs_lock = threading.Lock()


class HumanHandoffRead(BaseModel):
    id: str
    tenant_id: str
    session_id: str
    agent_id: str | None = None
    requester_user_id: str | None = None
    assignee_user_id: str | None = None
    trigger_skill_id: str | None = None
    trigger_step_id: str | None = None
    context_summary: str | None = None
    pending_question: str | None = None
    status: str
    human_reply: str | None = None
    metadata: dict[str, object]
    created_at: str
    updated_at: str
    answered_at: str | None = None


class ChatTurnCancelRequest(BaseModel):
    tenant_id: str
    turn_id: str


class HumanHandoffReplyRequest(BaseModel):
    tenant_id: str
    reply: str


def session_read(row: ChatSession) -> ChatSessionRead:
    return ChatSessionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_id=row.agent_id,
        title=row.title,
        active_skill_id=row.active_skill_id,
        active_step_id=row.active_step_id,
        status=row.status,
        summary=row.summary,
        last_agent_question=row.last_agent_question,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def message_read(
    row: Message,
    feedback_rating: str | None = None,
    turn_id: str | None = None,
) -> MessageRead:
    metadata = row.metadata_json or {}
    metadata_turn_id = str(metadata.get("turn_id") or metadata.get("user_message_id") or "").strip()
    return MessageRead(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        metadata=metadata,
        turn_id=turn_id or metadata_turn_id or None,
        created_at=row.created_at.isoformat(),
        feedback_rating=feedback_rating,
    )


def human_handoff_read(row: HumanHandoffRequest) -> HumanHandoffRead:
    return HumanHandoffRead(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        requester_user_id=row.requester_user_id,
        assignee_user_id=row.assignee_user_id,
        trigger_skill_id=row.trigger_skill_id,
        trigger_step_id=row.trigger_step_id,
        context_summary=row.context_summary,
        pending_question=row.pending_question,
        status=row.status,
        human_reply=row.human_reply,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        answered_at=row.answered_at.isoformat() if row.answered_at else None,
    )


def _user_message_metadata(request: ChatTurnRequest) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if request.client_turn_id:
        metadata["client_turn_id"] = request.client_turn_id
    if request.interaction_mode == "scheduled_task":
        metadata["interaction_mode"] = "scheduled_task"
    if request.model_config_id:
        metadata["model_config_id"] = request.model_config_id
    if request.attachments:
        metadata["attachments"] = [item.model_dump(mode="json") for item in request.attachments]
    return metadata


def _schedule_session_title_summary(
    tenant_id: str,
    user_id: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    if not session_id:
        return
    job_key = f"{tenant_id}:{user_id}:{session_id}"
    with _session_title_summary_jobs_lock:
        if job_key in _session_title_summary_jobs:
            return
        _session_title_summary_jobs.add(job_key)

    def run() -> None:
        try:
            _summarize_session_title_once(tenant_id, user_id, session_id, agent_id)
        finally:
            with _session_title_summary_jobs_lock:
                _session_title_summary_jobs.discard(job_key)

    thread = threading.Thread(
        target=run,
        daemon=True,
    )
    thread.start()


def _summarize_session_title_once(
    tenant_id: str,
    user_id: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    try:
        for attempt in range(8):
            messages: list[Message] = []
            model_config = None
            effective_agent_id = agent_id
            with Session(engine) as db:
                session = db.exec(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.user_id == user_id,
                    )
                ).first()
                if not session:
                    return
                if (session.title or "").strip():
                    return
                existing = db.exec(
                    select(AgentEvent).where(
                        AgentEvent.tenant_id == tenant_id,
                        AgentEvent.session_id == session_id,
                        AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
                    )
                ).first()
                if existing:
                    return
                messages = db.exec(
                    select(Message)
                    .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
                    .order_by(Message.created_at)
                    .limit(6)
                ).all()
                if not any(row.role == "user" for row in messages):
                    messages = []
                else:
                    effective_agent_id = agent_id or session.agent_id
                    model_config = model_for_agent(db, tenant_id, effective_agent_id)

            if not messages:
                if attempt < 7:
                    time.sleep(0.25)
                    continue
                return

            payload = {
                "current_title": "",
                "messages": [
                    {"role": row.role, "content": row.content[:1200]}
                    for row in messages
                    if row.role in {"user", "assistant"}
                ],
            }
            title = ""
            title_source = "first_user_fallback"
            if model_config:
                try:
                    raw = LLMClient(model_config).generate_json(SESSION_TITLE_PROMPT, payload)
                    title = _normalize_auto_title(str(raw.get("title") or ""))
                    if title:
                        title_source = "first_turn_summary"
                except LLMError:
                    title = ""
            if not title:
                title = _fallback_session_title(messages)
            if not title:
                return

            with Session(engine) as db:
                session = db.exec(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.user_id == user_id,
                    )
                ).first()
                if not session:
                    return
                if (session.title or "").strip():
                    return
                existing = db.exec(
                    select(AgentEvent).where(
                        AgentEvent.tenant_id == tenant_id,
                        AgentEvent.session_id == session_id,
                        AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
                    )
                ).first()
                if existing:
                    return
                session.title = title
                db.add(session)
                db.add(
                    AgentEvent(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type=SESSION_TITLE_SUMMARY_EVENT,
                        payload_json={
                            "title": title,
                            "source": title_source,
                            "agent_id": effective_agent_id,
                        },
                    )
                )
                db.commit()
                return
    except (LLMError, Exception):
        return


def _session_title_summary_payload(db: Session, tenant_id: str, session_id: str) -> dict[str, str] | None:
    event = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
        )
        .order_by(AgentEvent.created_at.desc())
        .limit(1)
    ).first()
    payload = event.payload_json if event else None
    title = payload.get("title") if isinstance(payload, dict) else None
    if not isinstance(title, str) or not title.strip():
        return None
    return {"sessionId": session_id, "title": title.strip()}


def _normalize_auto_title(value: str) -> str:
    title = value.strip().strip("\"'“”‘’`")
    for token in ("\n", "\r", "\t", "：", ":", "。", "，", ",", "；", ";"):
        title = title.replace(token, " ")
    title = " ".join(part for part in title.split() if part)
    return title[:24]


def _fallback_session_title(messages: list[Message]) -> str:
    first_user = next((row.content for row in messages if row.role == "user" and row.content.strip()), "")
    if not first_user:
        return ""
    return _normalize_auto_title(first_user)


def _normalized_session_event_payload(row: AgentEvent) -> dict[str, object]:
    payload = dict(row.payload_json or {})
    event_name = str(payload.get("event") or payload.get("type") or row.event_type)
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {key: value for key, value in payload.items() if key not in EVENT_PAYLOAD_META_KEYS}
    normalized: dict[str, object] = {
        **payload,
        "id": str(payload.get("id") or row.id),
        "event": event_name,
        "type": str(payload.get("type") or event_name),
        "event_type": str(payload.get("event_type") or event_name),
        "created_at": str(payload.get("created_at") or row.created_at.isoformat()),
        "data": data,
    }
    if "run_id" not in normalized and data.get("run_id"):
        normalized["run_id"] = str(data.get("run_id"))
    return normalized


def _resume_human_handoff_async(handoff_id: str) -> None:
    thread = threading.Thread(target=_resume_human_handoff_worker, args=(handoff_id,), daemon=True)
    thread.start()


def _resume_human_handoff_worker(handoff_id: str) -> None:
    try:
        with Session(engine) as db:
            handoff = db.get(HumanHandoffRequest, handoff_id)
            if not handoff or handoff.status != "answered" or not handoff.human_reply:
                return
            chat_session = db.get(ChatSession, handoff.session_id)
            if not chat_session or chat_session.tenant_id != handoff.tenant_id:
                return
            metadata = dict(handoff.metadata_json or {})
            if metadata.get("resume_started_at"):
                return
            now = utc_now()
            metadata["resume_started_at"] = now.isoformat()
            handoff.metadata_json = metadata
            db.add(handoff)
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_started",
                    payload_json={
                        "handoff_id": handoff.id,
                        "agent_id": handoff.agent_id,
                        "trigger_skill_id": handoff.trigger_skill_id,
                        "trigger_step_id": handoff.trigger_step_id,
                    },
                    created_at=now,
                )
            )
            db.commit()

            request = ChatTurnRequest(
                tenant_id=handoff.tenant_id,
                session_id=handoff.session_id,
                agent_id=handoff.agent_id or chat_session.agent_id,
                user_id=handoff.requester_user_id or chat_session.user_id or "",
                message=handoff.human_reply,
                channel="human_handoff_resume",
                debug=False,
            )
            AgentLoop(db).handle_turn(request)
            metadata = dict(handoff.metadata_json or {})
            metadata["resume_finished_at"] = utc_now().isoformat()
            handoff.metadata_json = metadata
            db.add(handoff)
            db.commit()
    except Exception as exc:
        with Session(engine) as db:
            handoff = db.get(HumanHandoffRequest, handoff_id)
            if not handoff:
                return
            metadata = dict(handoff.metadata_json or {})
            metadata["resume_failed_at"] = utc_now().isoformat()
            metadata["resume_error"] = str(exc)[:300]
            handoff.status = "failed"
            handoff.metadata_json = metadata
            handoff.updated_at = utc_now()
            db.add(handoff)
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_failed",
                    payload_json={"handoff_id": handoff.id, "error": str(exc)[:300]},
                )
            )
            db.commit()


def _maybe_handle_scheduled_task_request(
    db: Session,
    request: ChatTurnRequest,
    chat_session: ChatSession,
) -> tuple[ChatTurnResponse, ScheduledTaskDraftRead] | None:
    if request.interaction_mode != "scheduled_task" or not request.agent_id:
        return None
    draft = detect_scheduled_task_draft(
        db,
        request.tenant_id,
        request.agent_id,
        request.user_id,
        request.message,
        chat_session.id,
    )
    if not draft or not draft.should_create:
        return None

    reply = _scheduled_task_draft_reply(draft)
    now = utc_now()
    intent_time = now + timedelta(microseconds=1)
    parse_time = now + timedelta(microseconds=2)
    draft_status_time = now + timedelta(microseconds=3)
    event_time = now + timedelta(microseconds=4)
    assistant_time = now + timedelta(microseconds=5)
    state_time = now + timedelta(microseconds=6)
    chat_session.updated_at = assistant_time
    chat_session.summary = f"最近回复：{reply[:120]}"
    user_message = Message(
        tenant_id=request.tenant_id,
        session_id=chat_session.id,
        role="user",
        content=request.message,
        metadata_json=_user_message_metadata(request),
        created_at=now,
    )
    db.add(user_message)
    draft_payload = draft.model_dump(mode="json")
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="user_message_received",
            payload_json={
                "message_id": user_message.id,
                "client_turn_id": request.client_turn_id,
                "message": request.message,
                "channel": request.channel,
                "user_id": request.user_id,
            },
            created_at=now,
        )
    )
    _add_stream_status_event(
        db,
        request.tenant_id,
        chat_session.id,
        user_message.id,
        "scheduled_task_intent",
        "识别定时任务需求",
        created_at=intent_time,
    )
    _add_stream_status_event(
        db,
        request.tenant_id,
        chat_session.id,
        user_message.id,
        "scheduled_task_parse",
        "解析执行计划",
        created_at=parse_time,
    )
    _add_stream_status_event(
        db,
        request.tenant_id,
        chat_session.id,
        user_message.id,
        "scheduled_task_draft",
        "生成定时任务草案",
        extra=draft_payload,
        created_at=draft_status_time,
    )
    assistant_message = Message(
        tenant_id=request.tenant_id,
        session_id=chat_session.id,
        role="assistant",
        content=reply,
        metadata_json={
            "scheduled_task_draft": draft_payload,
            "user_message_id": user_message.id,
            "turn_id": user_message.id,
        },
        created_at=assistant_time,
    )
    db.add(assistant_message)
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="scheduled_task_draft_created",
            payload_json={**draft_payload, "user_message_id": user_message.id, "turn_id": user_message.id},
            created_at=event_time,
        )
    )
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="assistant_message_created",
            payload_json={
                "message_id": assistant_message.id,
                "assistant_message_id": assistant_message.id,
                "user_message_id": user_message.id,
                "turn_id": user_message.id,
                "reply": reply,
                "scheduled_task_draft": draft_payload,
            },
            created_at=assistant_time,
        )
    )
    state = public_session(chat_session)
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="session_state_changed",
            payload_json=state.model_dump(),
            created_at=state_time,
        )
    )
    db.commit()
    db.refresh(chat_session)
    response = ChatTurnResponse(
        reply=reply,
        session_id=chat_session.id,
        session_state=public_session(chat_session),
    )
    return response, draft


def _add_stream_status_event(
    db: Session,
    tenant_id: str,
    session_id: str,
    user_message_id: str,
    phase: str,
    text: str,
    *,
    extra: dict | None = None,
    created_at=None,
) -> None:
    payload = {
        "phase": phase,
        "text": text,
        "user_message_id": user_message_id,
        "turn_id": user_message_id,
        **(extra or {}),
    }
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="stream_status",
            payload_json=payload,
            created_at=created_at or utc_now(),
        )
    )


def _scheduled_task_draft_reply(draft: ScheduledTaskDraftRead) -> str:
    lines = [
        "我已按你选择的定时项目整理成自动任务草案。",
        f"任务：{draft.title}",
        f"计划：{_format_draft_schedule(draft)}",
        f"执行内容：{draft.prompt}",
        "确认下方卡片后才会启用；确认前不会创建自动任务。",
    ]
    return "\n".join(lines)


def _format_draft_schedule(draft: ScheduledTaskDraftRead) -> str:
    return _format_scheduled_task_schedule(draft.schedule_type, draft.schedule or {})


def _format_scheduled_task_schedule(schedule_type: object, schedule_value: object) -> str:
    schedule = schedule_value if isinstance(schedule_value, dict) else {}
    schedule_type_text = str(schedule_type or "daily")
    if schedule_type_text == "once":
        return f"一次性 {schedule.get('run_at') or '待确认时间'}"
    if schedule_type_text == "weekly":
        weekdays = schedule.get("weekdays")
        labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        if isinstance(weekdays, list):
            days = "、".join(labels[int(day)] for day in weekdays if str(day).isdigit() and 0 <= int(day) <= 6)
        else:
            days = "周一"
        return f"每周 {days or '周一'} {schedule.get('time') or '09:00'}"
    if schedule_type_text == "monthly":
        return f"每月 {schedule.get('day_of_month') or 1} 号 {schedule.get('time') or '09:00'}"
    return f"每天 {schedule.get('time') or '09:00'}"


def _scheduled_task_trace_detail(payload: dict) -> str | None:
    title = str(payload.get("title") or "").strip()
    schedule = _format_scheduled_task_schedule(payload.get("schedule_type"), payload.get("schedule"))
    detail = " · ".join(part for part in (title, schedule, "等待确认后启用") if part)
    return detail or None


def _scheduled_task_trace_lines(payload: dict, *, state: str = "completed") -> list[dict]:
    schedule = _format_scheduled_task_schedule(payload.get("schedule_type"), payload.get("schedule"))
    return [
        {
            "id": "scheduled_task_intent",
            "kind": "decision",
            "text": "识别定时任务需求",
            "detail": "用户选择了创建定时任务模式",
            "state": "completed",
        },
        {
            "id": "scheduled_task_parse",
            "kind": "decision",
            "text": "解析执行计划",
            "detail": f"计划：{schedule}" if schedule else None,
            "state": "completed",
        },
        {
            "id": "scheduled_task_draft",
            "kind": "decision",
            "text": "生成定时任务草案",
            "detail": _scheduled_task_trace_detail(payload),
            "state": state,
        },
    ]


def _persist_scheduled_task_draft(
    db: Session,
    tenant_id: str,
    session_id: str,
    draft: ScheduledTaskDraftRead,
) -> None:
    if not session_id:
        return
    payload = draft.model_dump(mode="json")
    latest_assistant = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
    ).first()
    if latest_assistant:
        metadata = dict(latest_assistant.metadata_json or {})
        metadata["scheduled_task_draft"] = payload
        latest_assistant.metadata_json = metadata
        db.add(latest_assistant)
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="scheduled_task_draft_created",
            payload_json=payload,
            created_at=utc_now(),
        )
    )
    db.commit()


def _reply_chunks(reply: str) -> Iterator[str]:
    for index in range(0, len(reply), STREAM_REPLY_CHUNK_SIZE):
        yield reply[index : index + STREAM_REPLY_CHUNK_SIZE]


@router.post("/attachments", response_model=list[ChatAttachmentRead])
async def upload_chat_attachments(
    tenant_id: str = Query(...),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChatAttachmentRead]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > MAX_CHAT_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"最多一次上传 {MAX_CHAT_ATTACHMENTS} 个文件")
    parsed: list[ChatAttachmentRead] = []
    for file in files:
        data = await file.read()
        if len(data) > MAX_CHAT_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"{file.filename or '文件'} 超过上传大小限制")
        parsed.append(parse_chat_attachment(file.filename or "uploaded-file", file.content_type, data))
    return parsed


@router.post("/turn", response_model=ChatTurnResponse)
def chat_turn(
    request: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatTurnResponse:
    _ensure_request_tenant(request.tenant_id, current_user)
    request = request.model_copy(update={"user_id": current_user.id})
    if request.session_id:
        chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
        request = _bind_request_to_session_agent(db, request, chat_session, current_user)
    else:
        _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if not request.message.strip() and not request.attachments:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if request.session_id:
        scheduled_response = _maybe_handle_scheduled_task_request(db, request, chat_session)
        if scheduled_response:
            response, _draft = scheduled_response
            _schedule_session_title_summary(request.tenant_id, request.user_id, response.session_id, request.agent_id)
            return response
    response = AgentLoop(db).handle_turn(request)
    _schedule_session_title_summary(request.tenant_id, request.user_id, response.session_id, request.agent_id)
    if request.interaction_mode == "scheduled_task" and request.agent_id:
        draft = detect_scheduled_task_draft(
            db,
            request.tenant_id,
            request.agent_id,
            request.user_id,
            request.message,
            response.session_id,
        )
        if draft and draft.should_create:
            _persist_scheduled_task_draft(db, request.tenant_id, response.session_id, draft)
    return response


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
        chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
        request = _bind_request_to_session_agent(db, request, chat_session, current_user)
    else:
        _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    if not request.message.strip() and not request.attachments:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    event_queue: queue.Queue[str | None] = queue.Queue()

    def enqueue(event: object, data: object) -> None:
        event_queue.put(_sse(event, data))

    def run_stream_worker() -> None:
        try:
            with Session(engine) as worker_db:
                ensure_tenant(worker_db, request.tenant_id)
                if request.session_id:
                    chat_session = _ensure_chat_session_available(
                        worker_db,
                        request.tenant_id,
                        request.user_id,
                        request.session_id,
                    )
                    if request.interaction_mode == "scheduled_task":
                        enqueue("status", {"phase": "scheduled_task_intent", "text": "识别定时任务需求"})
                        enqueue("status", {"phase": "scheduled_task_parse", "text": "解析执行计划"})
                    scheduled_response = _maybe_handle_scheduled_task_request(worker_db, request, chat_session)
                    if scheduled_response:
                        response, draft = scheduled_response
                        enqueue(
                            "status",
                            {
                                "phase": "scheduled_task_draft",
                                "text": "生成定时任务草案",
                                **draft.model_dump(mode="json"),
                            },
                        )
                        enqueue("scheduled_task_draft", draft.model_dump(mode="json"))
                        for chunk in _reply_chunks(response.reply):
                            enqueue("stream_delta", {"content": chunk})
                        enqueue("stream_end", {})
                        enqueue("complete", response.model_dump(mode="json"))
                        _schedule_session_title_summary(
                            request.tenant_id,
                            request.user_id,
                            response.session_id,
                            request.agent_id,
                        )
                        return
                for item in AgentLoop(worker_db).handle_turn_stream(request):
                    enqueue(item["event"], item["data"])
                    if item["event"] == "user_message_received":
                        source_session_id = str(item["data"].get("sessionId") or request.session_id or "")
                        _schedule_session_title_summary(
                            request.tenant_id,
                            request.user_id,
                            source_session_id,
                            request.agent_id,
                        )
                        continue
                    if item["event"] == "complete":
                        source_session_id = str(item["data"].get("sessionId") or request.session_id or "")
                        _schedule_session_title_summary(
                            request.tenant_id,
                            request.user_id,
                            source_session_id,
                            request.agent_id,
                        )
                        if source_session_id:
                            summary_payload = _session_title_summary_payload(worker_db, request.tenant_id, source_session_id)
                            if summary_payload:
                                enqueue(SESSION_TITLE_SUMMARY_EVENT, summary_payload)
                        if request.interaction_mode != "scheduled_task" or not request.agent_id:
                            continue
                        draft = detect_scheduled_task_draft(
                            worker_db,
                            request.tenant_id,
                            request.agent_id,
                            request.user_id,
                            request.message,
                            source_session_id or None,
                        )
                        if draft and draft.should_create:
                            _persist_scheduled_task_draft(worker_db, request.tenant_id, source_session_id, draft)
                            enqueue("scheduled_task_draft", draft.model_dump(mode="json"))
        except Exception as exc:
            enqueue("error", {"message": str(exc)[:300] or "stream worker failed"})
        finally:
            event_queue.put(None)

    threading.Thread(target=run_stream_worker, daemon=True).start()

    def stream_events() -> Iterator[str]:
        while True:
            item = event_queue.get()
            if item is None:
                return
            yield item

    return StreamingResponse(stream_events(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/cancel")
def cancel_chat_turn_endpoint(
    session_id: str,
    request: ChatTurnCancelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    _ensure_request_tenant(request.tenant_id, current_user)
    chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, session_id)
    cancel_chat_turn(session_id, request.turn_id)
    _persist_chat_turn_cancelled(db, request.tenant_id, chat_session, request.turn_id, current_user.id)
    db.commit()
    return {"ok": True}


def _persist_chat_turn_cancelled(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    requested_turn_id: str,
    cancelled_by_user_id: str | None = None,
) -> bool:
    requested_turn_id = requested_turn_id.strip()
    if not requested_turn_id:
        return False

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == chat_session.id)
        .order_by(AgentEvent.created_at)
    ).all()
    message_id = ""
    client_turn_id = ""
    for event in reversed(events):
        if event.event_type != "user_message_received":
            continue
        payload = event.payload_json or {}
        candidate_message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
        candidate_client_turn_id = str(payload.get("client_turn_id") or "").strip()
        if requested_turn_id in {candidate_message_id, candidate_client_turn_id}:
            message_id = candidate_message_id
            client_turn_id = candidate_client_turn_id
            break
    if not message_id:
        message_id = requested_turn_id
        client_turn_id = requested_turn_id

    turn_ids = {message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    for event in events:
        if event.event_type not in {"assistant_message_created", "stream_cancelled"}:
            continue
        payload = event.payload_json or {}
        event_turn_ids = {
            str(payload.get("turn_id") or "").strip(),
            str(payload.get("user_message_id") or "").strip(),
            str(payload.get("message_id") or "").strip(),
            str(payload.get("client_turn_id") or "").strip(),
        }
        matches_message = bool(message_id and message_id in event_turn_ids)
        matches_client_turn = bool(client_turn_id and client_turn_id in event_turn_ids)
        if not matches_message and not matches_client_turn:
            continue
        if event.event_type == "stream_cancelled":
            return _ensure_cancelled_assistant_message(
                db,
                tenant_id,
                chat_session,
                message_id,
                client_turn_id,
                event.created_at + timedelta(microseconds=1),
            )
        return False

    now = utc_now()
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="stream_cancelled",
            payload_json={
                "turn_id": message_id,
                "user_message_id": message_id,
                "client_turn_id": client_turn_id or None,
                "phase": "cancelled",
                "text": "已停止生成",
                "cancelled_by_user_id": cancelled_by_user_id,
            },
            created_at=now,
        )
    )
    _ensure_cancelled_assistant_message(
        db,
        tenant_id,
        chat_session,
        message_id,
        client_turn_id,
        now + timedelta(microseconds=1),
    )
    chat_session.status = "active"
    chat_session.updated_at = now
    db.add(chat_session)
    return True


def _ensure_cancelled_assistant_message(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    user_message_id: str,
    client_turn_id: str,
    created_at,
) -> bool:
    user_message = db.get(Message, user_message_id)
    if not user_message or user_message.tenant_id != tenant_id or user_message.session_id != chat_session.id:
        return False
    if user_message.role != "user":
        return False

    turn_ids = {user_message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == chat_session.id, Message.role == "assistant")
        .order_by(Message.created_at)
    ).all()
    for message_row in messages:
        metadata = message_row.metadata_json or {}
        row_turn_ids = {
            str(metadata.get("turn_id") or "").strip(),
            str(metadata.get("user_message_id") or "").strip(),
            str(metadata.get("client_turn_id") or "").strip(),
        }
        if turn_ids & row_turn_ids:
            return False

    assistant_message = Message(
        tenant_id=tenant_id,
        session_id=chat_session.id,
        role="assistant",
        content=CANCELLED_ASSISTANT_REPLY,
        metadata_json={
            "turn_id": user_message_id,
            "user_message_id": user_message_id,
            "client_turn_id": client_turn_id or None,
            "status": "cancelled",
        },
        created_at=created_at,
    )
    db.add(assistant_message)
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="assistant_message_created",
            payload_json={
                "message_id": assistant_message.id,
                "assistant_message_id": assistant_message.id,
                "user_message_id": user_message_id,
                "turn_id": user_message_id,
                "client_turn_id": client_turn_id or None,
                "reply": CANCELLED_ASSISTANT_REPLY,
                "status": "cancelled",
            },
            created_at=created_at,
        )
    )
    chat_session.summary = f"最近回复：{CANCELLED_ASSISTANT_REPLY}"
    chat_session.updated_at = created_at
    db.add(chat_session)
    return True


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
    _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    title = _normalize_title(request.title)
    row = ChatSession(
        id=new_id("session"),
        tenant_id=request.tenant_id,
        user_id=current_user.id,
        agent_id=request.agent_id,
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
    _cleanup_stale_completed_sessions(db, tenant_id, rows)
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
    skill_feedback_rows = db.exec(
        select(SkillFeedback).where(SkillFeedback.tenant_id == tenant_id, SkillFeedback.session_id == session_id)
    ).all()
    for message in messages:
        db.delete(message)
    for event in events:
        db.delete(event)
    for feedback in feedback_rows:
        db.delete(feedback)
    for feedback in skill_feedback_rows:
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
    chat_session = _get_readable_chat_session(db, tenant_id, current_user, session_id)
    _cleanup_stale_completed_sessions(db, tenant_id, [chat_session])
    rows = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    events = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type.in_(["user_message_received", "assistant_message_created"]),  # type: ignore[attr-defined]
        )
        .order_by(AgentEvent.created_at)
    ).all()
    turn_ids_by_message = _message_turn_ids_from_events(events)
    feedback_by_message = _feedback_by_message(db, tenant_id, current_user.id, [row.id for row in rows])
    return [message_read(row, feedback_by_message.get(row.id), turn_ids_by_message.get(row.id)) for row in rows]


@router.get("/sessions/{session_id}/events")
def list_chat_session_events(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    _ensure_request_tenant(tenant_id, current_user)
    _get_readable_chat_session(db, tenant_id, current_user, session_id)
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
        )
        .order_by(AgentEvent.created_at)
        .limit(500)
    ).all()
    return [_normalized_session_event_payload(row) for row in rows]


@router.get("/handoffs", response_model=list[HumanHandoffRead])
def list_human_handoffs(
    tenant_id: str = Query(...),
    status: str = Query("pending"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[HumanHandoffRead]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    stmt = select(HumanHandoffRequest).where(HumanHandoffRequest.tenant_id == tenant_id)
    if status != "all":
        stmt = stmt.where(HumanHandoffRequest.status == status)
    if current_user.username not in CHAT_ADMIN_USERNAMES:
        if status == "pending":
            stmt = stmt.where(
                or_(
                    HumanHandoffRequest.assignee_user_id == current_user.id,
                    HumanHandoffRequest.assignee_user_id.is_(None),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    HumanHandoffRequest.assignee_user_id == current_user.id,
                    HumanHandoffRequest.requester_user_id == current_user.id,
                )
            )
    rows = db.exec(stmt.order_by(HumanHandoffRequest.updated_at.desc()).limit(200)).all()
    return [human_handoff_read(row) for row in rows]


@router.post("/handoffs/{handoff_id}/reply", response_model=HumanHandoffRead)
def reply_human_handoff(
    handoff_id: str,
    request: HumanHandoffReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> HumanHandoffRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    row = db.get(HumanHandoffRequest, handoff_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Handoff request not found")
    if current_user.username not in CHAT_ADMIN_USERNAMES and row.assignee_user_id not in {None, current_user.id}:
        raise HTTPException(status_code=403, detail="Handoff request not assigned to current user")
    reply = request.reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="Reply is required")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Handoff request is not pending")
    chat_session = db.get(ChatSession, row.session_id)
    if not chat_session or chat_session.tenant_id != request.tenant_id:
        raise HTTPException(status_code=409, detail="Original handoff session is not available")

    now = utc_now()
    row.status = "answered"
    row.human_reply = reply
    row.answered_at = now
    row.updated_at = now
    row.resume_payload_json = {**(row.resume_payload_json or {}), "answered_by_user_id": current_user.id}
    db.add(row)

    chat_session.status = "active"
    chat_session.awaiting_input_json = None
    chat_session.summary = f"最近回复：{reply[:120]}"
    chat_session.updated_at = now
    db.add(chat_session)
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=row.session_id,
            event_type="human_handoff_answered",
            payload_json={
                "handoff_id": row.id,
                "agent_id": row.agent_id,
                "trigger_skill_id": row.trigger_skill_id,
                "trigger_step_id": row.trigger_step_id,
                "answered_by_user_id": current_user.id,
                "reply_preview": reply[:180],
            },
            created_at=now,
        )
    )
    db.commit()
    db.refresh(row)
    _resume_human_handoff_async(row.id)
    return human_handoff_read(row)


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
        existing.analysis_status = "pending"
        existing.analysis_bucket = None
        existing.analysis_reason = None
        existing.analysis_summary = None
        existing.analysis_confidence = None
        existing.analysis_json = {}
        existing.analyzed_at = None
        existing.updated_at = now
        row = existing
    else:
        row = MessageFeedback(
            tenant_id=request.tenant_id,
            session_id=message_row.session_id,
            message_id=message_row.id,
            user_id=current_user.id,
            rating=request.rating,
            analysis_status="pending",
            analysis_json={},
            created_at=now,
            updated_at=now,
        )
    db.add(row)
    _upsert_skill_feedback_for_message(db, request.tenant_id, current_user.id, message_row, request.rating, now)
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
    enqueue_feedback_analysis(row.tenant_id, row.id, row.session_id)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "message_id": row.message_id,
        "rating": row.rating,
        "analysis_status": row.analysis_status,
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
        _delete_skill_feedback_for_message(db, tenant_id, current_user.id, message_row)
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
    _get_readable_chat_session(db, tenant_id, current_user, session_id)
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


def _get_readable_chat_session(db: Session, tenant_id: str, current_user: User, session_id: str) -> ChatSession:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if row.user_id == current_user.id:
        return row
    if _user_can_read_handoff_session(db, tenant_id, current_user, session_id):
        return row
    raise HTTPException(status_code=404, detail="Session not found")


def _user_can_read_handoff_session(db: Session, tenant_id: str, current_user: User, session_id: str) -> bool:
    statement = select(HumanHandoffRequest).where(
        HumanHandoffRequest.tenant_id == tenant_id,
        HumanHandoffRequest.session_id == session_id,
    )
    if current_user.username not in CHAT_ADMIN_USERNAMES:
        statement = statement.where(
            or_(
                HumanHandoffRequest.assignee_user_id == current_user.id,
                HumanHandoffRequest.assignee_user_id.is_(None),
                HumanHandoffRequest.requester_user_id == current_user.id,
            )
        )
    return db.exec(statement).first() is not None


def _ensure_chat_agent_available(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile:
    if not agent_id:
        raise HTTPException(status_code=400, detail="Agent is required")
    ensure_tenant(db, tenant_id)
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id or row.status != "active" or row.is_overall:
        raise HTTPException(status_code=404, detail="Agent not available")
    if not _chat_agent_visible_to_user(row, current_user):
        raise HTTPException(status_code=403, detail="Agent not available")
    return row


def _bind_request_to_session_agent(
    db: Session,
    request: ChatTurnRequest,
    chat_session: ChatSession,
    current_user: User,
) -> ChatTurnRequest:
    if chat_session.agent_id:
        if request.agent_id and request.agent_id != chat_session.agent_id:
            raise HTTPException(status_code=409, detail="Session is already bound to another agent")
        return request.model_copy(update={"agent_id": chat_session.agent_id})

    agent = _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    chat_session.agent_id = agent.id
    chat_session.updated_at = utc_now()
    db.add(chat_session)
    db.commit()
    return request.model_copy(update={"agent_id": agent.id})


def _ensure_chat_session_available(db: Session, tenant_id: str, user_id: str, session_id: str) -> ChatSession:
    ensure_tenant(db, tenant_id)
    row = db.get(ChatSession, session_id)
    if not row or row.tenant_id != tenant_id or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


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


def _cleanup_stale_completed_sessions(
    db: Session,
    tenant_id: str,
    rows: list[ChatSession],
) -> None:
    candidates = [row for row in rows if row.active_skill_id]
    if not candidates:
        return
    skills = list(
        db.exec(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
        ).all()
    )
    if not skills:
        return
    loop = AgentLoop(db)
    changed = False
    for row in candidates:
        before = (
            row.active_skill_id,
            row.active_step_id,
            json.dumps(row.slots_json or {}, sort_keys=True, ensure_ascii=False),
        )
        loop._finish_stale_completed_skill(tenant_id, row, skills)
        after = (
            row.active_skill_id,
            row.active_step_id,
            json.dumps(row.slots_json or {}, sort_keys=True, ensure_ascii=False),
        )
        changed = changed or before != after
    if changed:
        db.commit()
        for row in candidates:
            db.refresh(row)


def _upsert_skill_feedback_for_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_row: Message,
    rating: str,
    now,
) -> None:
    skill_context = _active_skill_context_for_assistant_message(db, tenant_id, message_row)
    if not skill_context:
        return
    skill_id = skill_context["skill_id"]
    skill_version = skill_context.get("skill_version")
    step_id = skill_context.get("node_id") or skill_context.get("step_id")
    existing = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.message_id == message_row.id,
            SkillFeedback.user_id == user_id,
        )
    ).first()
    if existing:
        existing.skill_id = skill_id
        existing.skill_version = skill_version
        existing.step_id = step_id
        existing.rating = rating
        existing.updated_at = now
        db.add(existing)
        return
    db.add(
        SkillFeedback(
            tenant_id=tenant_id,
            skill_id=skill_id,
            skill_version=skill_version,
            step_id=step_id,
            session_id=message_row.session_id,
            message_id=message_row.id,
            user_id=user_id,
            rating=rating,
            created_at=now,
            updated_at=now,
        )
    )


def _delete_skill_feedback_for_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_row: Message,
) -> None:
    existing = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.message_id == message_row.id,
            SkillFeedback.user_id == user_id,
        )
    ).first()
    if existing:
        db.delete(existing)


def _active_skill_for_assistant_message(db: Session, tenant_id: str, message_row: Message) -> str | None:
    context = _active_skill_context_for_assistant_message(db, tenant_id, message_row)
    return context["skill_id"] if context else None


def _active_skill_context_for_assistant_message(
    db: Session, tenant_id: str, message_row: Message
) -> dict[str, str | None] | None:
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == message_row.session_id)
        .order_by(Message.created_at)
    ).all()
    target_index = next((index for index, item in enumerate(messages) if item.id == message_row.id), -1)
    if target_index < 0:
        return None
    user_message = next(
        (item for item in reversed(messages[:target_index]) if item.role == "user"),
        None,
    )
    if not user_message:
        return None

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == message_row.session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    collecting = False
    last_context: dict[str, str | None] | None = None
    skill_hint: str | None = None
    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "user_message_received":
            event_message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            collecting = bool(event_message_id and event_message_id == user_message.id)
            last_context = None if collecting else last_context
            skill_hint = None if collecting else skill_hint
            continue
        if not collecting:
            continue
        if event.event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                skill_hint = target_skill_id
        event_context = _skill_context_from_event(event, skill_hint=skill_hint)
        if event_context:
            last_context = event_context
            if event_context.get("skill_id"):
                skill_hint = event_context["skill_id"]
        if event.event_type == "assistant_message_created":
            assistant_message_id = str(
                payload.get("message_id") or payload.get("assistant_message_id") or ""
            ).strip()
            if assistant_message_id == message_row.id:
                return _fill_skill_context_version(db, tenant_id, last_context)
            continue
    return _fill_skill_context_version(db, tenant_id, last_context)


def _skill_id_from_event(event: AgentEvent) -> str | None:
    context = _skill_context_from_event(event)
    return context["skill_id"] if context else None


def _skill_context_from_event(event: AgentEvent, skill_hint: str | None = None) -> dict[str, str | None] | None:
    payload = event.payload_json or {}
    if event.event_type in {"skill_started", "skill_suspended", "skill_resumed", "skill_step_changed"}:
        skill_id = str(payload.get("to_skill_id") or payload.get("from_skill_id") or skill_hint or "") or None
        if not skill_id:
            return None
        skill_version = str(payload.get("to_skill_version") or payload.get("from_skill_version") or "") or None
        node_id = str(
            payload.get("to_node_id")
            or payload.get("from_node_id")
            or payload.get("to_step_id")
            or payload.get("from_step_id")
            or ""
        ) or None
        return {"skill_id": skill_id, "skill_version": skill_version, "node_id": node_id}
    if event.event_type == "skill_completed":
        skill_id = str(payload.get("skill_id") or "") or None
        if not skill_id:
            return None
        return {
            "skill_id": skill_id,
            "skill_version": str(payload.get("skill_version") or "") or None,
            "node_id": str(payload.get("node_id") or payload.get("step_id") or "") or None,
        }
    if event.event_type == "reflection_decision_created":
        skill_id = str(payload.get("target_skill_id") or "") or None
        if not skill_id:
            return None
        return {
            "skill_id": skill_id,
            "skill_version": str(payload.get("target_skill_version") or "") or None,
            "node_id": str(payload.get("target_node_id") or payload.get("target_step_id") or "") or None,
        }
    return None


def _fill_skill_context_version(
    db: Session, tenant_id: str, context: dict[str, str | None] | None
) -> dict[str, str | None] | None:
    if not context or context.get("skill_version"):
        return context
    skill_id = context.get("skill_id")
    if not skill_id:
        return context
    skill = db.exec(select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)).first()
    if skill:
        return {**context, "skill_version": skill.version}
    return context


def _trace_payload_text(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except Exception:
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _trace_payload_language(value: str) -> str:
    if not value.strip():
        return "text"
    try:
        json.loads(value)
        return "json"
    except Exception:
        return "text"


def _general_skill_trace_detail(payload: dict, phase: str) -> str | None:
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    if phase.startswith("reflection_"):
        parts = [
            str(review.get("reason") or "").strip(),
            str(review.get("repair_hint") or "").strip(),
        ]
        text = " · ".join(part for part in parts if part)
        return text or None
    detail = str(payload.get("rationale") or payload.get("text") or "").strip()
    return detail or None


def _general_skill_trace_output(payload: dict, phase: str) -> dict[str, str]:
    if phase == "stdout_chunk":
        output = _trace_payload_text(payload.get("stdout_preview") or payload.get("text"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看运行输出",
        } if output else {}
    if phase == "stderr_chunk":
        output = _trace_payload_text(payload.get("stderr_preview") or payload.get("text"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看错误输出",
        } if output else {}
    if phase in {"code_finished", "code_timeout"}:
        result: dict[str, object] = {}
        if "return_code" in payload:
            result["return_code"] = payload.get("return_code")
        if "structured_result" in payload:
            result["structured_result"] = payload.get("structured_result")
        if str(payload.get("stdout_preview") or "").strip():
            result["stdout"] = payload.get("stdout_preview")
        if str(payload.get("stderr_preview") or "").strip():
            result["stderr"] = payload.get("stderr_preview")
        output = _trace_payload_text(result if result else payload.get("stdout_preview") or payload.get("stderr_preview"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看超时结果" if phase == "code_timeout" else "查看执行结果",
        } if output else {}
    if phase.startswith("reflection_"):
        result: dict[str, object] = {}
        if "structured_result" in payload:
            result["structured_result"] = payload.get("structured_result")
        if "review" in payload:
            result["review"] = payload.get("review")
        if str(payload.get("stdout_preview") or "").strip():
            result["stdout"] = payload.get("stdout_preview")
        if str(payload.get("stderr_preview") or "").strip():
            result["stderr"] = payload.get("stderr_preview")
        output = _trace_payload_text(result)
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看校验详情",
        } if result and output else {}
    return {}


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    if user.username in {"admin", "admin_demo"}:
        return True
    metadata = row.metadata_json or {}
    return (
        metadata.get("owner_user_id") == user.id
        or metadata.get("owner_username") == user.username
        or metadata.get("created_by_user_id") == user.id
        or metadata.get("created_by_username") == user.username
        or metadata.get("published_to_gallery") is True
    )


def _normalize_title(value: str | None) -> str | None:
    if value is None:
        return None
    title = value.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Session title cannot be empty")
    return title[:80]


def _message_turn_ids_from_events(events: list[AgentEvent]) -> dict[str, str]:
    turn_ids: dict[str, str] = {}
    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "user_message_received":
            message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            if message_id:
                turn_ids[message_id] = message_id
            continue
        if event.event_type == "assistant_message_created":
            assistant_message_id = str(
                payload.get("message_id") or payload.get("assistant_message_id") or ""
            ).strip()
            explicit_turn_id = str(payload.get("turn_id") or payload.get("user_message_id") or "").strip()
            if assistant_message_id and explicit_turn_id:
                turn_ids[assistant_message_id] = explicit_turn_id
    return turn_ids


def _build_turn_traces(
    messages: list[Message],
    events: list[AgentEvent],
    skill_names: dict[str, str],
) -> list[dict]:
    if not events:
        return _fallback_knowledge_citation_traces(messages)

    user_messages_by_id = {message.id: message for message in messages if message.role == "user"}
    traces: list[dict] = []
    traces_by_turn_id: dict[str, dict] = {}
    skill_hints_by_turn_id: dict[str, str | None] = {}
    active_turn_id: str | None = None

    for event in events:
        payload = event.payload_json or {}
        if event.event_type == "user_message_received":
            text = str(payload.get("message") or "")
            message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            user_message = user_messages_by_id.get(message_id) if message_id else None
            turn_id = message_id or event.id
            active_turn_id = turn_id
            current = {
                "turn_id": turn_id,
                "user_message_id": message_id or None,
                "_user_message_content": user_message.content if user_message else text,
                "started_at": event.created_at.isoformat(),
                "completed_at": None,
                "lines": [],
            }
            traces.append(current)
            traces_by_turn_id[turn_id] = current
            skill_hints_by_turn_id[turn_id] = None
            continue

        target_turn_id = _event_trace_turn_id(event, active_turn_id)
        if not target_turn_id:
            continue
        current = traces_by_turn_id.get(target_turn_id)
        if not current:
            continue
        if event.event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                skill_hints_by_turn_id[target_turn_id] = target_skill_id

        skill_hint = skill_hints_by_turn_id.get(target_turn_id)
        trace_was_completed = bool(current.get("completed_at"))
        lines = _event_trace_lines(event, skill_names, skill_hint)
        for line in lines:
            if trace_was_completed and line.get("state") == "running":
                line = {**line, "state": "completed"}
            _upsert_trace_line(current["lines"], line)
        event_context = _skill_context_from_event(event, skill_hint=skill_hint)
        if event_context and event_context.get("skill_id"):
            skill_hints_by_turn_id[target_turn_id] = event_context["skill_id"]
        if event.event_type == "assistant_message_created":
            if not current.get("completed_at"):
                current["completed_at"] = event.created_at.isoformat()
            _ensure_knowledge_query_line(current["lines"], current.get("_user_message_content"))
            _complete_trace_lines(current["lines"])
            if active_turn_id == target_turn_id:
                active_turn_id = None
        elif event.event_type == "stream_cancelled":
            if not current.get("completed_at"):
                current["completed_at"] = event.created_at.isoformat()
            _finish_trace_if_needed(current, event.created_at)
            if active_turn_id == target_turn_id:
                active_turn_id = None

    fallback_time = events[-1].created_at if events else None
    open_turn_id = active_turn_id
    for current in traces:
        _ensure_knowledge_query_line(current["lines"], current.get("_user_message_content"))
        if open_turn_id and current.get("turn_id") == open_turn_id and not current.get("completed_at"):
            continue
        _finish_trace_if_needed(current, fallback_time)

    for trace in traces:
        trace.pop("_user_message_content", None)
    return _with_scheduled_draft_message_traces(traces, messages)


def _event_trace_turn_id(event: AgentEvent, _active_turn_id: str | None) -> str | None:
    payload = event.payload_json or {}
    if event.event_type == "user_message_received":
        return str(payload.get("message_id") or payload.get("user_message_id") or "").strip() or event.id
    explicit_turn_id = str(payload.get("turn_id") or payload.get("user_message_id") or "").strip()
    if explicit_turn_id:
        return explicit_turn_id
    return None


def _with_scheduled_draft_message_traces(traces: list[dict], messages: list[Message]) -> list[dict]:
    traced_turn_ids = {str(trace.get("turn_id") or "") for trace in traces}
    next_traces = list(traces)
    previous_user: Message | None = None
    for message in messages:
        if message.role == "user":
            previous_user = message
            continue
        if message.role != "assistant" or not previous_user:
            continue
        metadata = message.metadata_json or {}
        draft = metadata.get("scheduled_task_draft") if isinstance(metadata, dict) else None
        if not isinstance(draft, dict) or previous_user.id in traced_turn_ids:
            continue
        next_traces.append(
            {
                "turn_id": previous_user.id,
                "user_message_id": previous_user.id,
                "started_at": previous_user.created_at.isoformat(),
                "completed_at": message.created_at.isoformat(),
                "lines": _scheduled_task_trace_lines(draft),
            }
        )
        traced_turn_ids.add(previous_user.id)
    next_traces.sort(key=lambda item: str(item.get("started_at") or ""))
    return next_traces


def _fallback_knowledge_citation_traces(messages: list[Message]) -> list[dict]:
    traces: list[dict] = []
    current_user: Message | None = None

    for message in messages:
        if message.role == "user":
            current_user = message
            continue
        if message.role != "assistant":
            continue
        metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
        citations = metadata.get("knowledge_citations") if isinstance(metadata, dict) else None
        if not isinstance(citations, list) or not citations:
            continue

        citation_titles = [
            str(item.get("title") or item.get("source_title") or item.get("concept_id") or "").strip()
            for item in citations
            if isinstance(item, dict)
        ]
        citation_titles = [title for title in citation_titles if title]
        citation_summary = "、".join(citation_titles[:3])
        if len(citation_titles) > 3:
            citation_summary = f"{citation_summary} 等"

        traces.append(
            {
                "turn_id": current_user.id if current_user else message.id,
                "user_message_id": current_user.id if current_user else None,
                "started_at": (current_user.created_at if current_user else message.created_at).isoformat(),
                "completed_at": message.created_at.isoformat(),
                "lines": [
                    {
                        "id": "thinking",
                        "kind": "thinking",
                        "text": "执行记录",
                        "state": "completed",
                    },
                    {
                        "id": "knowledge_intent",
                        "kind": "decision",
                        "text": "识别为业务资料问答",
                        "detail": "回答需要引用业务资料库，进入知识检索链路。",
                        "state": "completed",
                    },
                    {
                        "id": "knowledge_query",
                        "kind": "knowledge",
                        "text": "查询业务资料",
                        "detail": current_user.content if current_user else None,
                        "state": "completed",
                    },
                    {
                        "id": "knowledge_retrieval",
                        "kind": "knowledge",
                        "text": "读取业务资料",
                        "detail": (
                            f"命中 {len(citations)} 条知识引用"
                            + (f"：{citation_summary}" if citation_summary else "")
                        ),
                        "state": "completed",
                    },
                    {
                        "id": "knowledge_answer",
                        "kind": "decision",
                        "text": "生成带引用回答",
                        "detail": "已将知识引用附加到回复下方，可点击查看来源、章节和证据片段。",
                        "state": "completed",
                    },
                ],
            }
        )

    return traces


def _ensure_knowledge_query_line(lines: list[dict], user_message: object | None = None) -> None:
    has_query = any(line.get("text") == "查询业务资料" for line in lines)
    read_index = next(
        (index for index, line in enumerate(lines) if line.get("text") == "读取业务资料"),
        None,
    )
    if has_query or read_index is None:
        return
    detail = str(user_message or "").strip()
    lines.insert(
        read_index,
        {
            "id": "knowledge_query_synthetic",
            "kind": "knowledge",
            "text": "查询业务资料",
            "detail": detail or None,
            "state": "completed",
        },
    )


def _event_trace_lines(event: AgentEvent, skill_names: dict[str, str], skill_hint: str | None = None) -> list[dict]:
    line = _event_trace_line(event, skill_names, skill_hint)
    if not line:
        return []
    if isinstance(line, list):
        return line
    return [line]


def _event_trace_line(
    event: AgentEvent, skill_names: dict[str, str], skill_hint: str | None = None
) -> dict | list[dict] | None:
    payload = event.payload_json or {}
    if event.event_type == "stream_status":
        phase = str(payload.get("phase") or "").strip()
        text = str(payload.get("text") or "").strip()
        if phase == "scheduled_task_intent":
            return {
                "id": "scheduled_task_intent",
                "kind": "decision",
                "text": text or "识别定时任务需求",
                "detail": "用户选择了创建定时任务模式",
                "state": "running",
            }
        if phase == "scheduled_task_parse":
            return {
                "id": "scheduled_task_parse",
                "kind": "decision",
                "text": text or "解析执行计划",
                "detail": None,
                "state": "running",
            }
        if phase == "scheduled_task_draft":
            return {
                "id": "scheduled_task_draft",
                "kind": "decision",
                "text": text or "生成定时任务草案",
                "detail": _scheduled_task_trace_detail(payload),
                "state": "running",
            }
        if phase == "routing":
            return {
                "id": "decision_router",
                "kind": "decision",
                "text": "判断意图",
                "detail": None,
                "state": "running",
            }
        if phase == "responding":
            return None
        if phase and phase != "received":
            return {
                "id": f"decision_status_{phase}",
                "kind": "decision",
                "text": text or phase,
                "detail": None,
                "state": "running",
            }
        return None
    if event.event_type == "stream_cancelled":
        return {
            "id": "generation_stopped",
            "kind": "decision",
            "text": "用户已停止生成",
            "detail": None,
            "state": "completed",
        }
    if event.event_type == "general_skill_selected":
        skill_name = str(payload.get("skill_name") or payload.get("skill_slug") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": f"general_skill_selected_{event.id}",
            "kind": "skill",
            "text": f"选择通用技能 {skill_name}" if skill_name else "选择通用技能",
            "detail": reason or None,
            "state": "completed",
        }
    if event.event_type == "general_skill_intent_checked":
        skill_name = str(payload.get("skill_name") or payload.get("skill_slug") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": f"general_skill_intent_{event.id}",
            "kind": "decision",
            "text": "判断意图" if not skill_name else f"判断意图 {skill_name}",
            "detail": reason or None,
            "state": "completed",
        }
    if event.event_type == "general_skill_trace":
        message = str(payload.get("message") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        if phase == "replying":
            return None
        detail = _general_skill_trace_detail(payload, phase)
        output = _general_skill_trace_output(payload, phase)
        code = str(payload.get("code") or "").strip()
        runtime = str(payload.get("runtime") or "").strip().lower()
        code_phases = {
            "plan_created",
            "attempt_started",
            "running_code",
            "stdout_chunk",
            "stderr_chunk",
            "code_finished",
            "code_timeout",
        }
        return {
            "id": f"general_skill_trace_{event.id}",
            "kind": "code" if code or phase in code_phases else "decision",
            "text": message or phase or "执行通用技能",
            "detail": detail or None,
            "code": code or None,
            "language": "bash" if code and runtime == "bash" else "python" if code else None,
            "state": "completed",
            "collapsible": bool(code or output.get("output")),
            **output,
        }
    if event.event_type == "general_skill_run_finished":
        success = bool(payload.get("success"))
        return {
            "id": f"general_skill_finished_{event.id}",
            "kind": "skill",
            "text": "通用技能运行完成" if success else "通用技能运行失败",
            "detail": str(payload.get("skill_slug") or "") or None,
            "state": "completed" if success else "failed",
        }
    if event.event_type == "skill_state":
        lines = []
        for index, entry in enumerate(payload.get("currentSkills") or []):
            if not isinstance(entry, dict):
                continue
            skill_id = str(entry.get("skillId") or "").strip()
            if not skill_id:
                continue
            name = str(entry.get("name") or skill_id).strip()
            state = str(entry.get("state") or "active").strip()
            label = "挂起技能" if state == "suspended" else "等待技能" if state == "pending" else "执行技能"
            step_id = str(entry.get("stepId") or "").strip()
            lines.append(
                {
                    "id": f"skill_state_{skill_id}_{state}_{index}",
                    "kind": "skill",
                    "text": f"{label} {name}",
                    "detail": f"当前步骤 {step_id}" if step_id else None,
                    "state": "completed" if state == "suspended" else "running",
                }
            )
        return lines or None
    if event.event_type == "scheduled_task_draft_created":
        return _scheduled_task_trace_lines(payload)
    if event.event_type == "router_decision_created":
        intent = str(payload.get("user_intent") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": "decision_router",
            "kind": "decision",
            "text": f"判断意图 {intent}" if intent else "完成技能判断",
            "detail": reason or None,
            "state": "completed",
        }
    if event.event_type == "step_result":
        tool_call = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
        knowledge_query = payload.get("knowledge_query") if isinstance(payload.get("knowledge_query"), dict) else {}
        next_step_id = str(payload.get("next_step_id") or "").strip()
        reply = str(payload.get("reply") or "").strip()
        raw_tool_name = tool_call.get("name") if isinstance(tool_call, dict) else ""
        raw_knowledge_query = knowledge_query.get("query") if isinstance(knowledge_query, dict) else ""
        tool_name = str(raw_tool_name or "").strip()
        knowledge_query_text = str(raw_knowledge_query or "").strip()
        detail = " · ".join(
            part
            for part in (
                f"下一节点 {next_step_id}" if next_step_id else "",
                f"查询：{knowledge_query_text}" if knowledge_query_text else "",
                reply[:80] if not tool_name and not knowledge_query_text and reply else "",
            )
            if part
        )
        if tool_name:
            return {
                "id": f"decision_step_tool_{tool_name}",
                "kind": "decision",
                "text": f"决定调用工具 {tool_name}",
                "detail": detail or None,
                "state": "running",
            }
        if knowledge_query_text:
            return {
                "id": "decision_step_knowledge",
                "kind": "decision",
                "text": "决定查询知识库",
                "detail": detail or None,
                "state": "running",
            }
        return {
            "id": "decision_step_result",
            "kind": "decision",
            "text": "决定下一步" if next_step_id else "完成步骤判断",
            "detail": detail or None,
            "state": "completed",
        }
    if event.event_type in {"skill_started", "skill_suspended", "skill_resumed", "skill_step_changed"}:
        to_skill_id = str(payload.get("to_skill_id") or "")
        from_skill_id = str(payload.get("from_skill_id") or "")
        skill_id = to_skill_id or from_skill_id or (skill_hint or "")
        if not skill_id:
            return None
        decision = str(payload.get("decision") or "")
        is_interrupt_switch = (
            decision in {"answer_related_question_then_resume", "answer_chitchat_then_resume"}
            and from_skill_id
            and to_skill_id
            and from_skill_id != to_skill_id
        )
        label = {
            "skill_started": "选择技能",
            "skill_suspended": "切换技能",
            "skill_resumed": "恢复技能",
            "skill_step_changed": "推进技能",
        }[event.event_type]
        if is_interrupt_switch:
            label = "切换技能"
        detail_parts = []
        if from_skill_id and from_skill_id != to_skill_id:
            detail_parts.append(f"from {skill_names.get(from_skill_id, from_skill_id)}")
        if payload.get("to_step_id"):
            detail_parts.append(f"step {payload['to_step_id']}")
        line = {
            "id": f"skill_{event.id}",
            "kind": "skill",
            "text": f"{label} {skill_names.get(skill_id, skill_id)}",
            "detail": " · ".join(detail_parts) or None,
            "state": "completed",
        }
        if event.event_type != "skill_suspended" and not is_interrupt_switch:
            return line
        stack_lines = []
        for index, frame in enumerate(payload.get("skill_stack") or []):
            if not isinstance(frame, dict):
                continue
            suspended_skill_id = str(frame.get("skill_id") or "")
            if not suspended_skill_id or suspended_skill_id == skill_id:
                continue
            suspended_step_id = str(frame.get("step_id") or "").strip()
            stack_lines.append(
                {
                    "id": f"skill_{event.id}_suspended_{index}",
                    "kind": "skill",
                    "text": f"挂起技能 {skill_names.get(suspended_skill_id, suspended_skill_id)}",
                    "detail": f"当前步骤 {suspended_step_id}" if suspended_step_id else None,
                    "state": "completed",
                }
            )
        if not stack_lines and from_skill_id and from_skill_id != skill_id:
            from_step_id = str(payload.get("from_step_id") or "").strip()
            stack_lines.append(
                {
                    "id": f"skill_{event.id}_suspended_from",
                    "kind": "skill",
                    "text": f"挂起技能 {skill_names.get(from_skill_id, from_skill_id)}",
                    "detail": f"当前步骤 {from_step_id}" if from_step_id else None,
                    "state": "completed",
                }
            )
        return [*stack_lines, line]
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
        tool_call_id = str(payload.get("tool_call_id") or name or event.id)
        if not name:
            return None
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"调用工具 {name}",
            "detail": None,
            "state": "running",
        }
    if event.event_type == "knowledge_query_started":
        query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
        text = str(query.get("query") if isinstance(query, dict) else payload.get("text") or "").strip()
        return {
            "id": f"knowledge_{event.id}_started",
            "kind": "knowledge",
            "text": "查询业务资料",
            "detail": text or None,
            "state": "running",
        }
    if event.event_type in {"knowledge_query_finished", "knowledge_result"}:
        chunks = payload.get("chunks") if isinstance(payload.get("chunks"), list) else []
        buckets = payload.get("selected_buckets") if isinstance(payload.get("selected_buckets"), list) else []
        concepts = payload.get("selected_concepts") if isinstance(payload.get("selected_concepts"), list) else []
        evidence = payload.get("evidence_pack") if isinstance(payload.get("evidence_pack"), list) else []
        parts = [
            f"命中 Wiki {len(concepts)} 个" if concepts else "",
            f"展开 {len(buckets)} 个知识桶" if buckets else "",
            f"读取 {len(chunks)} 个片段" if chunks else "",
            f"生成 {len(evidence)} 条引用候选" if evidence else "",
        ]
        return {
            "id": f"knowledge_{event.id}_finished",
            "kind": "knowledge",
            "text": "读取业务资料",
            "detail": " · ".join(part for part in parts if part),
            "state": "completed",
        }
    if event.event_type == "tool_result":
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        raw_name = str(
            payload.get("rawToolName")
            or payload.get("toolId")
            or content.get("tool_name")
            or ""
        ).strip()
        display_name = str(payload.get("toolName") or raw_name).strip()
        tool_call_id = str(payload.get("toolCallId") or raw_name or event.id)
        success = payload.get("success")
        is_error = bool(payload.get("isError")) if success is None else not bool(success)
        detail_payload = content if isinstance(content, dict) else payload
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"{'工具调用失败' if is_error else '调用工具'} {display_name}",
            "detail": _tool_trace_detail(detail_payload),
            "state": "failed" if is_error else "completed",
        }
    if event.event_type == "tool_call_finished":
        name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or name or event.id)
        success = bool(payload.get("success"))
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"{'调用工具' if success else '工具调用失败'} {name}",
            "detail": _tool_trace_detail(payload),
            "state": "completed" if success else "failed",
        }
    if event.event_type == "agent_loop_continued":
        iteration = str(payload.get("iteration") or event.id)
        target_tool = str(payload.get("target_tool_name") or "").strip()
        return {
            "id": f"decision_stepping_tool_continuation_{iteration}",
            "kind": "decision",
            "text": "重新分析执行动作",
            "detail": f"决定继续调用工具 {target_tool}" if target_tool else "决定继续调用工具",
            "state": "completed",
        }
    if event.event_type == "agent_loop_completed":
        iteration = str(payload.get("iteration") or event.id)
        return {
            "id": f"decision_stepping_tool_continuation_{iteration}",
            "kind": "decision",
            "text": "重新分析执行动作",
            "detail": "判断无需继续调用工具",
            "state": "completed",
        }
    if event.event_type in {"reflection_decision_created", "reflection_decision"}:
        needs_retry = bool(payload.get("needs_retry"))
        return {
            "id": f"decision_{event.id}",
            "kind": "decision",
            "text": "反思后继续尝试" if needs_retry else "反思通过",
            "detail": _reflection_trace_detail(payload),
            "state": "completed",
        }
    if event.event_type == "reflection_skipped":
        return {
            "id": f"decision_{event.id}",
            "kind": "decision",
            "text": "反思已关闭",
            "detail": str(payload.get("reason") or "") or None,
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
