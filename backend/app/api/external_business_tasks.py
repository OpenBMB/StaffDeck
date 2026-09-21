from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ExternalBusinessTask, ExternalBusinessTaskEvent, User
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.tools.external_tasks import apply_task_event, verify_callback_token

enterprise_router = APIRouter(
    prefix="/api/enterprise/external-business-tasks",
    tags=["enterprise:external-business-tasks"],
)
callback_router = APIRouter(
    prefix="/api/external-business-tasks",
    tags=["external-business-tasks"],
)


class ExternalTaskCallback(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(default="status", max_length=100)
    status: Literal[
        "accepted", "submitted", "queued", "pending", "working", "processing",
        "completed", "succeeded", "success", "failed", "cancelled", "canceled",
    ]
    task_id: str | None = Field(default=None, max_length=300)
    result: Any = None
    error: Any = None
    data: dict[str, Any] = Field(default_factory=dict)


def task_read(task: ExternalBusinessTask, db: Session) -> dict[str, Any]:
    from app.tools.external_tasks import task_result_envelope
    envelope = task_result_envelope(db, task)
    events = db.exec(
        select(ExternalBusinessTaskEvent)
        .where(ExternalBusinessTaskEvent.task_id == task.id)
        .order_by(ExternalBusinessTaskEvent.created_at)
    ).all()
    return {
        "id": task.id,
        "external_task_id": task.external_task_id,
        "tool_id": task.tool_id,
        "agent_id": task.agent_id,
        "session_id": task.session_id,
        "status": task.status,
        "result": task.result_json or {},
        "module_result": envelope,
        "citations": envelope.get('citations') or [],
        "artifacts": envelope.get('artifacts') or [],
        "extensions": envelope.get('extensions') or {},
        "error": task.error_json or {},
        "poll_attempts": task.poll_attempts,
        "created_at": task.created_at.isoformat(),
        "accepted_at": task.accepted_at.isoformat() if task.accepted_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "updated_at": task.updated_at.isoformat(),
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "data": event.data_json,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
    }


@enterprise_router.get("/{external_task_id}")
def get_external_business_task(
    external_task_id: str,
    tenant_id: str = Query(...),
    tool_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_current_user_tenant(tenant_id, current_user)
    statement = select(ExternalBusinessTask).where(
        ExternalBusinessTask.tenant_id == tenant_id,
        ExternalBusinessTask.user_id == current_user.id,
        or_(
            ExternalBusinessTask.id == external_task_id,
            ExternalBusinessTask.external_task_id == external_task_id,
        ),
    )
    if tool_id:
        statement = statement.where(ExternalBusinessTask.tool_id == tool_id)
    rows = db.exec(statement).all()
    if not rows:
        raise HTTPException(status_code=404, detail="External business task not found")
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="tool_id is required for this external task id")
    return task_read(rows[0], db)


@enterprise_router.post('/{external_task_id}/resume-tracking')
def resume_external_task_tracking(external_task_id: str, tenant_id: str = Query(...),
        db: Session = Depends(get_session), current_user: User = Depends(get_current_user)):
    """Explicitly reauthorize status tracking, never resubmit the original business request."""
    ensure_current_user_tenant(tenant_id, current_user)
    task = db.get(ExternalBusinessTask, external_task_id)
    if task is None or (task.tenant_id, task.user_id) != (tenant_id, current_user.id):
        raise HTTPException(404, 'External business task not found')
    if task.status != 'tracking_blocked' or not task.external_task_id or not task.status_url:
        raise HTTPException(409, '只能恢复有供应商任务 ID 的已暂停状态跟踪，不会重放提交')
    if (task.status_config_json or {}).get('_result_blocked'):
        raise HTTPException(409, '结果监管未通过，需要审核结果策略并由供应商重新回调，不能自动重放')
    from staffdeck_harness.runtime.external_tasks import resolve_task_tool
    from staffdeck_harness.contracts.errors import ModuleSdkError
    from app.db.models import utc_now
    try:
        with resolve_task_tool(db, task):
            pass
    except ModuleSdkError as exc:
        raise HTTPException(409, exc.to_dict()) from None
    from sqlalchemy import update
    db.exec(update(ExternalBusinessTask).where(ExternalBusinessTask.id == task.id,
        ExternalBusinessTask.tenant_id == tenant_id).values(id=ExternalBusinessTask.id)
        .execution_options(synchronize_session=False))
    db.refresh(task)
    if task.status != 'tracking_blocked' or (task.status_config_json or {}).get('_result_blocked'):
        db.rollback()
        raise HTTPException(409, '任务状态已变化，请刷新；没有重新提交业务请求')
    task.status = 'accepted'
    task.next_poll_at = utc_now()
    task.error_json = {}
    db.add(task)
    db.commit()
    return task_read(task, db)


@callback_router.post("/{task_id}/callback")
def external_business_task_callback(
    task_id: str,
    request: ExternalTaskCallback,
    callback_token: str = Header(default="", alias="X-StaffDeck-Callback-Token"),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    from staffdeck_harness.modules.registry import peek_registry
    if peek_registry() is not None:
        from staffdeck_harness.runtime.services import maintenance_sessions
        # Resolve only through the currently selected storage module. An opaque
        # task ID plus the per-task secret is the callback authority, not a user
        # or tenant claim supplied by the Provider.
        for runtime_db in maintenance_sessions():
            task = runtime_db.get(ExternalBusinessTask, task_id)
            if task is not None and verify_callback_token(task, callback_token):
                return _apply_callback(runtime_db, task, request)
        raise HTTPException(status_code=401, detail="Invalid callback credential")
    task = db.get(ExternalBusinessTask, task_id)
    if task is None or not verify_callback_token(task, callback_token):
        raise HTTPException(status_code=401, detail="Invalid callback credential")
    return _apply_callback(db, task, request)


def _apply_callback(db, task, request):
    if request.task_id and request.task_id != task.external_task_id:
        raise HTTPException(
            status_code=409,
            detail="Provider task id does not match callback target",
        )
    data = dict(request.data)
    if request.result is not None:
        data["result"] = request.result
    if request.error is not None:
        data["error"] = request.error
    created = apply_task_event(
        db,
        task,
        event_id=request.event_id,
        event_type=request.event_type,
        status=request.status,
        data=data,
    )
    return {"accepted": True, "duplicate": not created, "status": task.status}
