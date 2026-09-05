from __future__ import annotations

from typing import Literal

from sqlmodel import Session, select

from app.audit_cases.schema import AuditCaseAccessDenied
from app.db.models import AuditCase, AuditCaseMemberRole, User

ProjectRole = Literal["project_admin", "reviewer", "editor", "viewer"]

_PROJECT_ROLES = frozenset({"project_admin", "reviewer", "editor", "viewer"})
_CANDIDATE_SUBMIT_ROLES = frozenset({"project_admin", "reviewer", "editor"})
_CANDIDATE_APPROVE_ROLES = frozenset({"project_admin", "reviewer"})


def resolve_project_role(db: Session, case: AuditCase, user: User) -> str | None:
    if user.tenant_id != case.tenant_id:
        return None
    if user.role == "admin":
        return "project_admin"

    membership = db.exec(
        select(AuditCaseMemberRole).where(
            AuditCaseMemberRole.tenant_id == case.tenant_id,
            AuditCaseMemberRole.audit_case_id == case.id,
            AuditCaseMemberRole.user_id == user.id,
        )
    ).first()
    if membership is not None and membership.role in _PROJECT_ROLES:
        return membership.role
    if case.owner_user_id == user.id:
        return "project_admin"
    if user.id in set(case.member_user_ids_json or []):
        return "editor"
    return None


def ensure_project_role(
    db: Session,
    case: AuditCase,
    user: User,
    allowed: set[str],
) -> User:
    role = resolve_project_role(db, case, user)
    if role is None or role not in allowed:
        raise AuditCaseAccessDenied("PROJECT_ROLE_REQUIRED")
    return user


def can_submit_project_candidate(db: Session, case: AuditCase, user: User) -> bool:
    return resolve_project_role(db, case, user) in _CANDIDATE_SUBMIT_ROLES


def can_approve_project_candidate(db: Session, case: AuditCase, user: User) -> bool:
    return resolve_project_role(db, case, user) in _CANDIDATE_APPROVE_ROLES
