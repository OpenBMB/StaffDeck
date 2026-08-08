from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.async_jobs import AsyncJob, enqueue_async_job
from app.channels.adapters.base import is_group_conv_id
from app.db import engine
from app.db.models import AgentEvent, ChatSession, Message, ModelConfig
from app.memory.service import MemoryService, memory_read
from app.observability import EventLog
from app.observability.spans import bind_span_sink
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.tools.tool_schema import ToolResult


def enqueue_memory_capture(
    request: ChatTurnRequest,
    session_id: str,
    step_result: StepAgentResult,
    tool_result: ToolResult | None,
    model_config_id: str,
) -> AsyncJob:
    payload = {
        "request": request.model_dump(mode="json"),
        "session_id": session_id,
        "step_result": step_result.model_dump(mode="json"),
        "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
        "model_config_id": model_config_id,
    }
    return enqueue_async_job(
        "memory.capture_turn",
        run_memory_capture_job,
        payload,
        metadata={
            "tenant_id": request.tenant_id,
            "session_id": session_id,
            "user_id": request.user_id,
        },
    )


def run_memory_capture_job(payload: dict[str, Any]) -> None:
    request = ChatTurnRequest.model_validate(payload["request"])
    session_id = str(payload["session_id"])
    model_config_id = str(payload["model_config_id"])
    step_result = StepAgentResult.model_validate(payload["step_result"])
    tool_result = ToolResult.model_validate(payload["tool_result"]) if payload.get("tool_result") else None
    with Session(engine) as db:
        events = EventLog(db)
        chat_session = db.get(ChatSession, session_id)
        model_config = db.get(ModelConfig, model_config_id)
        if not chat_session or not model_config:
            events.record(
                request.tenant_id,
                session_id,
                "memory_error",
                {
                    "message": "后台 Memory 任务缺少 session 或 model_config。",
                    "missing_session": not bool(chat_session),
                    "missing_model_config": not bool(model_config),
                },
            )
            db.commit()
            return
        # 群记忆红线:群聊会话不沉淀任何记忆(成员隐私不得混入群账号/跨成员共享)
        if is_group_conv_id(chat_session.channel or "", chat_session.external_conv_id or ""):
            return

        user_events = db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == request.tenant_id,
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == "user_message_received",
            )
            .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        ).all()
        latest_user_event = next(
            (
                event
                for event in user_events
                if request.client_turn_id
                and str((event.payload_json or {}).get("client_turn_id") or "")
                == request.client_turn_id
            ),
            user_events[0] if user_events else None,
        )
        latest_user_payload = dict(latest_user_event.payload_json or {}) if latest_user_event else {}
        turn_id = str(
            latest_user_payload.get("turn_id")
            or latest_user_payload.get("user_message_id")
            or latest_user_payload.get("message_id")
            or ""
        )
        conversation_messages = _conversation_messages_for_turn(
            db, request.tenant_id, session_id, turn_id
        )
        if not conversation_messages:
            events.record(
                request.tenant_id,
                session_id,
                "memory_error",
                {"message": "后台 Memory 任务未找到本轮已落库的完整消息历史。"},
            )
            db.commit()
            return

        def persist_span(event_type: str, event_payload: dict[str, Any]) -> None:
            traced_payload = dict(event_payload)
            if turn_id:
                traced_payload.setdefault("turn_id", turn_id)
                traced_payload.setdefault("user_message_id", turn_id)
            if request.client_turn_id:
                traced_payload.setdefault("client_turn_id", request.client_turn_id)
            events.record(request.tenant_id, session_id, event_type, traced_payload)
            db.commit()

        try:
            with bind_span_sink(persist_span):
                rows = MemoryService(db).capture_turn(
                    request,
                    chat_session,
                    step_result,
                    tool_result,
                    model_config,
                    conversation_messages,
                )
        except Exception as exc:  # noqa: BLE001 - persist failure without affecting the request path.
            events.record(
                request.tenant_id,
                session_id,
                "memory_error",
                {"message": str(exc)},
            )
            db.commit()
            return

        saved = [memory_read(row) for row in rows]
        if saved:
            events.record(
                request.tenant_id,
                session_id,
                "memory_saved",
                {"memories": saved, "async": True},
            )
        db.commit()


def _conversation_messages_for_turn(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_id: str,
) -> list[dict[str, str]]:
    if not turn_id:
        return []
    rows = list(
        db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(100)
        ).all()
    )
    rows.reverse()
    target_index = next(
        (
            index
            for index in range(len(rows) - 1, -1, -1)
            if rows[index].role == "assistant"
            and str(
                (rows[index].metadata_json or {}).get("turn_id")
                or (rows[index].metadata_json or {}).get("user_message_id")
                or ""
            )
            == turn_id
        ),
        None,
    )
    if target_index is None:
        return []
    return [
        {"role": row.role, "content": row.content}
        for row in rows[: target_index + 1]
        if row.role in {"user", "assistant"} and row.content.strip()
    ][-12:]
