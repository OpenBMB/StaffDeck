from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.core.harness_session_cleanup import (
    HarnessSessionRecordCleanup,
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


def purge_chat_session_records(
    db: Session,
    session: ChatSession,
) -> HarnessSessionRecordCleanup:
    """Stage deletion of one chat session with its dependent rows.

    The caller owns the surrounding transaction; the on-disk Harness workspace
    should be removed afterwards via remove_chat_session_workspace. Its return
    value preserves historical roots that must be removed after commit.
    """
    tenant_id = session.tenant_id
    session_id = session.id
    harness_cleanup = stage_harness_session_record_deletion(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    # Remove conversation rows after Harness rows have released their dependencies.
    for model in (Message, AgentEvent, MessageFeedback, SkillFeedback):
        for row in db.exec(
            select(model).where(model.tenant_id == tenant_id, model.session_id == session_id)
        ).all():
            db.delete(row)
    db.delete(session)
    return harness_cleanup


def remove_chat_session_workspace(
    *,
    tenant_id: str,
    session_id: str,
    db: Session | None = None,
    workspace_roots: tuple[str, ...] = (),
) -> None:
    """Remove one session's Harness workspace after the deletion commit.

    ``workspace_roots`` must be the snapshots returned while the TaskFrame rows
    still existed; it is empty for sessions without a snapshotted workspace.
    """
    try:
        remove_harness_session_workspace(
            tenant_id=tenant_id,
            session_id=session_id,
            db=db,
            workspace_roots=workspace_roots,
        )
    except OSError:
        logger.warning(
            "Failed to remove Harness workspace for tenant=%s session=%s",
            tenant_id,
            session_id,
            exc_info=True,
        )
