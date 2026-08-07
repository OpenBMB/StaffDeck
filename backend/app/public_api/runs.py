from __future__ import annotations

import mimetypes
import re
from typing import Any
import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from app.core import AgentLoop
from app.core.cancellation import cancel_chat_turn
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db import engine, get_session
from app.db.models import (
    APIClient,
    APICredential,
    APIJob,
    AgentEvent,
    HarnessInvocationRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
)
from app.harness import HarnessArtifactAccessError, normalize_harness_artifact_path, open_harness_artifact
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.idempotency import replay_idempotent_response, store_idempotent_response
from app.public_api.jobs import (
    create_job,
    ensure_not_cancelled,
    job_read,
    register_job_handler,
    update_job,
)
from app.public_api.jobs import stream_job_events
from app.public_api.schemas import AgentRunCreate, PublicSessionCreate
from app.public_api.sessions import create_public_session_row, ensure_public_agent, owned_public_session
from app.session.session_schema import ChatAttachmentRead, ChatTurnRequest, ChatTurnResponse


router = APIRouter(tags=["runs"])

_TRACE_EVENT_MAP = {
    "stream_status": "run.status",
    "stream_delta": "run.output.delta",
    "stream_replace": "run.output.replace",
    "stream_end": "run.output.completed",
    "stream_cancelled": "run.cancelled",
    "turn_plan_created": "run.plan",
    "router_decision_created": "run.intent",
    "task_frame_started": "run.task_frame.started",
    "task_frame_finished": "run.task_frame.finished",
    "task_frame_completed": "run.task_frame.completed",
    "task_frame_dependency_waiting": "run.task_frame.waiting",
    "task_frame_dependencies_released": "run.task_frame.released",
    "capability_search_completed": "run.capability.search",
    "capability_described": "run.capability.described",
    "harness_tool_completed": "run.capability.completed",
    "harness_step_timeout": "run.sop.step.timeout",
    "knowledge_result": "run.citation",
    "tool_result": "run.tool.completed",
    "skill_state": "run.sop.state",
    "step_result": "run.sop.step",
    "general_skill_trace": "run.skill.trace",
    "general_skill_run_finished": "run.skill.completed",
    "agent_loop_continued": "run.loop.continued",
    "agent_loop_completed": "run.loop.completed",
    "human_handoff_created": "handoff.created",
}
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credentials",
    "error_traceback",
    "headers",
    "raw_cot",
    "system_prompt",
    "tool_credentials",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 8000:
        return value[:8000] + "…"
    return value


def _job_actor(db: Session, job: APIJob):
    credential = db.get(APICredential, job.credential_id)
    if not credential or credential.tenant_id != job.tenant_id:
        raise RuntimeError("Run credential is unavailable")
    client = db.get(APIClient, credential.client_id)
    if not client or not client.created_by_user_id:
        raise RuntimeError("Run API client is unavailable")
    from app.db.models import User

    actor = db.get(User, client.created_by_user_id)
    if not actor or actor.tenant_id != job.tenant_id:
        raise RuntimeError("Run API client owner is unavailable")
    return credential, actor


def _latest_artifacts(db: Session, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
    rows = db.exec(
        select(Message).where(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
    ).all()
    artifacts: list[dict[str, Any]] = []
    for row in rows:
        for artifact in (row.metadata_json or {}).get("harness_artifacts") or []:
            if isinstance(artifact, dict):
                artifacts.append(_redact(dict(artifact)))
    return artifacts[-100:]


@register_job_handler("run")
def execute_run(db: Session, job: APIJob) -> dict[str, Any]:
    credential, actor = _job_actor(db, job)
    payload = dict(job.request_json or {})
    session_id = str(payload.get("session_id") or "").strip() or None
    stateless = payload.get("session_mode") == "stateless"
    if not session_id:
        principal = PublicPrincipal(
            tenant_id=job.tenant_id,
            actor_user=actor,
            scopes=frozenset(credential.scopes_json or []),
            client_id=credential.client_id,
            credential_id=credential.id,
            agent_id=credential.agent_id,
        )
        session, _ = create_public_session_row(
            db,
            principal,
            str(job.agent_id),
            PublicSessionCreate(
                external_session_id=None if stateless else payload.get("external_session_id"),
                external_user_id=payload.get("external_user_id"),
                title=str(payload.get("input") or "")[:80],
                metadata=dict(payload.get("metadata") or {}),
            ),
        )
        session_id = session.id
    job.session_id = session_id
    db.add(job)
    update_job(
        db,
        job,
        stage="executing",
        progress=0.1,
        event_type="run.executing",
        event_data={"session_id": session_id, "engine": "harness_v2"},
    )
    attachments = [ChatAttachmentRead.model_validate(item) for item in payload.get("attachments") or []]
    request = ChatTurnRequest(
        tenant_id=job.tenant_id,
        session_id=session_id,
        agent_id=job.agent_id,
        client_turn_id=job.id,
        user_id=actor.id,
        message=str(payload.get("input") or ""),
        attachments=attachments,
        channel="public_api",
    )
    latest = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == job.tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
    ).first()
    cursor = (latest.created_at, latest.id) if latest else None
    worker_done = threading.Event()
    worker_result: dict[str, Any] = {}

    def execute_harness() -> None:
        try:
            with Session(engine) as worker_db:
                for item in AgentLoop(worker_db).handle_turn_stream(request):
                    if item.get("event") != "complete":
                        continue
                    data = item.get("data")
                    if isinstance(data, dict):
                        worker_result["response"] = ChatTurnResponse.model_validate(data)
                if "response" not in worker_result:
                    raise RuntimeError("Harness stream ended without a complete response")
        except Exception as exc:  # noqa: BLE001 - re-raised at persistent job boundary.
            worker_result["error"] = exc
        finally:
            worker_done.set()

    thread = threading.Thread(target=execute_harness, name=f"public-run-{job.id}", daemon=True)
    thread.start()
    while not worker_done.is_set():
        ensure_not_cancelled(db, job)
        cursor = _relay_agent_events(db, job, session_id, cursor)
        worker_done.wait(0.1)
    thread.join(timeout=1)
    _relay_agent_events(db, job, session_id, cursor)
    if "error" in worker_result:
        raise worker_result["error"]
    result = ChatTurnResponse.model_validate(worker_result.get("response"))
    response_json = result.model_dump(mode="json")
    state = dict(response_json.get("session_state") or {})
    turn = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == job.tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == job.id,
        )
    ).first()
    source_turn_id = turn.user_message_id if turn else None
    frames = db.exec(
        select(HarnessTaskFrameRecord)
        .where(
            HarnessTaskFrameRecord.tenant_id == job.tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.source_turn_id == source_turn_id,
        )
        .order_by(HarnessTaskFrameRecord.sequence)
    ).all() if source_turn_id else []
    invocations = db.exec(
        select(HarnessInvocationRecord)
        .where(
            HarnessInvocationRecord.tenant_id == job.tenant_id,
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.task_id.in_([frame.task_id for frame in frames]),
        )
        .order_by(HarnessInvocationRecord.created_at)
    ).all() if frames else []
    assistant = db.exec(
        select(Message)
        .where(
            Message.tenant_id == job.tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.desc())
    ).first()
    assistant_metadata = dict(assistant.metadata_json or {}) if assistant else {}
    return {
        "run_id": job.id,
        "agent_id": job.agent_id,
        "session_id": None if stateless else session_id,
        "reply": result.reply,
        "citations": _redact(list(assistant_metadata.get("knowledge_citations") or [])),
        "tool_calls": [
            {
                "call_id": invocation.call_id,
                "task_frame_id": invocation.task_id,
                "name": invocation.tool_name,
                "status": invocation.status,
                "arguments": _redact(dict(invocation.arguments_json or {})),
                "result": _redact(dict(invocation.result_json or {})),
            }
            for invocation in invocations
        ],
        "task_results": [
            {
                "task_frame_id": frame.task_id,
                "kind": frame.kind,
                "status": frame.status,
                "sop_id": frame.skill_id,
                "step_id": frame.step_id,
                "result": _redact(dict(frame.result_json or {})),
                "error": _redact(dict(frame.error_json or {})),
            }
            for frame in frames
        ],
        "awaiting_input": state.get("awaiting_input"),
        "session_state": state,
        "artifacts": _latest_artifacts(db, job.tenant_id, session_id),
    }


def _relay_agent_events(
    db: Session,
    job: APIJob,
    session_id: str,
    cursor: tuple[Any, str] | None,
) -> tuple[Any, str] | None:
    statement = select(AgentEvent).where(
        AgentEvent.tenant_id == job.tenant_id,
        AgentEvent.session_id == session_id,
    )
    if cursor:
        statement = statement.where(
            or_(
                AgentEvent.created_at > cursor[0],
                (AgentEvent.created_at == cursor[0]) & (AgentEvent.id > cursor[1]),
            )
        )
    rows = db.exec(statement.order_by(AgentEvent.created_at, AgentEvent.id)).all()
    for event in rows:
        public_type = _TRACE_EVENT_MAP.get(event.event_type)
        if public_type:
            update_job(
                db,
                job,
                event_type=public_type,
                event_data=_redact(dict(event.payload_json or {})),
            )
        cursor = (event.created_at, event.id)
    return cursor


@router.post("/agents/{agent_id}/runs", response_model=dict, status_code=202)
def create_run_route(
    agent_id: str,
    body: AgentRunCreate,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("runs:create")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    if body.session_id:
        owned_public_session(db, principal, agent_id, body.session_id)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    job = create_job(
        db,
        principal,
        kind="run",
        request_payload=body.model_dump(mode="json"),
        agent_id=agent_id,
    )
    payload = job_read(job).model_dump(mode="json")
    store_idempotent_response(
        db,
        principal,
        request,
        body.model_dump(mode="json"),
        payload,
        status_code=202,
        resource_id=job.id,
    )
    return payload


@router.post("/agents/{agent_id}/runs:stream", status_code=200)
def create_run_stream_route(
    agent_id: str,
    body: AgentRunCreate,
    request: Request,
    principal: PublicPrincipal = Depends(require_scopes("runs:create")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    """Create a durable run and stream its public events in the same request."""
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    if body.session_id:
        owned_public_session(db, principal, agent_id, body.session_id)
    request_payload = body.model_dump(mode="json")
    replay = replay_idempotent_response(db, principal, request, request_payload)
    if replay:
        run_id = str(replay[1].get("id") or "")
        job = _owned_run(db, principal, run_id)
    else:
        job = create_job(
            db,
            principal,
            kind="run",
            request_payload=request_payload,
            agent_id=agent_id,
        )
        payload = job_read(job).model_dump(mode="json")
        store_idempotent_response(
            db,
            principal,
            request,
            request_payload,
            payload,
            status_code=202,
            resource_id=job.id,
        )
    stream = stream_job_events(job.id, request, 0, None, principal, db)
    stream.headers["X-Run-ID"] = job.id
    stream.headers["Cache-Control"] = "no-cache, no-transform"
    stream.headers["X-Accel-Buffering"] = "no"
    return stream


def _owned_run(db: Session, principal: PublicPrincipal, run_id: str) -> APIJob:
    row = db.get(APIJob, run_id)
    if not row or row.kind != "run" or row.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "RUN_NOT_FOUND", "Run not found.")
    if principal.agent_id and row.agent_id != principal.agent_id:
        raise PublicAPIError(404, "RUN_NOT_FOUND", "Run not found.")
    return row


@router.get("/runs/{run_id}", response_model=dict)
def get_run(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    return job_read(_owned_run(db, principal, run_id)).model_dump(mode="json")


@router.get("/runs/{run_id}/result", response_model=dict)
def get_run_result(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    if row.status != "succeeded":
        raise PublicAPIError(409, "RUN_NOT_SUCCEEDED", "The run has not succeeded.")
    return dict(row.result_json or {})


@router.get("/runs/{run_id}/events")
def stream_run_events(
    run_id: str,
    request: Request,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    _owned_run(db, principal, run_id)
    return stream_job_events(run_id, request, after, last_event_id, principal, db)


@router.post("/runs/{run_id}:cancel", response_model=dict)
def cancel_run(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:cancel")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    if row.status not in {"succeeded", "failed", "cancelled"}:
        row.cancel_requested = True
        if row.session_id:
            cancel_chat_turn(row.session_id, row.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return job_read(row).model_dump(mode="json")


@router.get("/runs/{run_id}/artifacts", response_model=dict)
def list_run_artifacts(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    if not row.session_id:
        return {"data": []}
    return {"data": _latest_artifacts(db, row.tenant_id, row.session_id)}


@router.get("/runs/{run_id}/artifacts/{task_frame_id}")
def download_run_artifact(
    run_id: str,
    task_frame_id: str,
    path: str = Query(..., min_length=1),
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    row = _owned_run(db, principal, run_id)
    if not row.session_id:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
    try:
        normalized = normalize_harness_artifact_path(path)
    except HarnessArtifactAccessError as exc:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.") from exc
    artifact = next(
        (
            item
            for item in _latest_artifacts(db, row.tenant_id, row.session_id)
            if item.get("type") == "workspace_file"
            and str(item.get("task_frame_id") or "") == task_frame_id
            and item.get("path") == normalized
        ),
        None,
    )
    if not artifact:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
    try:
        opened = open_harness_artifact(
            harness_task_workspace_path(
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                task_frame_id=task_frame_id,
            ),
            normalized,
        )
    except (HarnessArtifactAccessError, OSError) as exc:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.") from exc
    filename = _safe_artifact_download_name(
        str(artifact.get("display_name") or opened.filename)
    )
    fallback_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    fallback_filename = (fallback_filename or "artifact")[:120]
    media_type = (
        str(artifact.get("content_type") or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    return StreamingResponse(
        opened.iter_bytes(),
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "Content-Length": str(opened.size),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(opened.close),
    )


def _safe_artifact_download_name(filename: str) -> str:
    cleaned = "".join(
        character
        for character in str(filename).strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned[:180] or "artifact"
