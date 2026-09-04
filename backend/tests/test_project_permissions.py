from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.audit_cases.schema import AuditCaseAccessDenied
from app.db.models import AuditCase, AuditCaseMemberRole, User
from app.project_data.permissions import (
    can_approve_project_candidate,
    can_submit_project_candidate,
    ensure_project_role,
    resolve_project_role,
)


def _user(
    user_id: str,
    *,
    tenant_id: str = "tenant_demo",
    role: str = "member",
) -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role=role,
        source="web",
        password_hash="test",
    )


@pytest.fixture
def permission_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'project-permissions.db'}")
    SQLModel.metadata.create_all(engine)
    owner = _user("owner")
    editor = _user("editor")
    reviewer = _user("reviewer")
    case = AuditCase(
        id="case-1",
        tenant_id="tenant_demo",
        owner_user_id=owner.id,
        member_user_ids_json=[editor.id],
        organization_name="甲公司",
        report_type="认证审核",
    )

    with Session(engine, expire_on_commit=False) as db:
        db.add_all(
            [
                owner,
                editor,
                reviewer,
                case,
                AuditCaseMemberRole(
                    id="role-editor",
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    user_id=editor.id,
                    role="editor",
                    created_by_user_id=owner.id,
                ),
                AuditCaseMemberRole(
                    id="role-reviewer",
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    user_id=reviewer.id,
                    role="reviewer",
                    created_by_user_id=owner.id,
                ),
            ]
        )
        db.commit()
        yield db, owner, editor, reviewer, case


def test_owner_is_project_admin_without_role_row(permission_context) -> None:
    db, owner, _editor, _reviewer, case = permission_context

    assert resolve_project_role(db, case, owner) == "project_admin"


def test_explicit_reviewer_can_approve_but_editor_cannot(permission_context) -> None:
    db, _owner, editor, reviewer, case = permission_context
    assert can_submit_project_candidate(db, case, editor) is True
    assert can_approve_project_candidate(db, case, editor) is False
    assert can_submit_project_candidate(db, case, reviewer) is True
    assert can_approve_project_candidate(db, case, reviewer) is True


def test_other_tenant_user_is_not_a_project_member(permission_context) -> None:
    db, _owner, _editor, _reviewer, case = permission_context
    other = _user("other", tenant_id="tenant_other")
    db.add(other)
    db.commit()

    assert resolve_project_role(db, case, other) is None


def test_tenant_admin_is_project_admin_only_within_own_tenant(permission_context) -> None:
    db, _owner, _editor, _reviewer, case = permission_context
    tenant_admin = _user("tenant-admin", role="admin")
    foreign_admin = _user("foreign-admin", tenant_id="tenant_other", role="admin")
    db.add_all([tenant_admin, foreign_admin])
    db.commit()
    assert resolve_project_role(db, case, tenant_admin) == "project_admin"
    assert can_submit_project_candidate(db, case, tenant_admin) is True
    assert can_approve_project_candidate(db, case, tenant_admin) is True
    assert resolve_project_role(db, case, foreign_admin) is None


def test_explicit_role_precedes_owner_and_legacy_membership(permission_context) -> None:
    db, owner, editor, _reviewer, case = permission_context
    db.add(
        AuditCaseMemberRole(
            id="role-owner-viewer",
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            user_id=owner.id,
            role="viewer",
            created_by_user_id=owner.id,
        )
    )
    editor_role = db.get(AuditCaseMemberRole, "role-editor")
    assert editor_role is not None
    editor_role.role = "viewer"
    db.add(editor_role)
    db.commit()
    assert resolve_project_role(db, case, owner) == "viewer"
    assert resolve_project_role(db, case, editor) == "viewer"
    assert can_submit_project_candidate(db, case, editor) is False


def test_legacy_member_without_role_row_remains_an_editor(permission_context) -> None:
    db, _owner, _editor, _reviewer, case = permission_context
    legacy_member = _user("legacy-member")
    case.member_user_ids_json = [*case.member_user_ids_json, legacy_member.id]
    db.add_all([legacy_member, case])
    db.commit()

    assert resolve_project_role(db, case, legacy_member) == "editor"


def test_ensure_project_role_returns_user_or_uses_one_non_disclosing_error(
    permission_context,
) -> None:
    db, _owner, editor, _reviewer, case = permission_context
    outsider = _user("outsider")
    foreign = _user("foreign", tenant_id="tenant_other")
    db.add_all([outsider, foreign])
    db.commit()
    assert ensure_project_role(db, case, editor, {"editor"}) is editor
    for unauthorized in (editor, outsider, foreign):
        with pytest.raises(AuditCaseAccessDenied, match="^PROJECT_ROLE_REQUIRED$"):
            ensure_project_role(db, case, unauthorized, {"reviewer"})
