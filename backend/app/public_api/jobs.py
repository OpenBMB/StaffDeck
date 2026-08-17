from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
import json
from time import sleep
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.async_jobs import enqueue_async_job
from app.config import get_settings
from app.db import engine, get_session
from app.db.models import (
    APIIdempotencyRecord,
    APIJob,
    APIJobEvent,
    AgentEvent,
    ChatSession,
    WebhookDelivery,
    new_id,
    utc_now,
)
from app.public_api.auth import PublicPrincipal, get_public_principal
from app.public_api.errors import PublicAPIError
from app.public_api.schemas import JobRead
from app.public_api.webhooks import (
    enqueue_webhook_deliveries,
    stage_webhook_deliveries,
)


router = APIRouter(prefix="/jobs", tags=["jobs"])
JobHandler = Callable[[Session, APIJob], dict[str, Any]]
_handlers: dict[str, JobHandler] = {}


def register_job_handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(handler: JobHandler) -> JobHandler:
        _handlers[kind] = handler
        return handler

    return decorator


def job_read(row: APIJob) -> JobRead:
    return JobRead(
        id=row.id,
        kind=row.kind,
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        agent_id=row.agent_id,
        session_id=row.session_id,
        retryable=row.retryable,
        error=dict(row.error_json or {}),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


def create_job(
    db: Session,
    principal: PublicPrincipal,
    *,
    kind: str,
    request_payload: dict[str, Any],
    agent_id: str | None = None,
) -> APIJob:
    if not principal.credential_id:
        raise PublicAPIError(403, "API_KEY_REQUIRED", "Jobs require an API credential.")
    row = APIJob(
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        agent_id=agent_id,
        kind=kind,
        request_json=request_payload,
    )
    db.add(row)
    db.flush()
    emit_job_event(db, row, "job.queued", {"job_id": row.id, "kind": kind})
    db.commit()
    db.refresh(row)
    enqueue_async_job(f"public_api.{kind}", run_job, row.id)
    return row


def create_internal_job(
    db: Session,
    *,
    tenant_id: str,
    kind: str,
    request_payload: dict[str, Any],
    agent_id: str | None = None,
) -> APIJob:
    """Persist an application-owned job before offering it to the executor.

    Internal callers deliberately use the same durable job/receipt machinery as
    the public API. If the executor is stopping, the queued row remains
    recoverable on the next startup instead of disappearing with the process.
    """

    row = APIJob(
        tenant_id=tenant_id,
        credential_id="internal",
        agent_id=agent_id,
        kind=kind,
        request_json=request_payload,
    )
    db.add(row)
    db.flush()
    emit_job_event(db, row, "job.queued", {"job_id": row.id, "kind": kind}, public=False)
    db.commit()
    db.refresh(row)
    try:
        enqueue_async_job(f"internal.{kind}", run_job, row.id)
    except RuntimeError:
        # The durable queued row is intentionally retained. Startup recovery
        # will submit it to the replacement executor.
        pass
    return row


def emit_job_event(
    db: Session,
    job: APIJob,
    event_type: str,
    data: dict[str, Any],
    *,
    public: bool = True,
) -> APIJobEvent:
    latest = db.exec(
        select(APIJobEvent)
        .where(APIJobEvent.job_id == job.id)
        .order_by(APIJobEvent.sequence.desc())
    ).first()
    event = APIJobEvent(
        tenant_id=job.tenant_id,
        job_id=job.id,
        sequence=(latest.sequence + 1) if latest else 1,
        event_type=event_type,
        data_json=data,
        public=public,
    )
    db.add(event)
    db.flush()
    payload = {
        "id": event.id,
        "type": event_type,
        "created_at": event.created_at.isoformat() + "Z",
        "data": {"job_id": job.id, **data},
    }
    delivery_ids = stage_webhook_deliveries(
        db,
        tenant_id=job.tenant_id,
        credential_id=job.credential_id,
        event_id=event.id,
        event_type=event_type,
        payload=payload,
    )
    if delivery_ids:
        db.info.setdefault("public_api_webhook_deliveries", []).extend(delivery_ids)
    return event


def _commit_and_dispatch(db: Session) -> None:
    delivery_ids = list(db.info.pop("public_api_webhook_deliveries", []))
    db.commit()
    enqueue_webhook_deliveries(delivery_ids)


def update_job(
    db: Session,
    job: APIJob,
    *,
    stage: str | None = None,
    progress: float | None = None,
    event_type: str | None = None,
    event_data: dict[str, Any] | None = None,
) -> None:
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = min(1.0, max(0.0, progress))
    job.updated_at = utc_now()
    db.add(job)
    if event_type:
        emit_job_event(db, job, event_type, event_data or {})
    _commit_and_dispatch(db)


def _run_turn_id(db: Session, job: APIJob) -> str:
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == job.tenant_id,
            AgentEvent.session_id == job.session_id,
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(200)
    ).all()
    for row in rows:
        payload = dict(row.payload_json or {})
        if str(payload.get("client_turn_id") or "") != job.id:
            continue
        return str(payload.get("user_message_id") or payload.get("turn_id") or job.id)
    return job.id


def _finalize_run_session(
    db: Session,
    job: APIJob,
    *,
    terminal_status: str,
    error: dict[str, Any] | None = None,
) -> None:
    if job.kind != "run" or not job.session_id:
        return
    chat_session = db.get(ChatSession, job.session_id)
    if not chat_session or chat_session.tenant_id != job.tenant_id:
        return
    now = utc_now()
    if chat_session.status in {"running", "executing"}:
        chat_session.status = "active"
        chat_session.updated_at = now
        db.add(chat_session)
    if terminal_status == "succeeded":
        return
    event_type = "stream_cancelled" if terminal_status == "cancelled" else "stream_interrupted"
    existing = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == job.tenant_id,
            AgentEvent.session_id == job.session_id,
            AgentEvent.event_type == event_type,
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(100)
    ).all()
    if any(str((row.payload_json or {}).get("job_id") or "") == job.id for row in existing):
        return
    error_payload = dict(error or {})
    turn_id = _run_turn_id(db, job)
    db.add(
        AgentEvent(
            id=new_id("evt"),
            tenant_id=job.tenant_id,
            session_id=job.session_id,
            event_type=event_type,
            payload_json={
                "job_id": job.id,
                "client_turn_id": job.id,
                "turn_id": turn_id,
                "user_message_id": turn_id,
                "status": terminal_status,
                "code": str(error_payload.get("code") or "RUN_CANCELLED"),
                "message": str(error_payload.get("message") or "Run cancelled."),
            },
            created_at=now,
        )
    )


def _reconcile_terminal_run_sessions(db: Session) -> None:
    active_session_ids = {
        session_id
        for session_id in db.exec(
            select(APIJob.session_id).where(
                APIJob.kind == "run",
                APIJob.status.in_(["queued", "running"]),  # type: ignore[attr-defined]
                APIJob.session_id.is_not(None),
            )
        ).all()
        if session_id
    }
    terminal_jobs = db.exec(
        select(APIJob)
        .where(
            APIJob.kind == "run",
            APIJob.status.in_(["succeeded", "failed", "cancelled"]),  # type: ignore[attr-defined]
            APIJob.session_id.is_not(None),
        )
        .order_by(APIJob.updated_at.desc())
    ).all()
    reconciled: set[str] = set()
    for job in terminal_jobs:
        session_id = str(job.session_id or "")
        if not session_id or session_id in active_session_ids or session_id in reconciled:
            continue
        chat_session = db.get(ChatSession, session_id)
        if not chat_session or chat_session.status not in {"running", "executing"}:
            continue
        reconciled.add(session_id)
        _finalize_run_session(
            db,
            job,
            terminal_status=job.status,
            error=dict(job.error_json or {}),
        )


def run_job(job_id: str) -> None:
    with Session(engine) as db:
        job = db.get(APIJob, job_id)
        if not job or job.status not in {"queued", "running"}:
            return
        handler = _handlers.get(job.kind)
        if handler is None:
            job.status = "failed"
            job.stage = "failed"
            job.error_json = {"code": "JOB_HANDLER_MISSING", "message": job.kind}
            job.finished_at = utc_now()
            _finalize_run_session(
                db,
                job,
                terminal_status="failed",
                error=dict(job.error_json),
            )
            emit_job_event(db, job, "job.failed", dict(job.error_json))
            _commit_and_dispatch(db)
            return
        job.status = "running"
        job.stage = "starting"
        job.started_at = job.started_at or utc_now()
        job.updated_at = utc_now()
        emit_job_event(db, job, f"{job.kind}.started", {"job_id": job.id})
        _commit_and_dispatch(db)
        try:
            if job.cancel_requested:
                raise JobCancelled()
            result = handler(db, job)
            db.refresh(job)
            if job.cancel_requested:
                raise JobCancelled()
            job.result_json = result
            job.status = "succeeded"
            job.stage = "completed"
            job.progress = 1.0
            job.error_json = {}
            job.retryable = False
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            _finalize_run_session(db, job, terminal_status="succeeded")
            emit_job_event(db, job, f"{job.kind}.succeeded", {"job_id": job.id})
            _commit_and_dispatch(db)
        except JobCancelled:
            job.status = "cancelled"
            job.stage = "cancelled"
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            _finalize_run_session(db, job, terminal_status="cancelled")
            emit_job_event(db, job, f"{job.kind}.cancelled", {"job_id": job.id})
            _commit_and_dispatch(db)
        except Exception as exc:  # noqa: BLE001 - persisted public job boundary.
            db.rollback()
            job = db.get(APIJob, job_id)
            if not job:
                return
            job.status = "failed"
            job.stage = "failed"
            job.retryable = True
            job.error_json = {
                "code": "JOB_EXECUTION_FAILED",
                "message": str(exc)[:2000],
            }
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            _finalize_run_session(
                db,
                job,
                terminal_status="failed",
                error=dict(job.error_json),
            )
            emit_job_event(db, job, f"{job.kind}.failed", dict(job.error_json))
            _commit_and_dispatch(db)


class JobCancelled(Exception):
    pass


def ensure_not_cancelled(db: Session, job: APIJob) -> None:
    db.refresh(job)
    if job.cancel_requested:
        raise JobCancelled()


def _owned_job(db: Session, principal: PublicPrincipal, job_id: str) -> APIJob:
    row = db.get(APIJob, job_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "JOB_NOT_FOUND", "Job not found.")
    if principal.agent_id and row.agent_id != principal.agent_id:
        raise PublicAPIError(404, "JOB_NOT_FOUND", "Job not found.")
    return row


def _require_job_scope(principal: PublicPrincipal, row: APIJob, action: str) -> None:
    namespace = row.kind.split(".", 1)[0]
    public_namespace = {"sop": "sops", "knowledge": "knowledge"}.get(namespace, namespace)
    candidates = [f"jobs:{action}", f"{public_namespace}:{action}"]
    if namespace == "run":
        candidates.append(f"runs:{action}")
    if not any(principal.can(scope) for scope in candidates):
        raise PublicAPIError(403, "INSUFFICIENT_SCOPE", f"One of {', '.join(candidates)} is required.")


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: str,
    principal: PublicPrincipal = Depends(get_public_principal),
    db: Session = Depends(get_session),
) -> JobRead:
    row = _owned_job(db, principal, job_id)
    _require_job_scope(principal, row, "read")
    return job_read(row)


@router.get("/{job_id}/result", response_model=dict)
def get_job_result(
    job_id: str,
    principal: PublicPrincipal = Depends(get_public_principal),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_job(db, principal, job_id)
    _require_job_scope(principal, row, "read")
    if row.status not in {"succeeded", "failed", "cancelled"}:
        raise PublicAPIError(409, "JOB_NOT_FINISHED", "The job has not finished.")
    return {
        "job": job_read(row).model_dump(mode="json"),
        "result": dict(row.result_json or {}),
        "error": dict(row.error_json or {}),
    }


@router.post("/{job_id}:cancel", response_model=JobRead)
def cancel_job(
    job_id: str,
    principal: PublicPrincipal = Depends(get_public_principal),
    db: Session = Depends(get_session),
) -> JobRead:
    row = _owned_job(db, principal, job_id)
    _require_job_scope(principal, row, "cancel")
    if row.status in {"succeeded", "failed", "cancelled"}:
        return job_read(row)
    row.cancel_requested = True
    row.updated_at = utc_now()
    db.add(row)
    emit_job_event(db, row, "job.cancel_requested", {"job_id": row.id})
    _commit_and_dispatch(db)
    db.refresh(row)
    return job_read(row)


@router.get("/{job_id}/events")
def stream_job_events(
    job_id: str,
    request: Request,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: PublicPrincipal = Depends(get_public_principal),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    row = _owned_job(db, principal, job_id)
    _require_job_scope(principal, row, "read")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    def events() -> Iterator[str]:
        cursor = max(0, after)
        idle_ticks = 0
        while True:
            with Session(engine) as event_db:
                current = event_db.get(APIJob, row.id)
                if not current:
                    return
                rows = event_db.exec(
                    select(APIJobEvent)
                    .where(
                        APIJobEvent.job_id == row.id,
                        APIJobEvent.sequence > cursor,
                        APIJobEvent.public == True,  # noqa: E712
                    )
                    .order_by(APIJobEvent.sequence)
                ).all()
                for item in rows:
                    cursor = item.sequence
                    data = json.dumps(item.data_json or {}, ensure_ascii=False)
                    yield f"id: {item.sequence}\nevent: {item.event_type}\ndata: {data}\n\n"
                if current.status in {"succeeded", "failed", "cancelled"} and not rows:
                    return
            idle_ticks += 1
            if idle_ticks % 100 == 0:
                yield ": keepalive\n\n"
            sleep(0.15)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def recover_public_jobs() -> None:
    with Session(engine) as db:
        running = db.exec(select(APIJob).where(APIJob.status == "running")).all()
        for job in running:
            job.status = "failed"
            job.stage = "interrupted"
            job.retryable = True
            job.error_json = {
                "code": "SERVICE_RESTARTED",
                "message": "The service restarted while the job was running.",
            }
            job.finished_at = utc_now()
            job.updated_at = utc_now()
            _finalize_run_session(
                db,
                job,
                terminal_status="failed",
                error=dict(job.error_json),
            )
            db.add(job)
            emit_job_event(db, job, f"{job.kind}.failed", dict(job.error_json))
        _reconcile_terminal_run_sessions(db)
        queued = db.exec(select(APIJob).where(APIJob.status == "queued")).all()
        _commit_and_dispatch(db)
    for job in queued:
        enqueue_async_job(f"public_api.{job.kind}", run_job, job.id)


def cleanup_public_api_records() -> None:
    cutoff = utc_now() - timedelta(days=get_settings().public_api_retention_days)
    with Session(engine) as db:
        old_events = db.exec(
            select(APIJobEvent).where(APIJobEvent.created_at < cutoff)
        ).all()
        for row in old_events:
            db.delete(row)
        old_jobs = db.exec(
            select(APIJob).where(
                APIJob.updated_at < cutoff,
                APIJob.status.in_(["succeeded", "failed", "cancelled"]),  # type: ignore[attr-defined]
            )
        ).all()
        for row in old_jobs:
            db.delete(row)
        expired_idempotency = db.exec(
            select(APIIdempotencyRecord).where(APIIdempotencyRecord.expires_at < utc_now())
        ).all()
        for row in expired_idempotency:
            db.delete(row)
        old_deliveries = db.exec(
            select(WebhookDelivery).where(
                WebhookDelivery.updated_at < cutoff,
                WebhookDelivery.status.in_(["delivered", "abandoned"]),  # type: ignore[attr-defined]
            )
        ).all()
        for row in old_deliveries:
            db.delete(row)
        db.commit()
