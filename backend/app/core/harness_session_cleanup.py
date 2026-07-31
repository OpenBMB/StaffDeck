from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from app import paths
from app.db.models import (
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
)


@dataclass(frozen=True)
class HarnessSessionRecordCleanup:
    session_lease_count: int
    turn_count: int
    invocation_count: int
    run_count: int
    task_frame_count: int


def stage_harness_session_record_deletion(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> HarnessSessionRecordCleanup:
    """Stage Harness v2 records for deletion in dependency order.

    The caller owns the surrounding transaction so chat-session deletion remains
    atomic with the existing message, event, and feedback cleanup.
    """

    invocations = db.exec(
        select(HarnessInvocationRecord).where(
            HarnessInvocationRecord.tenant_id == tenant_id,
            HarnessInvocationRecord.session_id == session_id,
        )
    ).all()
    runs = db.exec(
        select(HarnessRunRecord).where(
            HarnessRunRecord.tenant_id == tenant_id,
            HarnessRunRecord.session_id == session_id,
        )
    ).all()
    task_frames = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
        )
    ).all()
    turns = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
        )
    ).all()
    session_leases = db.exec(
        select(HarnessSessionLeaseRecord).where(
            HarnessSessionLeaseRecord.tenant_id == tenant_id,
            HarnessSessionLeaseRecord.session_id == session_id,
        )
    ).all()

    for invocation in invocations:
        db.delete(invocation)
    db.flush()
    for run in runs:
        db.delete(run)
    db.flush()
    for task_frame in task_frames:
        db.delete(task_frame)
    db.flush()
    for turn in turns:
        db.delete(turn)
    db.flush()
    for session_lease in session_leases:
        db.delete(session_lease)
    db.flush()

    return HarnessSessionRecordCleanup(
        session_lease_count=len(session_leases),
        turn_count=len(turns),
        invocation_count=len(invocations),
        run_count=len(runs),
        task_frame_count=len(task_frames),
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


def harness_session_workspace_path(*, tenant_id: str, session_id: str) -> Path:
    data_root = paths.user_data_dir().resolve()
    return (
        data_root
        / "harness_workspaces"
        / harness_path_segment(tenant_id)
        / harness_path_segment(session_id)
    )


def harness_task_workspace_path(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
) -> Path:
    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
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


def remove_harness_session_workspace(*, tenant_id: str, session_id: str) -> bool:
    """Remove only one exact tenant/session Harness workspace.

    Parent symlinks are rejected so cleanup can never traverse a redirected
    ``harness_workspaces`` or tenant directory. A symlink at the exact session
    path is unlinked without touching its target.
    """

    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
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
