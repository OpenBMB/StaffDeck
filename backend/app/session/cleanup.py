from __future__ import annotations

import logging

from sqlmodel import Session

from app.core.engine.session.cleanup import (
    HarnessSessionRecordCleanup,
    remove_harness_session_workspace,
    stage_harness_session_record_deletion,
)
from app.db.bulk_delete import bulk_delete_matching
from app.db.models import (
    AgentEvent,
    ChatSession,
    Message,
    MessageFeedback,
    SkillFeedback,
)

logger = logging.getLogger(__name__)

# 依赖会话的从属表。顺序只影响可读性（库内无外键），全部按 tenant_id + session_id 批量删。
_SESSION_DEPENDENT_MODELS: tuple[type, ...] = (
    Message,
    AgentEvent,
    MessageFeedback,
    SkillFeedback,
)


def purge_chat_session_records(
    db: Session, session: ChatSession
) -> HarnessSessionRecordCleanup:
    """Stage deletion of one chat session with its dependent rows.

    The caller owns the surrounding transaction; the on-disk Harness workspace
    should be removed afterwards via remove_chat_session_workspace.

    这里刻意用批量 DELETE 而不是「SELECT 全量行 → 逐行 db.delete(row)」：后者会把
    ``agent_events.payload_json`` 这类字段整会话读进内存只为丢掉，是一次实测
    4.6s → 0.08s 的删除耗时回归来源。``sessions`` 单行仍是 ORM 删除，保证调用方
    拿到的 ORM 对象随事务一起失效。
    """
    tenant_id = session.tenant_id
    session_id = session.id
    cleanup = stage_harness_session_record_deletion(
        db, tenant_id=tenant_id, session_id=session_id
    )
    for model in _SESSION_DEPENDENT_MODELS:
        bulk_delete_matching(db, model, tenant_id=tenant_id, session_id=session_id)
    db.delete(session)
    return cleanup


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
