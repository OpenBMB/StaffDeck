from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import APIAuditLog, APIJob, ChatSession, HumanHandoffRequest, MessageFeedback
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes


router = APIRouter(tags=["operations"])


@router.get("/audit-logs", response_model=dict)
def list_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    principal: PublicPrincipal = Depends(require_scopes("audit:read")),
    db: Session = Depends(get_session),
) -> dict:
    rows = db.exec(
        select(APIAuditLog)
        .where(APIAuditLog.tenant_id == principal.tenant_id)
        .order_by(APIAuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "data": [
            {
                "id": row.id,
                "credential_id": row.credential_id,
                "request_id": row.request_id,
                "method": row.method,
                "path": row.path,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "status_code": row.status_code,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.isoformat() + "Z",
            }
            for row in rows
        ],
        "next_cursor": None,
    }


@router.get("/usage", response_model=dict)
def get_usage(
    principal: PublicPrincipal = Depends(require_scopes("usage:read")),
    db: Session = Depends(get_session),
) -> dict:
    request_count = db.exec(
        select(func.count(APIAuditLog.id)).where(APIAuditLog.tenant_id == principal.tenant_id)
    ).one()
    average_duration = db.exec(
        select(func.avg(APIAuditLog.duration_ms)).where(APIAuditLog.tenant_id == principal.tenant_id)
    ).one()
    statuses = db.exec(
        select(APIJob.status, func.count(APIJob.id))
        .where(APIJob.tenant_id == principal.tenant_id)
        .group_by(APIJob.status)
    ).all()
    return {
        "api_requests": int(request_count or 0),
        "average_duration_ms": float(average_duration or 0),
        "jobs_by_status": {status: count for status, count in statuses},
    }


@router.get("/agents/{agent_id}/handoffs", response_model=dict)
def list_handoffs(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("operations:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    rows = db.exec(
        select(HumanHandoffRequest)
        .where(
            HumanHandoffRequest.tenant_id == principal.tenant_id,
            HumanHandoffRequest.agent_id == agent_id,
        )
        .order_by(HumanHandoffRequest.created_at.desc())
        .limit(100)
    ).all()
    return {"data": [row.model_dump(mode="json", exclude={"tenant_id", "resume_payload_json"}) for row in rows]}


@router.get("/agents/{agent_id}/feedback", response_model=dict)
def list_feedback(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("operations:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    session_ids = select(ChatSession.id).where(
        ChatSession.tenant_id == principal.tenant_id,
        ChatSession.agent_id == agent_id,
    )
    rows = db.exec(
        select(MessageFeedback)
        .where(
            MessageFeedback.tenant_id == principal.tenant_id,
            MessageFeedback.session_id.in_(session_ids),
        )
        .order_by(MessageFeedback.created_at.desc())
        .limit(100)
    ).all()
    return {"data": [row.model_dump(mode="json", exclude={"tenant_id"}) for row in rows]}
