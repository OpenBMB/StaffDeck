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
    UIConfig,
    utc_now,
)
from app.harness.artifacts import (
    HarnessArtifactAccessError,
    OpenedHarnessArtifact,
    open_harness_artifact,
)


@dataclass(frozen=True)
class HarnessSessionRecordCleanup:
    """Describe deleted Harness records and the persisted workspace roots needed after commit."""

    session_lease_count: int
    turn_count: int
    invocation_count: int
    run_count: int
    task_frame_count: int
    workspace_roots: tuple[str, ...]


class HarnessWorkspaceArtifactConflictError(HarnessArtifactAccessError):
    """Raised when more than one bounded workspace root contains one requested artifact path."""


def stage_harness_session_execution_reset(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> None:
    """Cancel durable Harness execution state while preserving turn receipts."""
    invocations = db.exec(
        select(HarnessInvocationRecord).where(
            HarnessInvocationRecord.tenant_id == tenant_id,
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.status == "started",
        )
    ).all()
    runs = db.exec(
        select(HarnessRunRecord).where(
            HarnessRunRecord.tenant_id == tenant_id,
            HarnessRunRecord.session_id == session_id,
            HarnessRunRecord.status == "running",
        )
    ).all()
    task_frames = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.status.notin_(
                {"completed", "cancelled", "failed"}
            ),
        )
    ).all()
    turns = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.status == "started",
        )
    ).all()
    leases = db.exec(
        select(HarnessSessionLeaseRecord).where(
            HarnessSessionLeaseRecord.tenant_id == tenant_id,
            HarnessSessionLeaseRecord.session_id == session_id,
        )
    ).all()

    now = utc_now()
    for turn in turns:
        turn.status = "cancelled"
        turn.error_json = {
            "code": "SESSION_RESET",
            "message": "会话已重置，原 Harness turn 已取消。",
        }
        turn.finished_at = now
        turn.updated_at = now
        db.add(turn)
    for invocation in invocations:
        db.delete(invocation)
    db.flush()
    for run in runs:
        db.delete(run)
    db.flush()
    for task_frame in task_frames:
        db.delete(task_frame)
    db.flush()
    for lease in leases:
        db.delete(lease)
    db.flush()


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

    workspace_roots = _workspace_roots_from_task_frames(task_frames)

    # Delete in foreign-key dependency order after capturing roots needed for post-commit cleanup.
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
        workspace_roots=workspace_roots,
    )


def _workspace_roots_from_task_frames(
    task_frames: list[HarnessTaskFrameRecord],
) -> tuple[str, ...]:
    """Extract distinct non-empty TaskFrame root snapshots before their database rows are deleted."""

    roots: list[str] = []
    for task_frame in task_frames:
        root = str(task_frame.workspace_root or "").strip()
        if root and root not in roots:
            roots.append(root)
    return tuple(roots)


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

    default_root = default_harness_storage_root()
    if db is not None:
        row = db.get(UIConfig, tenant_id)
        configured = str(getattr(row, "harness_storage_path", "") or "").strip()
        if row is not None and not bool(getattr(row, "sandbox_enabled", False)) and configured:
            return Path(configured).expanduser().resolve()
    return default_root


def default_harness_storage_root() -> Path:
    """Return the user-owned default root for newly created Harness workspaces."""

    return Path.home() / ".staffdeck" / "workspaces"


def legacy_harness_storage_root() -> Path:
    """Return the pre-migration default root used only for deterministic compatibility reads."""

    return paths.user_data_dir().resolve() / "harness_workspaces"


def harness_session_workspace_path(
    *, tenant_id: str, session_id: str, db: Session | None = None
) -> Path:
    """Return the current effective workspace path for one session's newly created TaskFrames."""

    return (
        harness_storage_root(tenant_id=tenant_id, db=db)
        / harness_path_segment(tenant_id)
        / harness_path_segment(session_id)
    )


def harness_session_workspace_candidates(
    *,
    tenant_id: str,
    session_id: str,
    db: Session | None = None,
    workspace_roots: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Return bounded session roots for cleanup without scanning arbitrary workspace directories.

    ``workspace_roots`` carries snapshots captured before their TaskFrame rows were
    deleted, so post-commit cleanup can still remove the exact historical root.
    """

    snapshot_roots = list(
        _session_workspace_snapshot_roots(
            tenant_id=tenant_id,
            session_id=session_id,
            db=db,
        )
    )
    for workspace_root in workspace_roots:
        value = str(workspace_root or "").strip()
        root = Path(value).expanduser() if value else None
        if root is not None and root not in snapshot_roots:
            snapshot_roots.append(root)
    roots = list(snapshot_roots)
    if not roots:
        roots.append(default_harness_storage_root())
    roots.append(legacy_harness_storage_root())
    session_paths: list[Path] = []
    for root in roots:
        session_path = (
            root
            / harness_path_segment(tenant_id)
            / harness_path_segment(session_id)
        )
        if session_path not in session_paths:
            session_paths.append(session_path)
    return tuple(session_paths)


def _session_workspace_snapshot_roots(
    *,
    tenant_id: str,
    session_id: str,
    db: Session | None,
) -> tuple[Path, ...]:
    """Return distinct persisted roots for one session, preserving stored symlink components for checks."""

    if db is None:
        return ()
    configured_roots = db.exec(
        select(HarnessTaskFrameRecord.workspace_root).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.workspace_root.is_not(None),
        )
    ).all()
    roots: list[Path] = []
    for configured in configured_roots:
        value = str(configured or "").strip()
        root = Path(value).expanduser() if value else None
        if root is not None and root not in roots:
            roots.append(root)
    return tuple(roots)


def harness_task_workspace_path(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    db: Session | None = None,
) -> Path:
    """Return the single write root selected for a TaskFrame without probing the filesystem."""

    return harness_task_workspace_candidates(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )[0]


def harness_task_workspace_candidates(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    db: Session | None = None,
) -> tuple[Path, ...]:
    """Return bounded TaskFrame roots in read order while rejecting symlinked path segments."""

    snapshot_root, has_task_frame = _task_frame_workspace_root(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )
    if snapshot_root is not None:
        roots = [snapshot_root, legacy_harness_storage_root()]
    elif has_task_frame:
        roots = [
            legacy_harness_storage_root(),
            harness_storage_root(tenant_id=tenant_id, db=db),
        ]
    else:
        roots = [harness_storage_root(tenant_id=tenant_id, db=db)]
    task_paths: list[Path] = []
    for root in roots:
        task_path = _task_workspace_path_under_root(
            root=root,
            tenant_id=tenant_id,
            session_id=session_id,
            task_frame_id=task_frame_id,
        )
        if task_path not in task_paths:
            task_paths.append(task_path)
    return tuple(task_paths)


def open_harness_task_artifact(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    path: str,
    db: Session | None = None,
) -> tuple[OpenedHarnessArtifact, Path]:
    """Open one artifact from exactly one bounded TaskFrame root or fail closed on a location conflict."""

    opened_candidates: list[tuple[OpenedHarnessArtifact, Path]] = []
    for workspace_root in harness_task_workspace_candidates(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    ):
        try:
            opened = open_harness_artifact(workspace_root, path)
        except HarnessArtifactAccessError:
            continue
        opened_candidates.append((opened, workspace_root))

    if not opened_candidates:
        raise HarnessArtifactAccessError("Harness artifact is unavailable in its permitted workspace roots.")
    if len(opened_candidates) == 1:
        return opened_candidates[0]

    for opened, _workspace_root in opened_candidates:
        opened.close()
    raise HarnessWorkspaceArtifactConflictError(
        "Harness artifact exists in multiple permitted workspace roots."
    )


def _task_frame_workspace_root(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    db: Session | None,
) -> tuple[Path | None, bool]:
    """Return one durable root snapshot and whether its exact TaskFrame record exists."""

    if db is None:
        return None, False
    frame = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.task_id == task_frame_id,
        )
    ).first()
    configured = str(getattr(frame, "workspace_root", "") or "").strip()
    return (Path(configured).expanduser() if configured else None), frame is not None


def _task_workspace_path_under_root(
    *,
    root: Path,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
) -> Path:
    """Build one isolated TaskFrame path and reject a root or child redirected through a symlink."""

    session_path = (
        root
        / harness_path_segment(tenant_id)
        / harness_path_segment(session_id)
    )
    task_path = session_path / harness_path_segment(task_frame_id)
    for parent in (
        root,
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
    *,
    tenant_id: str,
    session_id: str,
    db: Session | None = None,
    workspace_roots: tuple[str, ...] = (),
) -> bool:
    """Remove exact tenant/session paths from the persisted snapshot and legacy compatibility roots.

    Parent symlinks are rejected so cleanup can never traverse a redirected
    workspace or tenant directory. A symlink at an exact session path is
    unlinked without touching its target.
    """

    removed = False
    for session_path in harness_session_workspace_candidates(
        tenant_id=tenant_id,
        session_id=session_id,
        db=db,
        workspace_roots=workspace_roots,
    ):
        removed = _remove_harness_session_workspace_path(session_path) or removed
    return removed


def _remove_harness_session_workspace_path(session_path: Path) -> bool:
    """Remove one exact session path while leaving a symlink target and sibling sessions untouched."""

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
