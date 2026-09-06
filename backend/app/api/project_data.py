from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.audit_cases.schema import AuditCaseAccessDenied, AuditCaseNotFound
from app.audit_cases.service import AuditCaseService
from app.db import get_session
from app.db.models import (
    AuditCase,
    ProjectDataCandidate,
    ProjectDataFieldDefinition,
    User,
)
from app.project_data.fields import SYSTEM_FIELD_DEFINITIONS, FieldDefinition
from app.project_data.permissions import ensure_project_role
from app.project_data.schema import (
    ProjectDataCandidateCreate,
    ProjectDataCandidateRead,
    ProjectDataConflictError,
    ProjectDataConflictRead,
    ProjectDataNotFound,
    ProjectDataValueRead,
    ProjectFieldValidationError,
)
from app.project_data.service import ProjectDataService, _source_ref
from app.security.auth import ensure_current_user_tenant, get_current_user

router = APIRouter(prefix="/api", tags=["project-data"])


class FieldDefinitionRead(BaseModel):
    field_key: str
    label: str
    value_type: str
    information_domain: str
    scope: str
    required: bool
    editable: bool
    sync_policy: str
    validator_name: str | None = None
    source_optional: bool = False


class CandidateDecisionRequest(BaseModel):
    expected_current_revision: int | None = Field(default=None, ge=0)
    reason: str | None = None


class CandidateRejectRequest(BaseModel):
    reason: str = Field(min_length=1)


class ConflictResolveRequest(BaseModel):
    selected_candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def _data_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectDataNotFound):
        return HTTPException(status_code=404, detail="PROJECT_DATA_NOT_FOUND")
    if isinstance(exc, ProjectDataConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ProjectFieldValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    if isinstance(exc, AuditCaseNotFound):
        return HTTPException(status_code=404, detail="AUDIT_CASE_NOT_FOUND")
    if isinstance(exc, AuditCaseAccessDenied):
        return HTTPException(status_code=403, detail="PROJECT_ROLE_REQUIRED")
    return HTTPException(status_code=400, detail=str(exc))


def _authorized_case(
    db: Session,
    *,
    tenant_id: str,
    case_id: str,
    current_user: User,
) -> AuditCase:
    ensure_current_user_tenant(tenant_id, current_user)
    try:
        return AuditCaseService(db).get_case_for_user(tenant_id, case_id, current_user)
    except Exception as exc:
        raise _data_error(exc) from exc


def _candidate_read(row: ProjectDataCandidate) -> ProjectDataCandidateRead:
    return ProjectDataCandidateRead(
        id=row.id,
        field_key=row.field_key,
        value=row.value_json,
        source=_source_ref(row.source_json),
        status=row.status,
        expected_revision=row.expected_revision,
        submitted_by_user_id=row.submitted_by_user_id,
        decision_reason=row.decision_reason,
    )


def _value_read(row) -> ProjectDataValueRead:
    return ProjectDataValueRead(
        id=row.id,
        field_key=row.field_key,
        value=row.value_json,
        status=row.status,
        revision=row.revision,
        source=_source_ref(row.source_json),
        updated_by_user_id=row.updated_by_user_id,
    )


def _field_read(row: FieldDefinition) -> FieldDefinitionRead:
    return FieldDefinitionRead.model_validate(row.model_dump())


def _tenant_field_definition(row: ProjectDataFieldDefinition) -> FieldDefinition:
    return FieldDefinition(
        field_key=row.field_key,
        label=row.label,
        value_type=row.value_type,
        information_domain=row.information_domain,
        scope=row.scope,
        required=row.required,
        editable=row.editable,
        sync_policy=row.sync_policy,
        validator_name=row.validator_name,
        source_optional=row.source_optional,
    )


def _read_access(db: Session, case: AuditCase, current_user: User) -> None:
    ensure_project_role(
        db,
        case,
        current_user,
        {"project_admin", "reviewer", "editor", "viewer"},
    )


@router.get(
    "/audit-cases/{case_id}/field-schema",
    response_model=list[FieldDefinitionRead],
)
def get_field_schema(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[FieldDefinitionRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    _read_access(db, case, current_user)
    selected = {definition.field_key: definition for definition in SYSTEM_FIELD_DEFINITIONS}
    rows = db.exec(
        select(ProjectDataFieldDefinition).where(
            ProjectDataFieldDefinition.tenant_id == tenant_id,
            ProjectDataFieldDefinition.status == "active",
        )
    ).all()
    selected.update({row.field_key: _tenant_field_definition(row) for row in rows})
    return [_field_read(selected[key]) for key in sorted(selected)]


@router.get(
    "/audit-cases/{case_id}/fields",
    response_model=list[ProjectDataValueRead],
)
def list_project_fields(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ProjectDataValueRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        return ProjectDataService(db).list_fields(case, current_user)
    except Exception as exc:
        raise _data_error(exc) from exc


@router.get(
    "/audit-cases/{case_id}/field-candidates",
    response_model=list[ProjectDataCandidateRead],
)
def list_field_candidates(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ProjectDataCandidateRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    _read_access(db, case, current_user)
    rows = db.exec(
        select(ProjectDataCandidate)
        .where(
            ProjectDataCandidate.tenant_id == tenant_id,
            ProjectDataCandidate.audit_case_id == case_id,
        )
        .order_by(ProjectDataCandidate.created_at.desc())
    ).all()
    return [_candidate_read(row) for row in rows]


@router.post(
    "/audit-cases/{case_id}/field-candidates",
    response_model=ProjectDataCandidateRead,
    status_code=status.HTTP_201_CREATED,
)
def submit_field_candidate(
    case_id: str,
    request: ProjectDataCandidateCreate,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ProjectDataCandidateRead:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        row = ProjectDataService(db).submit_candidate(case, current_user, request)
        return _candidate_read(row)
    except Exception as exc:
        raise _data_error(exc) from exc


@router.post(
    "/field-candidates/{candidate_id}/approve",
    response_model=ProjectDataValueRead,
)
def approve_field_candidate(
    candidate_id: str,
    request: CandidateDecisionRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ProjectDataValueRead:
    ensure_current_user_tenant(tenant_id, current_user)
    try:
        row = ProjectDataService(db).approve_candidate(
            candidate_id,
            current_user,
            request.expected_current_revision,
            request.reason,
        )
        return _value_read(row)
    except Exception as exc:
        raise _data_error(exc) from exc


@router.post(
    "/field-candidates/{candidate_id}/reject",
    response_model=ProjectDataCandidateRead,
)
def reject_field_candidate(
    candidate_id: str,
    request: CandidateRejectRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ProjectDataCandidateRead:
    ensure_current_user_tenant(tenant_id, current_user)
    try:
        row = ProjectDataService(db).reject_candidate(
            candidate_id,
            current_user,
            request.reason,
        )
        return _candidate_read(row)
    except Exception as exc:
        raise _data_error(exc) from exc


@router.get(
    "/audit-cases/{case_id}/field-conflicts",
    response_model=list[ProjectDataConflictRead],
)
def list_field_conflicts(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ProjectDataConflictRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        return ProjectDataService(db).list_conflicts(case, current_user)
    except Exception as exc:
        raise _data_error(exc) from exc


@router.post(
    "/field-conflicts/{conflict_id}/resolve",
    response_model=ProjectDataValueRead,
)
def resolve_field_conflict(
    conflict_id: str,
    request: ConflictResolveRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ProjectDataValueRead:
    ensure_current_user_tenant(tenant_id, current_user)
    try:
        row = ProjectDataService(db).resolve_conflict(
            conflict_id,
            request.selected_candidate_id,
            current_user,
            request.reason,
        )
        return _value_read(row)
    except Exception as exc:
        raise _data_error(exc) from exc
