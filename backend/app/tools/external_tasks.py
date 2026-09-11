from __future__ import annotations

import hashlib
import hmac
import secrets
import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    ExternalBusinessTask,
    ExternalBusinessTaskEvent,
    HarnessAgentLoopRecord,
    HarnessTaskFrameRecord,
    Tool,
    new_id,
    utc_now,
)

TERMINAL_STATUSES = {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}
SUCCESS_STATUSES = {"completed", "succeeded", "success"}
PERSISTED_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "expired",
    "outcome_unknown",
}
PROVIDER_TASK_STRATEGY = "provider_task"
CALLBACK_URL_HEADER = "X-StaffDeck-Callback-URL"
CALLBACK_TOKEN_HEADER = "X-StaffDeck-Callback-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"
logger = logging.getLogger(__name__)


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
    if status == "outcome_unknown":
        return "outcome_unknown"
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
        and task.status != "outcome_unknown"
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
    if task.status == "completed":
        task.error_json = {}
    if task.status in PERSISTED_TERMINAL_STATUSES:
        task.finished_at = now
        task.next_poll_at = None
    else:
        task.finished_at = None
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
    if frame.status == "running":
        # The active engine still owns the frame lease and will fold a fast
        # completion into its in-memory result before releasing that lease.
        return
    if frame.status not in {"waiting_external_task", "ready_to_resume"}:
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
    update_external_task_checkpoint(db, frame, task)
    if frame.kind == "sop":
        frame.status = "ready_to_resume"
        if task.status == "completed" and task.resume_step_id:
            frame.step_id = task.resume_step_id
    else:
        frame.status = "completed" if task.status == "completed" else "failed"
    frame.lease_owner = None
    frame.lease_expires_at = None
    frame.updated_at = utc_now()
    frame.state_version += 1
    db.add(frame)
    db.commit()


def update_external_task_checkpoint(
    db: Session,
    frame: HarnessTaskFrameRecord,
    task: ExternalBusinessTask,
) -> None:
    if not frame.agent_loop_id:
        return
    loop = db.get(HarnessAgentLoopRecord, frame.agent_loop_id)
    if loop is None:
        return
    task_result = {
        "success": task.status == "completed",
        "data": {
            "detached": True,
            "task_id": task.id,
            "provider_task_id": task.external_task_id,
            "status": task.status,
            "result": dict(task.result_json or {}),
            "error": dict(task.error_json or {}),
        },
        "error": dict(task.error_json or {}) or None,
    }
    checkpoint = dict(loop.checkpoint_json or {})
    transcript = [dict(item) for item in checkpoint.get("transcript") or []]
    updated = False
    for item in reversed(transcript):
        result = item.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if (
            item.get("role") == "tool"
            and isinstance(data, dict)
            and data.get("task_id") == task.id
        ):
            item["result"] = task_result
            updated = True
            break
    if not updated:
        transcript.append(
            {
                "role": "tool",
                "tool_name": "external_task_status",
                "result": task_result,
            }
        )
    checkpoint["transcript"] = transcript
    capability_results = [
        dict(item) for item in checkpoint.get("capability_results") or []
    ]
    for item in reversed(capability_results):
        data = item.get("data")
        if isinstance(data, dict) and data.get("task_id") == task.id:
            item["success"] = task_result["success"]
            item["data"] = task_result["data"]
            item["error"] = task_result["error"]
            break
    checkpoint["capability_results"] = capability_results
    checkpoint["external_task_result"] = task_result["data"]
    loop.checkpoint_json = checkpoint
    loop.updated_at = utc_now()
    loop.state_version = max(1, int(loop.state_version or 0) + 1)
    db.add(loop)


def poll_due_external_tasks(db: Session) -> int:
    now = utc_now()
    stale_tasks = db.exec(
        select(ExternalBusinessTask).where(
            ExternalBusinessTask.status.in_(["submitting", "working"]),
            ExternalBusinessTask.lease_expires_at.is_not(None),
            ExternalBusinessTask.lease_expires_at <= now,
        )
    ).all()
    recovered = 0
    for task in stale_tasks:
        if (
            _task_strategy(task) == PROVIDER_TASK_STRATEGY
            and task.external_task_id
            and task.status_url
        ):
            task.status = "working"
            task.next_poll_at = now
        else:
            task.status = "outcome_unknown"
            task.error_json = {
                "code": "DETACHED_OUTCOME_UNKNOWN",
                "message": (
                    "The worker stopped after the external request may have been sent; "
                    "StaffDeck will not replay a potentially non-idempotent request."
                ),
            }
            task.finished_at = now
            task.next_poll_at = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = now
        db.add(task)
        db.commit()
        if task.status in PERSISTED_TERMINAL_STATUSES:
            _prepare_sop_resume(db, task)
        recovered += 1
    expired = db.exec(
        select(ExternalBusinessTask).where(
            ExternalBusinessTask.status.in_(["queued", "accepted", "working", "submitting"]),
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
        try:
            _poll_task(db, task, owner=owner)
        except Exception:
            db.rollback()
            logger.exception("External task polling failed outside provider transport")
        claimed += 1
    # Poll previously accepted jobs first; a backlog of slow submissions must not
    # delay every existing Provider status query until after the entire batch.
    queued = db.exec(select(ExternalBusinessTask.id).where(
        ExternalBusinessTask.status == "queued",
        ExternalBusinessTask.lease_owner.is_(None),
    ).order_by(ExternalBusinessTask.created_at, ExternalBusinessTask.id).limit(20)).all()
    local_count = 0
    for task_id in queued:
        try:
            task = db.get(ExternalBusinessTask, task_id)
            if task is not None:
                _execute_local_task(db, task)
                local_count += 1
        except Exception:
            db.rollback()
            logger.exception("External task execution failed task=%s", task_id)
    return claimed + local_count + len(expired) + recovered


def _execute_local_task(db: Session, task: ExternalBusinessTask) -> None:
    task_id = task.id
    owner = new_id("exttasklease")
    now = utc_now()
    strategy = _task_strategy(task)
    claimed = db.exec(
        update(ExternalBusinessTask)
        .where(
            ExternalBusinessTask.id == task.id,
            ExternalBusinessTask.status == "queued",
            ExternalBusinessTask.lease_owner.is_(None),
        )
        .values(
            status=("submitting" if strategy == PROVIDER_TASK_STRATEGY else "working"),
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

        if strategy == PROVIDER_TASK_STRATEGY:
            _submit_provider_task(db, task, tool)
        else:
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
        db.rollback()
        task = db.get(ExternalBusinessTask, task_id)
        if task is None:
            logger.exception("Detached task disappeared during execution task=%s", task_id)
            return
        if strategy == PROVIDER_TASK_STRATEGY and task.external_task_id and task.status_url:
            # A durable provider receipt means submission must not be repeated.
            if task.status not in PERSISTED_TERMINAL_STATUSES:
                task.status = "working"
                task.next_poll_at = utc_now()
                db.add(task)
                db.commit()
            logger.exception("Provider task tracking interrupted task=%s", task_id)
            return
        apply_task_event(
            db,
            task,
            event_id=(
                f"submission-{task.id}"
                if strategy == PROVIDER_TASK_STRATEGY
                else f"local-{task.id}"
            ),
            event_type="submission_failed",
            status=(
                "outcome_unknown"
                if strategy == PROVIDER_TASK_STRATEGY
                else "failed"
            ),
            data={"error": {"code": "DETACHED_EXECUTION_ERROR", "message": str(exc)}},
        )
    finally:
        if not db.is_active:
            db.rollback()
        db.exec(
            update(ExternalBusinessTask)
            .where(
                ExternalBusinessTask.id == task_id,
                ExternalBusinessTask.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()


def _submit_provider_task(db: Session, task: ExternalBusinessTask, tool: Tool) -> None:
    from app.config import get_settings
    from app.tools.tool_executor import ToolExecutor

    callback_token = new_callback_token()
    task.callback_token_hash = callback_token_hash(callback_token)
    task.updated_at = utc_now()
    db.add(task)
    db.commit()

    headers = {IDEMPOTENCY_HEADER: str(task.idempotency_key or task.id)}
    callback_base_url = get_settings().external_task_callback_base_url.strip().rstrip("/")
    if callback_base_url:
        headers[CALLBACK_URL_HEADER] = (
            f"{callback_base_url}/api/external-business-tasks/{task.id}/callback"
        )
        headers[CALLBACK_TOKEN_HEADER] = callback_token

    response = ToolExecutor(db).execute_http_with_metadata(
        tool,
        task.request_json,
        additional_headers=headers,
    )
    if not response.result.success:
        error = (
            response.result.error.model_dump(mode="json")
            if response.result.error
            else {"code": "PROVIDER_SUBMISSION_FAILED"}
        )
        uncertain = str(error.get("code") or "") in {"TIMEOUT", "EXECUTION_ERROR"}
        apply_task_event(
            db,
            task,
            event_id=f"submission-{task.id}",
            event_type="submission_failed",
            status="outcome_unknown" if uncertain else "failed",
            data={"error": error},
        )
        return

    payload = response.result.data
    data = dict(payload) if isinstance(payload, dict) else {"result": payload}
    config = dict(task.status_config_json or {})
    provider_task_id = _json_path(payload, str(config.get("task_id_field") or "taskId"))
    raw_status = _json_path(payload, str(config.get("status_field") or "status"))
    status_mapping = dict(config.get("status_mapping") or {})
    if provider_task_id is not None:
        if isinstance(provider_task_id, bool) or not isinstance(provider_task_id, (str, int)):
            raise ValueError("Provider task ID must be a string or integer")
        elif not str(provider_task_id).strip():
            raise ValueError("Provider task ID cannot be empty")
    if raw_status is None:
        raw_status = "accepted" if response.status_code == 202 or provider_task_id is not None else "completed"
    mapped_status = status_mapping.get(str(raw_status), raw_status)
    next_status = normalize_status(mapped_status)

    if provider_task_id is None and next_status not in PERSISTED_TERMINAL_STATUSES:
        apply_task_event(
            db,
            task,
            event_id=f"submission-{task.id}",
            event_type="submission_failed",
            status="failed",
            data={
                "error": {
                    "code": "PROVIDER_TASK_ID_MISSING",
                    "message": "Provider accepted an async task without returning its task ID.",
                }
            },
        )
        return

    if provider_task_id is not None:
        task.external_task_id = str(provider_task_id)
    result_field = str(config.get("result_field") or "result")
    result_data = _json_path(payload, result_field)
    if result_data is not None:
        data["result"] = result_data
    if next_status not in PERSISTED_TERMINAL_STATUSES and task.status_url:
        task.next_poll_at = utc_now() + timedelta(seconds=task.poll_interval_seconds)
    db.add(task)
    apply_task_event(
        db,
        task,
        event_id=f"submission-{task.id}",
        event_type="submitted",
        status=next_status,
        data=data,
    )


def _task_strategy(task: ExternalBusinessTask) -> str:
    config = task.status_config_json if isinstance(task.status_config_json, dict) else {}
    strategy = str(config.get("async_strategy") or "staffdeck_worker")
    return strategy if strategy == PROVIDER_TASK_STRATEGY else "staffdeck_worker"


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
        db.rollback()
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
