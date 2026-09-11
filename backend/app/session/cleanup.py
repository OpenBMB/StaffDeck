from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.core.harness_session_cleanup import (
    remove_harness_session_workspace,
    stage_harness_session_record_deletion,
)
from app.db.models import (
    AgentEvent,
    ChatSession,
    HumanHandoffRequest,
    Message,
    MessageFeedback,
    SkillFeedback,
    utc_now,
)

logger = logging.getLogger(__name__)


def cancel_pending_handoffs(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    reason: str,
) -> int:
    """级联取消会话的待处理转人工请求,返回取消条数。

    会话删除后 pending handoff 无法再恢复执行(原会话已不存在),
    残留会一直挂在收件箱里。仅取消 pending;answered/failed/cancelled
    属于历史记录,保留供审计。取消原因写入 metadata_json.cancelled_reason。
    """
    rows = db.exec(
        select(HumanHandoffRequest).where(
            HumanHandoffRequest.tenant_id == tenant_id,
            HumanHandoffRequest.session_id == session_id,
            HumanHandoffRequest.status == "pending",
        )
    ).all()
    for row in rows:
        row.status = "cancelled"
        row.updated_at = utc_now()
        metadata = dict(row.metadata_json or {})
        metadata["cancelled_reason"] = reason
        row.metadata_json = metadata
        db.add(row)
    return len(rows)


def purge_chat_session_records(db: Session, session: ChatSession) -> None:
    """Stage deletion of one chat session with its dependent rows.

    The caller owns the surrounding transaction; the on-disk Harness workspace
    should be removed afterwards via remove_chat_session_workspace.
    """
    tenant_id = session.tenant_id
    session_id = session.id
    cancel_pending_handoffs(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        reason="session deleted",
    )
    stage_harness_session_record_deletion(db, tenant_id=tenant_id, session_id=session_id)
    for model in (Message, AgentEvent, MessageFeedback, SkillFeedback):
        for row in db.exec(
            select(model).where(model.tenant_id == tenant_id, model.session_id == session_id)
        ).all():
            db.delete(row)
    db.delete(session)


def remove_chat_session_workspace(
    *,
    tenant_id: str,
    session_id: str,
    db: Session | None = None,
) -> None:
    """Remove one session's Harness workspace after the deletion commit."""
    try:
        remove_harness_session_workspace(tenant_id=tenant_id, session_id=session_id, db=db)
    except OSError:
        logger.warning(
            "Failed to remove Harness workspace for tenant=%s session=%s",
            tenant_id,
            session_id,
            exc_info=True,
        )
