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
    Message,
    MessageFeedback,
    SkillFeedback,
)

logger = logging.getLogger(__name__)


def purge_chat_session_records(db: Session, session: ChatSession) -> None:
    """Stage deletion of one chat session with its dependent rows.

    The caller owns the surrounding transaction; the on-disk Harness workspace
    should be removed afterwards via remove_chat_session_workspace.
    """
    tenant_id = session.tenant_id
    session_id = session.id
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
