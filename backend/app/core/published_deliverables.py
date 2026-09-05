from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.db.models import HarnessTaskFrameRecord, Message
from app.harness import HarnessArtifactAccessError, normalize_harness_artifact_path


MAX_PUBLISHED_DELIVERABLES = 20


def list_published_deliverables(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    limit: int = MAX_PUBLISHED_DELIVERABLES,
    query: str = "",
    exclude_task_frame_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded latest-first inventory of published workspace artifacts."""

    bounded_limit = max(1, min(int(limit), MAX_PUBLISHED_DELIVERABLES))
    frame_ids = set(
        db.exec(
            select(HarnessTaskFrameRecord.task_id).where(
                HarnessTaskFrameRecord.tenant_id == tenant_id,
                HarnessTaskFrameRecord.session_id == session_id,
            )
        ).all()
    )
    keyword = str(query or "").strip().casefold()
    rows = db.exec(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.desc())
    ).all()
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for row in rows:
        artifacts = (row.metadata_json or {}).get("harness_artifacts")
        if not isinstance(artifacts, list):
            continue
        for raw in reversed(artifacts):
            item = _published_item(raw, frame_ids=frame_ids)
            if item is None or item["task_frame_id"] == exclude_task_frame_id:
                continue
            searchable = " ".join(
                str(item.get(key) or "")
                for key in ("display_name", "path", "description")
            ).casefold()
            if keyword and keyword not in searchable:
                continue
            dedupe_key = str(item["display_name"]).strip().casefold()
            if dedupe_key in seen_names:
                continue
            seen_names.add(dedupe_key)
            item["message_id"] = row.id
            item["published_at"] = row.created_at.isoformat()
            result.append(item)
            if len(result) >= bounded_limit:
                return result
    return result


def find_published_deliverable(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    path: str,
) -> dict[str, Any] | None:
    try:
        requested_path = normalize_harness_artifact_path(path)
    except HarnessArtifactAccessError:
        return None
    frame = db.exec(
        select(HarnessTaskFrameRecord.id).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.task_id == task_frame_id,
        )
    ).first()
    if frame is None:
        return None
    rows = db.exec(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.desc())
    ).all()
    for row in rows:
        artifacts = (row.metadata_json or {}).get("harness_artifacts")
        if not isinstance(artifacts, list):
            continue
        for raw in reversed(artifacts):
            item = _published_item(raw, frame_ids={task_frame_id})
            if item is None:
                continue
            if item["task_frame_id"] == task_frame_id and item["path"] == requested_path:
                item["message_id"] = row.id
                item["published_at"] = row.created_at.isoformat()
                return item
    return None


def _published_item(
    raw: object,
    *,
    frame_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("type") != "workspace_file":
        return None
    task_frame_id = str(raw.get("task_frame_id") or "").strip()
    if not task_frame_id or task_frame_id not in frame_ids:
        return None
    try:
        path = normalize_harness_artifact_path(str(raw.get("path") or ""))
    except HarnessArtifactAccessError:
        return None
    display_name = str(raw.get("display_name") or Path(path).name).strip() or Path(path).name
    return {
        "task_frame_id": task_frame_id,
        "path": path,
        "display_name": display_name[:180],
        "description": str(raw.get("description") or "").strip()[:500] or None,
        "content_type": str(raw.get("content_type") or "").strip() or None,
        "size": raw.get("size") if isinstance(raw.get("size"), int) else None,
        "sha256": str(raw.get("sha256") or "").strip().lower() or None,
    }


__all__ = [
    "MAX_PUBLISHED_DELIVERABLES",
    "find_published_deliverable",
    "list_published_deliverables",
]
