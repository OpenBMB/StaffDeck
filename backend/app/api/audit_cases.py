from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlmodel import Session, select

from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseCoverageRead,
    AuditCaseCreate,
    AuditCaseMaterialRead,
    AuditCaseNotFound,
    AuditCaseRead,
    AuditCaseReadOnly,
    audit_case_material_read,
    audit_case_read,
)
from app.audit_cases.service import AuditCaseService
from app.db import get_session
from app.db.models import AuditCase, AuditCaseMaterial, AuditCaseMaterialChunk, User
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import require_tenant_admin


router = APIRouter(
    prefix="/api/audit-cases",
    tags=["audit-cases"],
    dependencies=[Depends(get_current_user)],
)


def _case_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuditCaseNotFound):
        return HTTPException(status_code=404, detail="AUDIT_CASE_NOT_FOUND")
    if isinstance(exc, AuditCaseReadOnly):
        return HTTPException(status_code=409, detail="AUDIT_CASE_READ_ONLY")
    if isinstance(exc, AuditCaseAccessDenied):
        return HTTPException(status_code=403, detail="AUDIT_CASE_ACCESS_DENIED")
    return HTTPException(status_code=400, detail=str(exc))


def _authorized_case(
    service: AuditCaseService,
    *,
    tenant_id: str,
    case_id: str,
    current_user: User,
) -> AuditCase:
    try:
        ensure_current_user_tenant(tenant_id, current_user)
        return service.get_case_for_user(tenant_id, case_id, current_user)
    except HTTPException:
        raise
    except Exception as exc:
        raise _case_error(exc) from exc


@router.post("", response_model=AuditCaseRead)
def create_audit_case(
    request: AuditCaseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCaseRead:
    try:
        ensure_current_user_tenant(request.tenant_id, current_user)
        row = AuditCaseService(db).create_case(current_user, request)
        return audit_case_read(row)
    except HTTPException:
        raise
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("", response_model=list[AuditCaseRead])
def list_audit_cases(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AuditCaseRead]:
    ensure_current_user_tenant(tenant_id, current_user)
    service = AuditCaseService(db)
    rows = db.exec(select(AuditCase).where(AuditCase.tenant_id == tenant_id)).all()
    return [audit_case_read(row) for row in rows if service.can_access(row, current_user)]


@router.post("/{case_id}/materials", response_model=list[AuditCaseMaterialRead])
async def upload_audit_case_materials(
    case_id: str,
    files: list[UploadFile] = File(...),
    tenant_id: str = Query(...),
    material_type: str = Query("audit_record", min_length=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AuditCaseMaterialRead]:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    result: list[AuditCaseMaterialRead] = []
    try:
        for upload in files:
            data = await upload.read()
            material = service.add_material(
                case,
                current_user,
                material_type,
                upload.filename or "unnamed",
                upload.content_type or "application/octet-stream",
                data,
            )
            result.append(audit_case_material_read(material))
        return result
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("/{case_id}/materials", response_model=list[AuditCaseMaterialRead])
def list_audit_case_materials(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AuditCaseMaterialRead]:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    return [audit_case_material_read(row) for row in service.list_current_materials(case)]


@router.post("/{case_id}/process", response_model=list[AuditCaseMaterialRead])
def process_audit_case_materials(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AuditCaseMaterialRead]:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    try:
        return [
            audit_case_material_read(service.process_material(case, material))
            for material in service.list_current_materials(case)
        ]
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("/{case_id}/coverage", response_model=AuditCaseCoverageRead)
def audit_case_coverage(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCaseCoverageRead:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    materials = service.list_current_materials(case)
    material_ids = [material.id for material in materials]
    chunks = (
        db.exec(
            select(AuditCaseMaterialChunk).where(
                AuditCaseMaterialChunk.tenant_id == tenant_id,
                AuditCaseMaterialChunk.audit_case_id == case.id,
                AuditCaseMaterialChunk.material_id.in_(material_ids),
            )
        ).all()
        if material_ids
        else []
    )
    successful_materials = [
        material
        for material in materials
        if material.extraction_status == "succeeded"
        and material.processing_status == "succeeded"
    ]
    successful_chunks = [chunk for chunk in chunks if chunk.processing_status == "succeeded"]
    material_count = len(materials)
    chunk_count = len(chunks)
    return AuditCaseCoverageRead(
        current_material_count=material_count,
        successful_material_count=len(successful_materials),
        failed_material_count=material_count - len(successful_materials),
        total_chunk_count=chunk_count,
        successful_chunk_count=len(successful_chunks),
        file_coverage=(len(successful_materials) / material_count) if material_count else 0.0,
        chunk_coverage=(len(successful_chunks) / chunk_count) if chunk_count else 0.0,
    )


@router.get("/{case_id}", response_model=AuditCaseRead)
def get_audit_case(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCaseRead:
    return audit_case_read(
        _authorized_case(
            AuditCaseService(db),
            tenant_id=tenant_id,
            case_id=case_id,
            current_user=current_user,
        )
    )


@router.post("/{case_id}/archive", response_model=AuditCaseRead)
def archive_audit_case(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCaseRead:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    try:
        return audit_case_read(service.archive_case(case, current_user))
    except Exception as exc:
        raise _case_error(exc) from exc


@router.delete("/{case_id}", status_code=204, response_class=Response)
def delete_audit_case(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> Response:
    try:
        AuditCaseService(db).delete_case(case_id, current_user)
    except Exception as exc:
        raise _case_error(exc) from exc
    return Response(status_code=204)
