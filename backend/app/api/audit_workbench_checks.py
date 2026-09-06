from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.audit_cases.schema import AuditCaseAccessDenied, AuditCaseNotFound
from app.audit_cases.service import AuditCaseService
from app.audit_cases.workbench_checks import (
    DocumentCheckError,
    check_read,
    create_check,
    enqueue_check,
    retry_check,
)
from app.db import get_session
from app.db.models import User
from app.db.workbench_checks import AuditDocumentCheck
from app.security.auth import ensure_current_user_tenant, get_current_user

router = APIRouter(prefix="/api/audit-workbench", tags=["audit-workbench-checks"])


class CheckRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=160)
    reference_document_ids: list[str] = Field(default_factory=list, max_length=50)
    request_key: str = Field(min_length=1, max_length=160)


class RetryRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=160)


def _case(db, actor, tenant_id, case_id):
    ensure_current_user_tenant(tenant_id, actor)
    try:
        return AuditCaseService(db).get_case_for_user(tenant_id, case_id, actor)
    except AuditCaseNotFound as exc:
        raise HTTPException(404, "AUDIT_CASE_NOT_FOUND") from exc
    except AuditCaseAccessDenied as exc:
        raise HTTPException(403, "PROJECT_ROLE_REQUIRED") from exc


def _error(db, exc):
    db.rollback()
    if isinstance(exc, AuditCaseAccessDenied):
        return HTTPException(403, "PROJECT_ROLE_REQUIRED")
    return HTTPException(404 if str(exc).endswith("NOT_FOUND") else 409, str(exc))


@router.get("/cases/{case_id}/checks")
def list_checks(
    case_id: str,
    tenant_id: str = Query(...),
    document_id: str | None = None,
    db: Session = Depends(get_session),
    actor: User = Depends(get_current_user),
):
    case = _case(db, actor, tenant_id, case_id)
    query = select(AuditDocumentCheck).where(
        AuditDocumentCheck.audit_case_id == case.id,
        AuditDocumentCheck.tenant_id == tenant_id,
    )
    if document_id:
        query = query.where(AuditDocumentCheck.document_id == document_id)
    jobs = db.exec(query.order_by(AuditDocumentCheck.created_at.desc()).limit(100)).all()
    return [check_read(db, case, job) for job in jobs]


@router.post("/cases/{case_id}/checks", status_code=202)
def start_check(
    case_id: str,
    request: CheckRequest,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    actor: User = Depends(get_current_user),
):
    case = _case(db, actor, tenant_id, case_id)
    try:
        job = create_check(
            db,
            case,
            actor,
            request.document_id,
            request.reference_document_ids,
            request.request_key,
        )
        response = check_read(db, case, job)
        enqueue_check(db.get_bind(), job)
        return response
    except (AuditCaseAccessDenied, DocumentCheckError) as exc:
        raise _error(db, exc) from exc


@router.post("/cases/{case_id}/checks/{job_id}/retry", status_code=202)
def restart_check(
    case_id: str,
    job_id: str,
    request: RetryRequest,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    actor: User = Depends(get_current_user),
):
    case = _case(db, actor, tenant_id, case_id)
    try:
        job = retry_check(db, case, actor, job_id, request.request_key)
        response = check_read(db, case, job)
        enqueue_check(db.get_bind(), job)
        return response
    except (AuditCaseAccessDenied, DocumentCheckError) as exc:
        raise _error(db, exc) from exc
