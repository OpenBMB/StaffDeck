from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    ExternalBusinessTask,
    ExternalBusinessTaskEvent,
    HarnessTaskFrameRecord,
    Tool,
    new_id,
    utc_now,
)

TERMINAL_STATUSES = {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}
SUCCESS_STATUSES = {"completed", "succeeded", "success"}
PERSISTED_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}


def new_callback_token() -> str:
    return secrets.token_urlsafe(32)


def callback_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_callback_token(task: ExternalBusinessTask, token: str) -> bool:
    return bool(token) and hmac.compare_digest(task.callback_token_hash, callback_token_hash(token))


def normalize_status(value: object) -> str:
    status = str(value or "working").strip().lower()
    if status in SUCCESS_STATUSES:
        return "completed"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status == "failed":
        return "failed"
    if status == "expired":
        return "expired"
    if status in {"accepted", "submitted", "queued", "pending"}:
        return "accepted"
    return "working"


def apply_task_event(
    db: Session,
    task: ExternalBusinessTask,
    *,
    event_id: str,
    event_type: str,
    status: object,
    data: dict[str, Any],
) -> bool:
    existing = db.exec(
        select(ExternalBusinessTaskEvent).where(
            ExternalBusinessTaskEvent.task_id == task.id,
            ExternalBusinessTaskEvent.event_id == event_id,
        )
    ).first()
    if existing is not None:
        return False
    event = ExternalBusinessTaskEvent(
        tenant_id=task.tenant_id,
        task_id=task.id,
        event_id=event_id,
        event_type=event_type,
        data_json=data,
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False

    now = utc_now()
    next_status = normalize_status(status)
    if (
        task.status in PERSISTED_TERMINAL_STATUSES
        and next_status not in PERSISTED_TERMINAL_STATUSES
    ):
        db.commit()
        return True
    task.status = next_status
    result = data.get("result")
    error = data.get("error")
    if isinstance(result, dict):
        task.result_json = result
    elif result is not None:
        task.result_json = {"value": result}
    if isinstance(error, dict):
        task.error_json = error
    elif error is not None:
        task.error_json = {"message": str(error)}
    if task.status in PERSISTED_TERMINAL_STATUSES:
        task.finished_at = now
        task.next_poll_at = None
    task.updated_at = now
    db.add(task)
    db.commit()
    if task.status in PERSISTED_TERMINAL_STATUSES:
        _prepare_sop_resume(db, task)
    return True


def _prepare_sop_resume(db: Session, task: ExternalBusinessTask) -> None:
    if not task.task_frame_id or task.status not in PERSISTED_TERMINAL_STATUSES:
        return
    frame = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == task.tenant_id,
            HarnessTaskFrameRecord.session_id == task.session_id,
            HarnessTaskFrameRecord.task_id == task.task_frame_id,
        )
    ).first()
    if frame is None:
        return
    if frame.status in {"completed", "cancelled", "failed"}:
        return
    result = dict(task.result_json or {})
    frame.result_json = {
        **dict(frame.result_json or {}),
        "external_task_id": task.external_task_id,
        "external_task_status": task.status,
        "external_task_result": result,
    }
    frame.slots_json = {**dict(frame.slots_json or {}), **result}
    if task.error_json:
        frame.error_json = dict(task.error_json)
    frame.status = "ready_to_resume"
    if task.resume_step_id:
        frame.step_id = task.resume_step_id
    frame.lease_owner = None
    frame.lease_expires_at = None
    frame.updated_at = utc_now()
    frame.state_version += 1
    db.add(frame)
    db.commit()


def poll_due_external_tasks(db: Session) -> int:
    now = utc_now()
    db.exec(
        update(ExternalBusinessTask)
        .where(
            ExternalBusinessTask.status == "working",
            ExternalBusinessTask.lease_expires_at.is_not(None),
            ExternalBusinessTask.lease_expires_at <= now,
        )
        .values(status="queued", lease_owner=None, lease_expires_at=None, updated_at=now)
    )
    db.commit()
    queued = db.exec(
        select(ExternalBusinessTask)
        .where(
            ExternalBusinessTask.status == "queued",
            ExternalBusinessTask.lease_owner.is_(None),
        )
        .limit(20)
    ).all()
    local_count = 0
    for task in queued:
        _execute_local_task(db, task)
        local_count += 1
    expired = db.exec(
        select(ExternalBusinessTask).where(
            ExternalBusinessTask.status.in_(["accepted", "working"]),
            ExternalBusinessTask.expires_at.is_not(None),
            ExternalBusinessTask.expires_at <= now,
        )
    ).all()
    for task in expired:
        apply_task_event(
            db,
            task,
            event_id=f"expired-{int(now.timestamp())}",
            event_type="expired",
            status="expired",
            data={
                "error": {
                    "code": "TASK_TRACKING_EXPIRED",
                    "message": "External task tracking exceeded its configured deadline.",
                }
            },
        )
    candidates = db.exec(
        select(ExternalBusinessTask).where(
            ExternalBusinessTask.status.in_(["accepted", "working"]),
            ExternalBusinessTask.status_url.is_not(None),
            ExternalBusinessTask.next_poll_at.is_not(None),
            ExternalBusinessTask.next_poll_at <= now,
            or_(
                ExternalBusinessTask.lease_owner.is_(None),
                ExternalBusinessTask.lease_expires_at <= now,
            ),
        )
        .limit(20)
    ).all()
    claimed = 0
    for task in candidates:
        owner = new_id("exttasklease")
        result = db.exec(
            update(ExternalBusinessTask)
            .where(
                ExternalBusinessTask.id == task.id,
                ExternalBusinessTask.status.in_(["accepted", "working"]),
                or_(
                    ExternalBusinessTask.lease_owner.is_(None),
                    ExternalBusinessTask.lease_expires_at <= now,
                ),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=60),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            db.rollback()
            continue
        db.commit()
        task = db.get(ExternalBusinessTask, task.id)
        if task is None:
            continue
        _poll_task(db, task, owner=owner)
        claimed += 1
    return claimed + local_count + len(expired)


def _execute_local_task(db: Session, task: ExternalBusinessTask) -> None:
    owner = new_id("exttasklease")
    now = utc_now()
    claimed = db.exec(
        update(ExternalBusinessTask)
        .where(
            ExternalBusinessTask.id == task.id,
            ExternalBusinessTask.status == "queued",
            ExternalBusinessTask.lease_owner.is_(None),
        )
        .values(
            status="working",
            lease_owner=owner,
            lease_expires_at=now + timedelta(hours=1),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(claimed, "rowcount", 0) != 1:
        db.rollback()
        return
    db.commit()
    task = db.get(ExternalBusinessTask, task.id)
    tool = db.get(Tool, task.tool_id) if task else None
    try:
        if task is None or tool is None:
            raise ValueError("Detached tool no longer exists")
        from app.tools.tool_executor import ToolExecutor

        result = ToolExecutor(db).execute_sync_http(tool, task.request_json)
        if result.success:
            apply_task_event(
                db,
                task,
                event_id=f"local-{task.id}",
                event_type="completed",
                status="completed",
                data={"result": result.data},
            )
        else:
            error = result.error.model_dump(mode="json") if result.error else {}
            apply_task_event(
                db,
                task,
                event_id=f"local-{task.id}",
                event_type="failed",
                status="failed",
                data={"error": error},
            )
    except Exception as exc:
        apply_task_event(
            db,
            task,
            event_id=f"local-{task.id}",
            event_type="failed",
            status="failed",
            data={"error": {"code": "DETACHED_EXECUTION_ERROR", "message": str(exc)}},
        )
    finally:
        db.exec(
            update(ExternalBusinessTask)
            .where(
                ExternalBusinessTask.id == task.id,
                ExternalBusinessTask.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()


def _poll_task(db: Session, task: ExternalBusinessTask, *, owner: str) -> None:
    tool = db.get(Tool, task.tool_id)
    if tool is None or not task.external_task_id or not task.status_url:
        return
    config = dict(task.status_config_json or {})
    status_field = str(config.get("status_field") or "status")
    result_field = str(config.get("result_field") or "result")
    status_mapping = dict(config.get("status_mapping") or {})
    url = task.status_url.replace("{taskId}", task.external_task_id)
    task.poll_attempts += 1
    task.next_poll_at = utc_now() + timedelta(seconds=task.poll_interval_seconds)
    try:
        from app.tools.tool_executor import ToolExecutor

        executor = ToolExecutor(db)
        if url.startswith("/"):
            url = f"{executor.settings.normalized_tool_base_url}{url}"
        headers = executor._request_headers(
            url,
            executor._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        response = httpx.get(
            url,
            headers=headers,
            timeout=executor._execution_policy(tool).timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("status response must be a JSON object")
        raw_status = _json_path(payload, status_field)
        mapped_status = status_mapping.get(str(raw_status), raw_status)
        result_data = _json_path(payload, result_field)
        event_data = dict(payload)
        if result_data is not None:
            event_data["result"] = result_data
        apply_task_event(
            db,
            task,
            event_id=f"poll-{task.poll_attempts}",
            event_type="polled",
            status=mapped_status,
            data=event_data,
        )
    except Exception as exc:
        task.error_json = {"code": "POLL_ERROR", "message": str(exc)}
        task.updated_at = utc_now()
        db.add(task)
        db.commit()
    finally:
        db.exec(
            update(ExternalBusinessTask)
            .where(
                ExternalBusinessTask.id == task.id,
                ExternalBusinessTask.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()


def _json_path(value: Any, path: str) -> Any:
    current = value
    for part in (item for item in path.split(".") if item):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current
