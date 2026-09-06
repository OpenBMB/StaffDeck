from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.audit_cases.workbench import AuditWorkbenchService
from app.audit_cases.workbench_schema import (
    WorkIssueCreate,
    WorkIssueTransition,
    WorkItemCreate,
    WorkItemTransition,
    WorkMemberRoleUpdate,
)
from app.db import get_session
from app.db.models import User
from app.security.auth import get_current_user

router = APIRouter(prefix="/api/audit-workbench", tags=["audit-workbench"])


@router.get("/cases/{case_id}/rule-options")
def rule_options(
    case_id: str,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    from app.api.rules import _version_read
    from app.db.models import RuleSet, RuleSetVersion

    AuditWorkbenchService(db).case(tenant_id, case_id, actor)
    rows = db.exec(
        select(RuleSet, RuleSetVersion)
        .join(RuleSetVersion, RuleSetVersion.rule_set_id == RuleSet.id)
        .where(
            RuleSet.tenant_id == tenant_id,
            RuleSetVersion.tenant_id == tenant_id,
            RuleSet.status == "active",
            RuleSetVersion.status == "published",
        )
        .order_by(RuleSet.key, RuleSetVersion.version.desc())
    ).all()
    return [
        {
            "ruleSet": {"id": rules.id, "key": rules.key, "name": rules.name},
            "version": _version_read(version),
        }
        for rules, version in rows
    ]


@router.post("/cases/{case_id}/reports/{report_id}/work-document")
def report_work_document(
    case_id: str,
    report_id: str,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    from app.api.audit_cases import _audit_case_document_read

    service = AuditWorkbenchService(db)
    row = service.report_work_document(service.case(tenant_id, case_id, actor), actor, report_id)
    return _audit_case_document_read(db, row)


@router.get("/inbox")
def inbox(
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    return AuditWorkbenchService(db).inbox(tenant_id, actor)


@router.get("/cases/{case_id}")
def snapshot(
    case_id: str,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.snapshot(service.case(tenant_id, case_id, actor), actor)


@router.post("/cases/{case_id}/items")
def create_item(
    case_id: str,
    request: WorkItemCreate,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.create_item(service.case(tenant_id, case_id, actor), actor, request)


@router.post("/cases/{case_id}/items/{item_id}/transition")
def transition_item(
    case_id: str,
    item_id: str,
    request: WorkItemTransition,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.transition_item(service.case(tenant_id, case_id, actor), actor, item_id, request)


@router.post("/cases/{case_id}/issues")
def create_issue(
    case_id: str,
    request: WorkIssueCreate,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.create_issue(service.case(tenant_id, case_id, actor), actor, request)


@router.post("/cases/{case_id}/issues/{issue_id}/transition")
def transition_issue(
    case_id: str,
    issue_id: str,
    request: WorkIssueTransition,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.transition_issue(
        service.case(tenant_id, case_id, actor), actor, issue_id, request
    )


@router.put("/cases/{case_id}/members/{user_id}/role")
def member_role(
    case_id: str,
    user_id: str,
    request: WorkMemberRoleUpdate,
    tenant_id: str = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.set_member_role(
        service.case(tenant_id, case_id, actor), actor, user_id, request.role
    )


@router.get("/cases/{case_id}/events")
def events(
    case_id: str,
    tenant_id: str = Query(...),
    work_item_id: str | None = Query(None),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    service = AuditWorkbenchService(db)
    return service.events(service.case(tenant_id, case_id, actor), actor, work_item_id)
