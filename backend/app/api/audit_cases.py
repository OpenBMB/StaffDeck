from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.audit_cases.coverage import calculate_coverage
from app.audit_cases.evidence import AuditEvidenceProcessor
from app.audit_cases.knowledge import AuditKnowledgeOrchestrator
from app.audit_cases.reporting import AuditReportService
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseCreate,
    AuditCaseEventRead,
    AuditCaseManagementOptions,
    AuditCaseManagementPage,
    AuditCaseMaterialRead,
    AuditCaseMemberUpdate,
    AuditCaseNotFound,
    AuditCaseProcessRequest,
    AuditCaseRead,
    AuditCaseReadOnly,
    AuditCaseUpdate,
    AuditCoverageSnapshot,
    AuditMaterialAlreadyExists,
    AuditMaterialCategoryConflict,
    AuditMaterialFormatError,
    AuditMaterialTooLarge,
    AuditReportCreateRequest,
    AuditReportRead,
    AuditReportSectionRead,
    audit_case_material_read,
    audit_case_read,
)
from app.audit_cases.service import AuditCaseService
from app.db import get_session
from app.db.models import (
    AuditCase,
    AuditReportSection,
    AuditReportVersion,
    ModelConfig,
    User,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import ensure_tenant_admin, require_tenant_admin

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
    if isinstance(exc, AuditMaterialAlreadyExists):
        return HTTPException(status_code=409, detail="MATERIAL_ALREADY_EXISTS")
    if isinstance(exc, AuditMaterialCategoryConflict):
        return HTTPException(status_code=409, detail="MATERIAL_CATEGORY_CONFLICT")
    if isinstance(exc, AuditMaterialFormatError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AuditMaterialTooLarge):
        return HTTPException(status_code=413, detail="AUDIT_MATERIAL_TOO_LARGE")
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
        ensure_tenant_admin(request.tenant_id, current_user)
        row = AuditCaseService(db).create_case(current_user, request)
        return audit_case_read(row)
    except HTTPException:
        raise
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("/management", response_model=AuditCaseManagementPage)
def list_audit_case_management(
    tenant_id: str = Query(...),
    query: str = Query(""),
    status: str | None = Query(None),
    management_system: str | None = Query(None),
    report_type: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> AuditCaseManagementPage:
    return AuditCaseService(db).list_management_cases(
        current_user,
        tenant_id=tenant_id,
        query=query,
        status=status,
        management_system=management_system,
        report_type=report_type,
        offset=offset,
        limit=limit,
    )


@router.get("/management-options", response_model=AuditCaseManagementOptions)
def list_audit_case_management_options(
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> AuditCaseManagementOptions:
    return AuditCaseService(db).list_management_options(current_user, tenant_id=tenant_id)


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
    current_user: User = Depends(require_tenant_admin),
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
    include_history: bool = Query(False),
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
    return [
        audit_case_material_read(row)
        for row in service.list_materials(case, include_history=include_history)
    ]


@router.post(
    "/{case_id}/materials/{material_id}/process",
    response_model=AuditCaseMaterialRead,
)
def process_one_audit_case_material(
    case_id: str,
    material_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> AuditCaseMaterialRead:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    try:
        return audit_case_material_read(
            service.process_one_material(case, current_user, material_id)
        )
    except Exception as exc:
        raise _case_error(exc) from exc


@router.post(
    "/{case_id}/materials/{material_id}/replace",
    response_model=AuditCaseMaterialRead,
)
async def replace_audit_case_material(
    case_id: str,
    material_id: str,
    file: UploadFile = File(...),
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> AuditCaseMaterialRead:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    try:
        material = service.get_material(case.id, material_id, current_user)
        replacement = service.replace_material(
            case,
            current_user,
            material,
            file.filename or "unnamed",
            file.content_type or "application/octet-stream",
            await file.read(),
        )
        return audit_case_material_read(replacement)
    except Exception as exc:
        raise _case_error(exc) from exc


@router.post("/{case_id}/process", response_model=None)
def process_audit_case_materials(
    case_id: str,
    tenant_id: str = Query(...),
    request: AuditCaseProcessRequest | None = Body(default=None),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> list[AuditCaseMaterialRead] | JSONResponse:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    if request is not None:
        model_config = _model_config_for_case(db, case, request.model_config_id)
        try:
            for material in service.list_current_materials(case):
                if not (
                    material.extraction_status == "succeeded"
                    and material.processing_status == "succeeded"
                ):
                    service.process_material(case, material, actor_user_id=current_user.id)
            evidence = AuditEvidenceProcessor(db).process_pending_chunks(case, model_config)
            knowledge = AuditKnowledgeOrchestrator(db).retrieve(case, model_config)
            return JSONResponse(
                status_code=202,
                content={
                    "status": "succeeded" if evidence.failed == 0 else "partial_failure",
                    "evidence": evidence.model_dump(mode="json"),
                    "knowledge": knowledge.model_dump(mode="json"),
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc
    try:
        return [
            audit_case_material_read(
                service.process_material(case, material, actor_user_id=current_user.id)
            )
            for material in service.list_current_materials(case)
        ]
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("/{case_id}/coverage", response_model=AuditCoverageSnapshot)
def audit_case_coverage(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCoverageSnapshot:
    case = _authorized_case(
        AuditCaseService(db),
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    return calculate_coverage(db, case)


@router.patch("/{case_id}", response_model=AuditCaseRead)
def update_audit_case(
    case_id: str,
    request: AuditCaseUpdate,
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
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
        return audit_case_read(service.update_case(case, current_user, request))
    except Exception as exc:
        raise _case_error(exc) from exc


@router.put("/{case_id}/members", response_model=AuditCaseRead)
def replace_audit_case_members(
    case_id: str,
    request: AuditCaseMemberUpdate,
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
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
        return audit_case_read(service.replace_members(case, current_user, request))
    except Exception as exc:
        raise _case_error(exc) from exc


@router.get("/{case_id}/events", response_model=list[AuditCaseEventRead])
def list_audit_case_events(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> list[AuditCaseEventRead]:
    service = AuditCaseService(db)
    case = _authorized_case(
        service,
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    return service.list_events(case, current_user)


@router.get("/{case_id}/reports", response_model=list[AuditReportRead])
def list_audit_case_reports(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AuditReportRead]:
    case = _authorized_case(
        AuditCaseService(db),
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    rows = db.exec(
        select(AuditReportVersion)
        .where(
            AuditReportVersion.tenant_id == case.tenant_id,
            AuditReportVersion.audit_case_id == case.id,
        )
        .order_by(AuditReportVersion.version.desc())
    ).all()
    return [_audit_report_read(db, row) for row in rows]


@router.post("/{case_id}/reports", response_model=AuditReportRead, status_code=202)
def create_audit_case_report(
    case_id: str,
    request: AuditReportCreateRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditReportRead:
    case = _authorized_case(
        AuditCaseService(db),
        tenant_id=tenant_id,
        case_id=case_id,
        current_user=current_user,
    )
    model_config = _model_config_for_case(db, case, request.model_config_id)
    try:
        service = AuditReportService(db)
        report = service.create_version(case)
        service.generate_pending_sections(case, report, model_config)
        if request.publish:
            report = service.publish(case, report, request.confirmed_by)
        return _audit_report_read(db, report)
    except HTTPException:
        raise
    except Exception as exc:
        raise _case_error(exc) from exc


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
    current_user: User = Depends(require_tenant_admin),
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


def _model_config_for_case(
    db: Session, case: AuditCase, model_config_id: str | None
) -> ModelConfig:
    if model_config_id:
        row = db.get(ModelConfig, model_config_id)
        if row is None or row.tenant_id != case.tenant_id or not row.enabled:
            raise HTTPException(status_code=404, detail="MODEL_CONFIG_NOT_FOUND")
        return row
    row = db.exec(
        select(ModelConfig)
        .where(ModelConfig.tenant_id == case.tenant_id, ModelConfig.enabled)
        .order_by(ModelConfig.is_default.desc(), ModelConfig.created_at)
    ).first()
    if row is None:
        raise HTTPException(status_code=400, detail="MODEL_CONFIG_REQUIRED")
    return row


def _audit_report_read(db: Session, row: AuditReportVersion) -> AuditReportRead:
    sections = db.exec(
        select(AuditReportSection)
        .where(AuditReportSection.report_version_id == row.id)
        .order_by(AuditReportSection.sequence)
    ).all()
    return AuditReportRead(
        id=row.id,
        tenant_id=row.tenant_id,
        audit_case_id=row.audit_case_id,
        version=row.version,
        status=row.status,
        material_version_ids=list(row.material_version_ids_json or []),
        knowledge_base_version_ids=list(row.knowledge_base_version_ids_json or []),
        coverage_snapshot=dict(row.coverage_snapshot_json or {}),
        final_storage_key=row.final_storage_key,
        sections=[
            AuditReportSectionRead(
                id=section.id,
                section_id=section.section_id,
                title=section.title,
                sequence=section.sequence,
                status=section.status,
                retry_count=section.retry_count,
                error_code=section.error_code,
                draft_markdown=section.draft_markdown,
                citation_ids=list(section.citation_ids_json or []),
            )
            for section in sections
        ],
    )
