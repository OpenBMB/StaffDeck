from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.audit_cases.schema import AuditCaseAccessDenied, AuditCaseNotFound
from app.audit_cases.service import AuditCaseService, record_case_event
from app.db import get_session
from app.db.models import (
    AuditCase,
    RuleDefinition,
    RuleEvaluation,
    RuleException,
    RuleSet,
    RuleSetVersion,
    User,
)
from app.project_data.permissions import ensure_project_role
from app.rules.schema import (
    RuleDefinitionCreate,
    RuleSetCreate,
    RuleValidationError,
    RuleVersionImmutableError,
)
from app.rules.service import (
    RuleBindingError,
    RuleBindingService,
    RuleLibraryService,
)
from app.security.auth import ensure_current_user_tenant, get_current_user

router = APIRouter(prefix="/api", tags=["rules"])


class RuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    key: str
    name: str
    description: str
    management_systems: list[str]
    audit_types: list[str]
    business_domain: str
    status: str


class RuleSetVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    rule_set_id: str
    version: int
    status: str
    content_sha256: str
    published_by_user_id: str | None = None
    published_at: Any | None = None


class RuleVersionPayload(BaseModel):
    rules: list[RuleDefinitionCreate] = Field(default_factory=list)


class RuleBindingReplaceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version_ids: list[str] = Field(min_length=1, alias="rule_set_version_ids")
    selection_source: Literal["recommended", "manual"] = "manual"


class RuleMigrationRequest(BaseModel):
    version_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class RuleMigrationPreviewRead(BaseModel):
    added_rule_keys: list[str] = Field(default_factory=list)
    removed_rule_keys: list[str] = Field(default_factory=list)
    changed_rule_keys: list[str] = Field(default_factory=list)
    unchanged_rule_keys: list[str] = Field(default_factory=list)
    impacted_information_domains: list[str] = Field(default_factory=list)
    impacted_workflow_nodes: list[str] = Field(default_factory=list)


class RuleBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    audit_case_id: str
    rule_set_id: str
    rule_set_version_id: str
    selection_source: str
    status: str
    priority: int
    bound_by_user_id: str
    supersedes_binding_id: str | None = None


class RuleEvaluationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    audit_case_id: str
    rule_set_version_id: str
    rule_definition_id: str
    workflow_node: str
    information_domain: str
    target_ref: str
    input_revision: int
    status: str
    result: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    executor_type: str
    executor_version: str | None = None


class RuleExceptionCreate(BaseModel):
    reason: str = Field(min_length=1)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class RuleExceptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    audit_case_id: str
    rule_evaluation_id: str
    reason: str
    evidence_refs: list[dict[str, Any]]
    granted_by_user_id: str


def _require_rule_admin(tenant_id: str, current_user: User) -> None:
    ensure_current_user_tenant(tenant_id, current_user)
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="RULE_ADMIN_REQUIRED")


def _rule_set_read(row: RuleSet) -> RuleSetRead:
    return RuleSetRead(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        name=row.name,
        description=row.description,
        management_systems=list(row.management_systems_json or []),
        audit_types=list(row.audit_types_json or []),
        business_domain=row.business_domain,
        status=row.status,
    )


def _version_read(row: RuleSetVersion) -> RuleSetVersionRead:
    return RuleSetVersionRead.model_validate(row)


def _evaluation_read(row: RuleEvaluation) -> RuleEvaluationRead:
    return RuleEvaluationRead(
        id=row.id,
        tenant_id=row.tenant_id,
        audit_case_id=row.audit_case_id,
        rule_set_version_id=row.rule_set_version_id,
        rule_definition_id=row.rule_definition_id,
        workflow_node=row.workflow_node,
        information_domain=row.information_domain,
        target_ref=row.target_ref,
        input_revision=row.input_revision,
        status=row.status,
        result=dict(row.result_json or {}),
        evidence_refs=list(row.evidence_refs_json or []),
        executor_type=row.executor_type,
        executor_version=row.executor_version,
    )


def _exception_read(row: RuleException) -> RuleExceptionRead:
    return RuleExceptionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        audit_case_id=row.audit_case_id,
        rule_evaluation_id=row.rule_evaluation_id,
        reason=row.reason,
        evidence_refs=list(row.evidence_refs_json or []),
        granted_by_user_id=row.granted_by_user_id,
    )


def _rule_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuleValidationError):
        return HTTPException(status_code=422, detail=exc.code)
    if isinstance(exc, RuleVersionImmutableError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RuleBindingError):
        code = exc.code
        if code in {
            "RULE_TENANT_ACCESS_DENIED",
            "PROJECT_ROLE_REQUIRED",
        }:
            return HTTPException(status_code=403, detail=code)
        if code in {
            "RULE_MIGRATION_CONFIRMATION_REQUIRED",
        }:
            return HTTPException(status_code=409, detail=code)
        return HTTPException(status_code=422, detail=code)
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
        raise _rule_error(exc) from exc


def _parse_rules(payload: Any) -> list[RuleDefinitionCreate]:
    raw_rules = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(raw_rules, list):
        raise HTTPException(status_code=422, detail="RULES_REQUIRED")
    try:
        return [RuleDefinitionCreate.model_validate(item) for item in raw_rules]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="INVALID_RULE_DEFINITION") from exc


@router.post("/rule-sets", response_model=RuleSetRead, status_code=status.HTTP_201_CREATED)
def create_rule_set(
    request: RuleSetCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RuleSetRead:
    _require_rule_admin(request.tenant_id, current_user)
    try:
        return _rule_set_read(RuleLibraryService(db).create_rule_set(current_user, request))
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.post(
    "/rule-sets/{rule_set_id}/versions",
    response_model=RuleSetVersionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_rule_set_version(
    rule_set_id: str,
    payload: Any = Body(...),
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RuleSetVersionRead:
    _require_rule_admin(tenant_id, current_user)
    rule_set = db.exec(
        select(RuleSet).where(
            RuleSet.id == rule_set_id,
            RuleSet.tenant_id == tenant_id,
        )
    ).first()
    if rule_set is None:
        raise HTTPException(status_code=404, detail="RULE_SET_NOT_FOUND")
    try:
        version = RuleLibraryService(db).create_draft_version(
            rule_set,
            current_user,
            _parse_rules(payload),
        )
        return _version_read(version)
    except Exception as exc:
        raise _rule_error(exc) from exc


def _get_version_for_set(
    db: Session,
    *,
    rule_set_id: str,
    version_id: str,
    tenant_id: str,
) -> RuleSetVersion:
    version = db.exec(
        select(RuleSetVersion).where(
            RuleSetVersion.id == version_id,
            RuleSetVersion.rule_set_id == rule_set_id,
            RuleSetVersion.tenant_id == tenant_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="RULE_VERSION_NOT_FOUND")
    return version


@router.post(
    "/rule-sets/{rule_set_id}/versions/{version_id}/validate",
    response_model=dict[str, list[str]],
)
def validate_rule_set_version(
    rule_set_id: str,
    version_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, list[str]]:
    _require_rule_admin(tenant_id, current_user)
    _get_version_for_set(
        db,
        rule_set_id=rule_set_id,
        version_id=version_id,
        tenant_id=tenant_id,
    )
    try:
        return {"errors": RuleLibraryService(db).validate_version(version_id, current_user)}
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.post(
    "/rule-sets/{rule_set_id}/versions/{version_id}/publish",
    response_model=RuleSetVersionRead,
)
def publish_rule_set_version(
    rule_set_id: str,
    version_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RuleSetVersionRead:
    _require_rule_admin(tenant_id, current_user)
    _get_version_for_set(
        db,
        rule_set_id=rule_set_id,
        version_id=version_id,
        tenant_id=tenant_id,
    )
    try:
        return _version_read(RuleLibraryService(db).publish_version(version_id, current_user))
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.get("/rule-sets/{rule_set_id}/versions", response_model=list[RuleSetVersionRead])
def list_rule_set_versions(
    rule_set_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RuleSetVersionRead]:
    _require_rule_admin(tenant_id, current_user)
    rows = db.exec(
        select(RuleSetVersion)
        .where(
            RuleSetVersion.rule_set_id == rule_set_id,
            RuleSetVersion.tenant_id == tenant_id,
        )
        .order_by(RuleSetVersion.version.desc())
    ).all()
    return [_version_read(row) for row in rows]


@router.get(
    "/audit-cases/{case_id}/rule-bindings",
    response_model=list[RuleBindingRead],
)
def list_rule_bindings(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RuleBindingRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        rows = RuleBindingService(db).list_current_bindings(case, current_user)
        return [RuleBindingRead.model_validate(row) for row in rows]
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.put(
    "/audit-cases/{case_id}/rule-bindings",
    response_model=list[RuleBindingRead],
)
def replace_rule_bindings(
    case_id: str,
    request: RuleBindingReplaceRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RuleBindingRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        rows = RuleBindingService(db).replace_current_bindings(
            case,
            request.version_ids,
            current_user,
            request.selection_source,
        )
        return [RuleBindingRead.model_validate(row) for row in rows]
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.post(
    "/audit-cases/{case_id}/rule-bindings/migration-preview",
    response_model=RuleMigrationPreviewRead,
)
def preview_rule_binding_migration(
    case_id: str,
    request: RuleBindingReplaceRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RuleMigrationPreviewRead:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        preview = RuleBindingService(db).preview_migration(
            case, request.version_ids, current_user
        )
        return RuleMigrationPreviewRead.model_validate(vars(preview))
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.post(
    "/audit-cases/{case_id}/rule-bindings/migrate",
    response_model=list[RuleBindingRead],
)
def migrate_rule_bindings(
    case_id: str,
    request: RuleMigrationRequest,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RuleBindingRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    try:
        rows = RuleBindingService(db).migrate(
            case,
            request.version_ids,
            current_user,
            request.reason,
        )
        return [RuleBindingRead.model_validate(row) for row in rows]
    except Exception as exc:
        raise _rule_error(exc) from exc


@router.get(
    "/audit-cases/{case_id}/rule-evaluations",
    response_model=list[RuleEvaluationRead],
)
def list_rule_evaluations(
    case_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[RuleEvaluationRead]:
    case = _authorized_case(
        db, tenant_id=tenant_id, case_id=case_id, current_user=current_user
    )
    ensure_project_role(
        db,
        case,
        current_user,
        {"project_admin", "reviewer", "editor", "viewer"},
    )
    rows = db.exec(
        select(RuleEvaluation)
        .where(
            RuleEvaluation.tenant_id == tenant_id,
            RuleEvaluation.audit_case_id == case_id,
        )
        .order_by(RuleEvaluation.created_at.desc())
    ).all()
    return [_evaluation_read(row) for row in rows]


@router.post(
    "/rule-evaluations/{evaluation_id}/exception",
    response_model=RuleExceptionRead,
    status_code=status.HTTP_201_CREATED,
)
def grant_rule_exception(
    evaluation_id: str,
    request: RuleExceptionCreate,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> RuleExceptionRead:
    ensure_current_user_tenant(tenant_id, current_user)
    evaluation = db.exec(
        select(RuleEvaluation).where(
            RuleEvaluation.id == evaluation_id,
            RuleEvaluation.tenant_id == tenant_id,
        )
    ).first()
    if evaluation is None:
        raise HTTPException(status_code=404, detail="RULE_EVALUATION_NOT_FOUND")
    case = _authorized_case(
        db,
        tenant_id=tenant_id,
        case_id=evaluation.audit_case_id,
        current_user=current_user,
    )
    try:
        ensure_project_role(db, case, current_user, {"project_admin", "reviewer"})
        reason = request.reason.strip()
        if not reason:
            raise RuleBindingError("RULE_EXCEPTION_NOT_ALLOWED")
        rule = db.get(RuleDefinition, evaluation.rule_definition_id)
        if (
            rule is None
            or rule.tenant_id != tenant_id
            or not (
                bool(rule.condition_json.get("exception_allowed"))
                or bool(rule.condition_json.get("allow_exception"))
            )
        ):
            raise RuleBindingError("RULE_EXCEPTION_NOT_ALLOWED")
        exception = RuleException(
            tenant_id=tenant_id,
            audit_case_id=case.id,
            rule_evaluation_id=evaluation.id,
            reason=reason,
            evidence_refs_json=[dict(item) for item in request.evidence_refs],
            granted_by_user_id=current_user.id,
        )
        db.add(exception)
        record_case_event(
            db,
            case=case,
            actor_user_id=current_user.id,
            event_type="rule_exception_granted",
            resource_type="rule_exception",
            resource_id=exception.id,
            metadata={
                "rule_evaluation_id": evaluation.id,
                "rule_set_version_id": evaluation.rule_set_version_id,
                "status": "granted",
            },
        )
        db.commit()
        db.refresh(exception)
        return _exception_read(exception)
    except HTTPException:
        raise
    except Exception as exc:
        raise _rule_error(exc) from exc
