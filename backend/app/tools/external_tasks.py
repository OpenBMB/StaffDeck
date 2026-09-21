from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote

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
    if status == 'tracking_blocked':
        return 'tracking_blocked'
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
    from staffdeck_harness.modules.registry import peek_registry
    modular = bool((task.status_config_json or {}).get('_runtime') or db.info.get('staffdeck_registry') or peek_registry())
    next_status = normalize_status(status)
    result_denied = False
    review_failed = False
    if task.status in PERSISTED_TERMINAL_STATUSES and task.status != 'outcome_unknown':
        # A duplicate or late callback must not expose unreviewed raw payloads.
        data = {'ignored': True, 'reason': 'task_already_terminal'}
    elif modular and (
            next_status in PERSISTED_TERMINAL_STATUSES or data.get('result') is not None or data.get('error') is not None):
        from staffdeck_harness.runtime.external_tasks import project_external_result
        from staffdeck_harness.contracts.invocation import ModuleResult
        from staffdeck_harness.contracts.errors import ModuleSdkError
        raw = ModuleResult(success=next_status not in {'failed', 'cancelled', 'expired', 'outcome_unknown'},
            data=data.get('result'), error=data.get('error'))
        try:
            projected = project_external_result(db, task, raw)
            data = {'result':projected.data, 'error':dict(projected.error or {}),
                    'provider_status':next_status, 'result_reviewed':True}
            if raw.success and not projected.success:
                result_denied = True
                next_status = 'tracking_blocked'
                data['error'] = {**dict(projected.error or {}), 'retryable':False,
                    'next_action':'review_result_policy', 'business_replay_allowed':False}
        except Exception as exc:
            # Never persist the callback/provider payload when identity or policy
            # reconstruction failed. A later explicit callback may be revalidated.
            next_status = 'tracking_blocked'
            review_failed = True
            data = {'error': {'code':exc.code if isinstance(exc, ModuleSdkError) else 'RESULT_POLICY_UNAVAILABLE',
                'message':'异步结果未通过当前身份或结果监管，已停止跟踪；不会重放业务调用。',
                'retryable':False, 'business_replay_allowed':False}, 'result_reviewed':False}
    elif modular:
        # Transport progress without a result is not a final capability output.
        # Keep only protocol state; arbitrary Provider fields are not public data.
        data = {'provider_status':next_status}
    # Policy/identity reconstruction may commit its own bookkeeping. Lock and
    # refresh AFTER it, so concurrent callbacks/polls cannot regress a terminal
    # task using a stale Session. A no-op UPDATE is a portable row write lock
    # (SQLite writer serialization, PostgreSQL row lock); no network follows it.
    db.flush()
    db.exec(update(ExternalBusinessTask).where(ExternalBusinessTask.id == task.id,
        ExternalBusinessTask.tenant_id == task.tenant_id).values(id=ExternalBusinessTask.id)
        .execution_options(synchronize_session=False))
    db.refresh(task)
    existing = db.exec(select(ExternalBusinessTaskEvent).where(
        ExternalBusinessTaskEvent.task_id == task.id, ExternalBusinessTaskEvent.event_id == event_id)).first()
    if existing is not None:
        db.commit()
        return False
    if task.status in PERSISTED_TERMINAL_STATUSES and task.status != 'outcome_unknown':
        data = {'ignored':True, 'reason':'task_already_terminal'}
        task.next_poll_at = None
    elif task.status == 'tracking_blocked' and next_status != 'tracking_blocked' and not (
            next_status in PERSISTED_TERMINAL_STATUSES and data.get('result_reviewed') is True):
        # Mere progress never lifts a security/output-policy block.
        next_status = 'tracking_blocked'
        data = {'ignored':True, 'reason':'tracking_requires_reauthorization'}
    elif result_denied or review_failed:
        task.result_json = {}
        task.status_config_json = {**dict(task.status_config_json or {}),
            '_tracking_blocked_from':task.status, '_result_blocked':result_denied}
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
        if task.status == 'tracking_blocked':
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
    if frame.status == "running":
        # The active engine still owns the frame lease and will fold a fast
        # completion into its in-memory result before releasing that lease.
        return
    if frame.status not in {"waiting_external_task", "ready_to_resume"}:
        return
    receipt = (frame.result_json or {}).get("structured_result")
    current_task_id = receipt.get("task_id") if isinstance(receipt, dict) else None
    if current_task_id and current_task_id != task.id:
        # One SOP frame can submit several tasks in successive nodes. An older
        # completion must not resume the node currently waiting on another task.
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
    # Native warm context no longer matches the completed receipt. Cold restore
    # must use this updated public transcript, not the pre-completion session.
    checkpoint["context_revision"] = new_id("extcheckpoint")
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


def poll_due_external_tasks(db: Session, *, dispatch=None) -> int:
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
    queued = db.exec(
        select(ExternalBusinessTask.id)
        .where(
            ExternalBusinessTask.status == "queued",
            ExternalBusinessTask.lease_owner.is_(None),
        )
        .order_by(ExternalBusinessTask.created_at, ExternalBusinessTask.id)
        .limit(20)
    ).all()
    local_count = 0
    for task_id in queued:
        try:
            if dispatch is None:
                task = db.get(ExternalBusinessTask, task_id)
                if task is not None:
                    _execute_local_task(db, task)
                    local_count += 1
            elif dispatch(db, task_id, 'submit'):
                local_count += 1
        except Exception:
            db.rollback()
            import logging

            logging.getLogger(__name__).exception(
                "External task execution failed task=%s", task_id
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
        if dispatch is not None:
            claimed += bool(dispatch(db, task.id, 'poll'))
            continue
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
                lease_expires_at=now + timedelta(seconds=3660),
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
    return claimed + local_count + len(expired) + recovered


def execute_external_task(db: Session, task_id: str, phase: str) -> None:
    """Worker entry: independent transaction, live status and conditional DB claim."""
    task = db.get(ExternalBusinessTask, task_id)
    if task is None:
        return
    now = utc_now()
    if task.expires_at is not None and task.expires_at.replace(tzinfo=None) <= now.replace(tzinfo=None):
        return  # scanner owns timeout transitions
    if phase == 'submit':
        if task.status == 'queued' and task.lease_owner is None:
            _execute_local_task(db, task)
        return
    if phase != 'poll' or task.status not in {'accepted', 'working'}:
        return
    owner = new_id('exttasklease')
    claimed = db.exec(update(ExternalBusinessTask).where(
        ExternalBusinessTask.id == task.id, ExternalBusinessTask.tenant_id == task.tenant_id,
        ExternalBusinessTask.status.in_(['accepted','working']),
        ExternalBusinessTask.next_poll_at <= now,
        or_(ExternalBusinessTask.lease_owner.is_(None), ExternalBusinessTask.lease_expires_at <= now)
    ).values(lease_owner=owner, lease_expires_at=now+timedelta(seconds=3660), updated_at=now)
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.rollback()
        return
    db.commit()
    db.refresh(task)
    _poll_task(db, task, owner=owner)


def _execute_local_task(db: Session, task: ExternalBusinessTask) -> None:
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
            lease_expires_at=now + timedelta(seconds=3660),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(claimed, "rowcount", 0) != 1:
        db.rollback()
        return
    db.commit()
    task = db.get(ExternalBusinessTask, task.id)
    request_started = False
    try:
        if task is None:
            raise ValueError("Detached task no longer exists")
        from app.tools.tool_executor import ToolExecutor

        from staffdeck_harness.runtime.external_tasks import resolve_task_tool
        with resolve_task_tool(db, task) as tool:
            request_started = True
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
                if strategy == PROVIDER_TASK_STRATEGY and request_started
                else "failed"
            ),
            data={"error": {"code": getattr(exc, "code", "DETACHED_EXECUTION_ERROR"), "message": str(exc)}},
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
    config = dict(task.status_config_json or {})
    status_field = str(config.get("status_field") or "status")
    result_field = str(config.get("result_field") or "result")
    status_mapping = dict(config.get("status_mapping") or {})
    url = (task.status_url or "").replace("{taskId}", quote(task.external_task_id or "", safe=""))
    task.poll_attempts += 1
    task.next_poll_at = utc_now() + timedelta(seconds=task.poll_interval_seconds)
    try:
        from app.tools.tool_executor import ToolExecutor

        from staffdeck_harness.runtime.external_tasks import resolve_task_tool
        with resolve_task_tool(db, task) as tool:
            if not task.external_task_id or not task.status_url:
                raise ValueError("Provider tracking identity is missing")
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
        from staffdeck_harness.contracts.errors import ModuleSdkError
        db.flush()
        db.exec(update(ExternalBusinessTask).where(ExternalBusinessTask.id == task.id,
            ExternalBusinessTask.tenant_id == task.tenant_id).values(id=ExternalBusinessTask.id)
            .execution_options(synchronize_session=False))
        db.refresh(task)
        if task.status in PERSISTED_TERMINAL_STATUSES:
            # A callback may have completed while this GET was failing.
            return
        module_error = isinstance(exc, ModuleSdkError)
        permanent = module_error and exc.code not in {'AUTHORIZATION_UNAVAILABLE', 'ENGINE_UNAVAILABLE',
            'BUSINESS_CATALOG_UNAVAILABLE', 'BUSINESS_DIRECTORY_UNAVAILABLE'}
        if permanent:
            task.status_config_json = {**dict(task.status_config_json or {}),
                '_tracking_blocked_from': task.status}
            task.status = 'tracking_blocked'
            task.next_poll_at = None
        task.error_json = {'code':exc.code if module_error else 'POLL_ERROR',
            'message':exc.message if module_error else '外部任务状态查询暂不可用，稍后重试。',
            'retryable':not permanent, 'business_replay_allowed':False,
            'next_action':'restore_module_or_permission' if permanent else 'retry_status_query'}
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
