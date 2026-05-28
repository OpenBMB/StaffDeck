from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.api.chat import message_read, session_read
from app.db import get_session
from app.db.models import ChatSession, Message, MessageFeedback, User
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/feedback", tags=["enterprise:feedback"])


@router.get("/sessions")
def list_feedback_sessions(
    tenant_id: str = Query(...),
    rating: str = Query(default="down"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_session),
) -> list[dict]:
    ensure_tenant(db, tenant_id)
    feedback_rows = list(
        db.exec(
            select(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.rating == rating)
            .order_by(MessageFeedback.updated_at.desc())
            .limit(limit)
        ).all()
    )
    grouped: dict[str, list[MessageFeedback]] = {}
    for row in feedback_rows:
        grouped.setdefault(row.session_id, []).append(row)

    results: list[dict] = []
    for session_id, rows in grouped.items():
        chat_session = db.get(ChatSession, session_id)
        if not chat_session or chat_session.tenant_id != tenant_id:
            continue
        latest = max(rows, key=lambda item: item.updated_at)
        latest_message = db.get(Message, latest.message_id)
        user = db.get(User, chat_session.user_id) if chat_session.user_id else None
        results.append(
            {
                "session_id": chat_session.id,
                "tenant_id": chat_session.tenant_id,
                "user_id": chat_session.user_id,
                "username": user.username if user else None,
                "display_name": user.display_name if user else None,
                "title": chat_session.title,
                "summary": chat_session.summary,
                "status": chat_session.status,
                "feedback_count": len(rows),
                "latest_feedback_at": latest.updated_at.isoformat(),
                "latest_message_id": latest.message_id,
                "latest_message": latest_message.content if latest_message else "",
                "updated_at": chat_session.updated_at.isoformat(),
            }
        )
    return sorted(results, key=lambda item: item["latest_feedback_at"], reverse=True)


@router.get("/sessions/{session_id}")
def get_feedback_session_detail(
    session_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict:
    ensure_tenant(db, tenant_id)
    chat_session = db.get(ChatSession, session_id)
    if not chat_session or chat_session.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = list(
        db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.created_at)
        ).all()
    )
    feedback_rows = list(
        db.exec(
            select(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.session_id == session_id)
            .order_by(MessageFeedback.updated_at.desc())
        ).all()
    )
    feedback_by_message = {row.message_id: row for row in feedback_rows}
    user = db.get(User, chat_session.user_id) if chat_session.user_id else None
    return {
        "session": {
            **session_read(chat_session).model_dump(),
            "username": user.username if user else None,
            "display_name": user.display_name if user else None,
        },
        "messages": [_message_with_feedback(message, feedback_by_message.get(message.id)) for message in messages],
        "feedback": [
            {
                "id": row.id,
                "message_id": row.message_id,
                "user_id": row.user_id,
                "rating": row.rating,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in feedback_rows
        ],
    }


def _message_with_feedback(message: Message, feedback: MessageFeedback | None) -> dict:
    payload = message_read(message, feedback.rating if feedback else None).model_dump()
    if feedback:
        payload["feedback_updated_at"] = feedback.updated_at.isoformat()
    return payload
