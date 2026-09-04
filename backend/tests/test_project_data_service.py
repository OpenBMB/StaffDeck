from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AuditCase,
    AuditCaseMemberRole,
    ProjectDataConflict,
    ProjectDataValue,
    User,
)
from app.project_data.fields import (
    SYSTEM_FIELD_DEFINITIONS,
    get_field_definition,
    validate_candidate_request,
    validate_field_value,
)
from app.project_data.schema import (
    ProjectDataCandidateCreate,
    ProjectDataConflictError,
    ProjectFieldValidationError,
    SourceRef,
)
from app.project_data.service import ProjectDataService


def _user(user_id: str, tenant_id: str, role: str = "member") -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role=role,
        source="web",
        password_hash="test",
    )


@pytest.fixture
def service_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'project-data.db'}")
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = _user("owner-1", "tenant_demo")
    editor = _user("editor-1", "tenant_demo")
    reviewer = _user("reviewer-1", "tenant_demo")
    case = AuditCase(
        id="case-1",
        tenant_id="tenant_demo",
        owner_user_id=owner.id,
        member_user_ids_json=[editor.id, reviewer.id],
        organization_name="甲公司",
        report_type="认证审核",
    )
    db.add_all(
        [
            owner,
            editor,
            reviewer,
            case,
            AuditCaseMemberRole(
                id="reviewer-role",
                tenant_id="tenant_demo",
                audit_case_id=case.id,
                user_id=reviewer.id,
                role="reviewer",
            ),
        ]
    )
    db.commit()
    yield db, case, editor, reviewer
    db.close()


def _submit(db, case, editor, value, expected_revision=None):
    return ProjectDataService(db).submit_candidate(
        case,
        editor,
        ProjectDataCandidateCreate(
            field_key="organization.legal_name",
            value=value,
            source=SourceRef(
                material_id="material-1",
                location="page:2",
                evidence_excerpt=str(value),
            ),
            expected_revision=expected_revision,
        ),
    )


def _submit_and_approve(db, case, editor, reviewer, value):
    candidate = _submit(db, case, editor, value)
    return ProjectDataService(db).approve_candidate(
        candidate.id, reviewer, expected_current_revision=None, reason="资料确认"
    )


def test_field_keys_are_stable_and_unique() -> None:
    keys = [item.field_key for item in SYSTEM_FIELD_DEFINITIONS]
    # The approved field list contains 37 entries; the prior plan's count of 36
    # omitted one of the explicitly listed document fields.
    assert len(keys) == 37
    assert len(keys) == len(set(keys))
    assert "certification_project.scope" in keys


def test_date_field_rejects_non_iso_date() -> None:
    definition = get_field_definition("audit_event.start_date", tenant_id="tenant_demo")
    with pytest.raises(ProjectFieldValidationError, match="INVALID_DATE"):
        validate_field_value(definition, "2026/09/04")


def test_failed_or_empty_source_cannot_be_approved() -> None:
    request = ProjectDataCandidateCreate(
        field_key="organization.legal_name",
        value="",
        source=SourceRef(material_id="material-1", location="page:1", evidence_excerpt=""),
    )
    with pytest.raises(ProjectFieldValidationError, match="EMPTY_VALUE"):
        validate_candidate_request(request)


def test_candidate_is_pending_and_approval_creates_revision(service_context) -> None:
    db, case, editor, reviewer = service_context
    candidate = _submit(db, case, editor, "甲公司", expected_revision=0)
    assert candidate.status == "pending"

    approved = ProjectDataService(db).approve_candidate(
        candidate.id, reviewer, expected_current_revision=0, reason="资料确认"
    )
    assert approved.value_json == "甲公司"
    assert approved.status == "approved"
    assert approved.revision == 1


def test_stale_candidate_creates_conflict_instead_of_overwriting(service_context) -> None:
    db, case, editor, reviewer = service_context
    first = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    first_revision = first.revision
    second = _submit(db, case, editor, "乙公司", expected_revision=first_revision)
    _submit_and_approve(db, case, editor, reviewer, "丙公司")

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            second.id,
            reviewer,
            expected_current_revision=first_revision,
            reason="冲突测试",
        )

    conflict = ProjectDataService(db).list_conflicts(case, reviewer)[0]
    assert conflict.status == "open"
    current = db.exec(
        select(ProjectDataValue).where(
            ProjectDataValue.audit_case_id == case.id,
            ProjectDataValue.field_key == "organization.legal_name",
        )
    ).one()
    # The intervening approval remains authoritative; the stale candidate must
    # not overwrite it.
    assert current.value_json == "丙公司"


def test_rejected_candidate_never_changes_approved_value(service_context) -> None:
    db, case, editor, reviewer = service_context
    current = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    candidate = _submit(db, case, editor, "错误值", expected_revision=current.revision)
    ProjectDataService(db).reject_candidate(candidate.id, reviewer, "证据不足")
    db.refresh(current)
    assert current.value_json == "甲公司"
    assert current.revision == 1


def test_conflict_resolution_applies_selected_candidate(service_context) -> None:
    db, case, editor, reviewer = service_context
    first = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    first_revision = first.revision
    stale = _submit(db, case, editor, "乙公司", expected_revision=first_revision)
    _submit_and_approve(db, case, editor, reviewer, "丙公司")
    with pytest.raises(ProjectDataConflictError):
        ProjectDataService(db).approve_candidate(
            stale.id, reviewer, expected_current_revision=first_revision, reason="冲突"
        )
    resolved = ProjectDataService(db).resolve_conflict(
        db.exec(select(ProjectDataConflict)).first().id,
        stale.id,
        reviewer,
        "采用补充证据",
    )
    assert resolved.value_json == "乙公司"
    assert resolved.revision == 3
