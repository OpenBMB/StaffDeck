from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import Session

from app import paths
from app.db.bulk_delete import bulk_delete_matching
from app.db.models import (
    HarnessAgentLoopRecord,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    UIConfig,
    utc_now,
)


@dataclass(frozen=True)
class HarnessSessionRecordCleanup:
    session_lease_count: int
    turn_count: int
    invocation_count: int
    run_count: int
    task_frame_count: int
    # 后加的字段给默认值：老的构造点（含测试里的假数据）不需要同步改。
    agent_loop_count: int = 0


def _delete_session_rows(
    db: Session, model: type, *, tenant_id: str, session_id: str
) -> int:
    """按 tenant_id + session_id 批量删一张表，返回命中行数。

    刻意用 Core 的 DELETE 而不是「SELECT 出行再 db.delete(row)」：后者会把整行
    （``agent_events.payload_json`` / ``messages.content_json`` 这类字段单会话可到 MB 级）
    读进 Python 只是为了丢掉，实测单会话删除从 4.6s 降到 0.08s。
    ``bulk_delete_matching`` 会顺带把会话内已加载的同批对象摘出 identity map，
    避免调用方随后读属性时去刷新已删除的行。
    """
    return bulk_delete_matching(
        db, model, tenant_id=tenant_id, session_id=session_id
    )


def stage_harness_session_record_deletion(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> HarnessSessionRecordCleanup:
    """Stage Harness v2 records for deletion in dependency order.

    The caller owns the surrounding transaction so chat-session deletion remains
    atomic with the existing message, event, and feedback cleanup.

    删除顺序即依赖顺序（invocation → run → task_frame → agent_loop → turn → lease）。
    库里这些表之间没有外键约束，顺序只影响可读性；``harness_agent_loops`` 曾经被漏删，
    导致会话删掉后留下 active 的孤儿 loop（本次一并修掉）。
    """

    invocation_count = _delete_session_rows(
        db, HarnessInvocationRecord, tenant_id=tenant_id, session_id=session_id
    )
    run_count = _delete_session_rows(
        db, HarnessRunRecord, tenant_id=tenant_id, session_id=session_id
    )
    task_frame_count = _delete_session_rows(
        db, HarnessTaskFrameRecord, tenant_id=tenant_id, session_id=session_id
    )
    agent_loop_count = _delete_session_rows(
        db, HarnessAgentLoopRecord, tenant_id=tenant_id, session_id=session_id
    )
    turn_count = _delete_session_rows(
        db, HarnessTurnRecord, tenant_id=tenant_id, session_id=session_id
    )
    session_lease_count = _delete_session_rows(
        db, HarnessSessionLeaseRecord, tenant_id=tenant_id, session_id=session_id
    )

    return HarnessSessionRecordCleanup(
        session_lease_count=session_lease_count,
        turn_count=turn_count,
        invocation_count=invocation_count,
        run_count=run_count,
        task_frame_count=task_frame_count,
        agent_loop_count=agent_loop_count,
    )


def stage_harness_session_execution_reset(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> None:
    """Cancel durable Harness execution state while preserving turn receipts.

    与删除同理：这些行只需按条件改状态/删掉，没必要读进 ORM 再逐行处理。
    ``turns`` 用批量 UPDATE 落同一个终态（status/error_json/finished_at/updated_at 四个字段
    的取值与逐行赋值完全一致）。
    """
    now = utc_now()
    db.exec(
        sa_update(HarnessTurnRecord)
        .where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.status == "started",
        )
        .values(
            status="cancelled",
            error_json={
                "code": "SESSION_RESET",
                "message": "会话已重置，原 Harness turn 已取消。",
            },
            finished_at=now,
            updated_at=now,
        )
    )
    db.exec(
        sa_delete(HarnessInvocationRecord).where(
            HarnessInvocationRecord.tenant_id == tenant_id,
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.status == "started",
        )
    )
    db.exec(
        sa_delete(HarnessRunRecord).where(
            HarnessRunRecord.tenant_id == tenant_id,
            HarnessRunRecord.session_id == session_id,
            HarnessRunRecord.status == "running",
        )
    )
    db.exec(
        sa_delete(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.status.notin_({"completed", "cancelled", "failed"}),
        )
    )
    db.exec(
        sa_delete(HarnessSessionLeaseRecord).where(
            HarnessSessionLeaseRecord.tenant_id == tenant_id,
            HarnessSessionLeaseRecord.session_id == session_id,
        )
    )


def harness_path_segment(value: str) -> str:
    """Map an external identifier to the exact Harness workspace segment."""

    raw = str(value or "")
    normalized = "".join(
        character
        for character in raw
        if character.isalnum() or character in {"-", "_"}
    )
    prefix = normalized[:72] or "unknown"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{suffix}"


def harness_storage_root(*, tenant_id: str, db: Session | None = None) -> Path:
    """Resolve the administrator-selected root for new non-sandboxed workspaces."""

    default_root = paths.user_data_dir().resolve() / "harness_workspaces"
    if db is not None:
        row = db.get(UIConfig, tenant_id)
        configured = str(getattr(row, "harness_storage_path", "") or "").strip()
        if row is not None and not bool(getattr(row, "sandbox_enabled", False)) and configured:
            return Path(configured).expanduser().resolve()
    return default_root


def harness_session_workspace_path(
    *, tenant_id: str, session_id: str, db: Session | None = None
) -> Path:
    return (
        harness_storage_root(tenant_id=tenant_id, db=db)
        / harness_path_segment(tenant_id)
        / harness_path_segment(session_id)
    )


def harness_task_workspace_path(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    db: Session | None = None,
) -> Path:
    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        db=db,
    )
    task_path = session_path / harness_path_segment(task_frame_id)
    for parent in (
        session_path.parents[1],
        session_path.parent,
        session_path,
        task_path,
    ):
        if parent.is_symlink():
            raise OSError(
                "refusing to provision Harness workspace through a symlink"
            )
    return task_path


def remove_harness_session_workspace(
    *, tenant_id: str, session_id: str, db: Session | None = None
) -> bool:
    """Remove only one exact tenant/session Harness workspace.

    Parent symlinks are rejected so cleanup can never traverse a redirected
    ``harness_workspaces`` or tenant directory. A symlink at the exact session
    path is unlinked without touching its target.
    """

    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        db=db,
    )
    harness_root = session_path.parents[1]
    tenant_path = session_path.parent

    if harness_root.is_symlink() or tenant_path.is_symlink():
        raise OSError("refusing to clean Harness workspace through a symlinked parent")
    if session_path.is_symlink():
        session_path.unlink()
        return True
    if not session_path.exists():
        return False
    if not session_path.is_dir():
        session_path.unlink()
        return True

    shutil.rmtree(session_path)
    return True
