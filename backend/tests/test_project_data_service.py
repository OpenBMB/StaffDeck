from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMemberRole,
    ProjectDataCandidate,
    ProjectDataConflict,
    ProjectDataFieldDefinition,
    ProjectDataValue,
    ProjectDataValueRevision,
    User,
)
from app.project_data.fields import (
    SYSTEM_FIELD_DEFINITIONS,
    FieldDefinition,
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


def test_empty_candidate_value_is_rejected() -> None:
    request = ProjectDataCandidateCreate(
        field_key="organization.legal_name",
        value="",
        source=SourceRef(
            material_id="material-1",
            location="page:1",
            evidence_excerpt="甲公司",
        ),
    )
    with pytest.raises(ProjectFieldValidationError, match="EMPTY_VALUE"):
        validate_candidate_request(request)


def test_missing_candidate_source_is_rejected() -> None:
    request = ProjectDataCandidateCreate(
        field_key="organization.legal_name",
        value="甲公司",
        source=None,
    )
    with pytest.raises(ProjectFieldValidationError, match="SOURCE_REQUIRED"):
        validate_candidate_request(request)


def test_unknown_tenant_value_type_is_rejected_with_stable_error() -> None:
    definition = FieldDefinition(
        field_key="tenant.custom",
        label="自定义字段",
        value_type="unsupported",
        information_domain="tenant",
        scope="project",
    )

    with pytest.raises(ProjectFieldValidationError, match="UNKNOWN_VALUE_TYPE") as exc_info:
        validate_field_value(definition, "值")

    assert exc_info.value.code == "UNKNOWN_VALUE_TYPE"


def test_tenant_source_optional_definition_allows_missing_source(service_context) -> None:
    db, case, editor, _reviewer = service_context
    db.add(
        ProjectDataFieldDefinition(
            id="tenant-source-optional",
            tenant_id=case.tenant_id,
            field_key="tenant.source_optional",
            label="可无来源字段",
            value_type="text",
            information_domain="tenant",
            scope="project",
            source_optional=True,
        )
    )
    db.commit()

    candidate = ProjectDataService(db).submit_candidate(
        case,
        editor,
        ProjectDataCandidateCreate(
            field_key="tenant.source_optional",
            value="人工确认值",
            source=None,
        ),
    )

    assert candidate.status == "pending"
    assert candidate.source_json == {}


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


def test_candidate_without_expected_revision_is_bound_to_submission_baseline(
    service_context,
) -> None:
    db, case, editor, reviewer = service_context
    candidate = _submit(db, case, editor, "乙公司", expected_revision=None)
    assert candidate.expected_revision == 0

    db.add(
        ProjectDataValue(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            field_key=candidate.field_key,
            value_json="甲公司",
            status="approved",
            revision=1,
        )
    )
    db.commit()

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            candidate.id,
            reviewer,
            expected_current_revision=None,
            reason="不得覆盖新值",
        )

    current = db.exec(select(ProjectDataValue)).one()
    assert current.value_json == "甲公司"
    assert current.revision == 1


def test_approval_revision_cannot_override_candidate_baseline(service_context) -> None:
    db, case, editor, reviewer = service_context
    candidate = _submit(db, case, editor, "乙公司", expected_revision=0)
    db.add(
        ProjectDataValue(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            field_key=candidate.field_key,
            value_json="甲公司",
            status="approved",
            revision=1,
        )
    )
    db.commit()

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            candidate.id,
            reviewer,
            expected_current_revision=1,
            reason="调用方版本不得替换候选基线",
        )

    current = db.exec(select(ProjectDataValue)).one()
    assert current.value_json == "甲公司"
    assert current.revision == 1


def test_different_pending_candidate_creates_conflict_before_mutation(service_context) -> None:
    db, case, editor, reviewer = service_context
    current = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    first = _submit(db, case, editor, "乙公司", expected_revision=current.revision)
    second = _submit(db, case, editor, "丙公司", expected_revision=current.revision)

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            first.id,
            reviewer,
            expected_current_revision=current.revision,
            reason="待审值不一致",
        )

    db.refresh(current)
    assert current.value_json == "甲公司"
    assert current.revision == 1
    assert db.get(ProjectDataCandidate, first.id).status == "pending"
    assert db.get(ProjectDataCandidate, second.id).status == "pending"
    conflict = db.exec(select(ProjectDataConflict)).one()
    assert set(conflict.candidate_ids_json) == {first.id, second.id}


def test_retrying_stale_approval_reuses_conflict_and_audit_event(service_context) -> None:
    db, case, editor, reviewer = service_context
    candidate = _submit(db, case, editor, "乙公司", expected_revision=0)
    db.add(
        ProjectDataValue(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            field_key=candidate.field_key,
            value_json="甲公司",
            status="approved",
            revision=1,
        )
    )
    db.commit()

    service = ProjectDataService(db)
    for _ in range(2):
        with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
            service.approve_candidate(
                candidate.id,
                reviewer,
                expected_current_revision=0,
                reason="重复冲突请求",
            )

    conflicts = db.exec(select(ProjectDataConflict)).all()
    events = db.exec(
        select(AuditCaseEvent).where(
            AuditCaseEvent.audit_case_id == case.id,
            AuditCaseEvent.event_type == "project_field_conflict_detected",
        )
    ).all()
    assert len(conflicts) == 1
    assert conflicts[0].trigger_candidate_id == candidate.id
    assert len(events) == 1


def test_stale_candidate_creates_conflict_instead_of_overwriting(service_context) -> None:
    db, case, editor, reviewer = service_context
    first = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    first_revision = first.revision
    second = _submit(db, case, editor, "乙公司", expected_revision=first_revision)
    first.value_json = "丙公司"
    first.revision = 2
    db.add(first)
    db.commit()

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
    first.value_json = "丙公司"
    first.revision = 2
    db.add(first)
    db.commit()
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


def test_interleaved_approval_loser_is_a_conflict_without_a_second_revision(
    service_context,
    monkeypatch,
) -> None:
    db, case, editor, reviewer = service_context
    losing_candidate = _submit(db, case, editor, "甲公司", expected_revision=0)
    winning_candidate = _submit(db, case, editor, "甲公司", expected_revision=0)
    service = ProjectDataService(db)
    original_apply = service._apply_candidate

    with Session(db.get_bind()) as winning_db:
        winning_reviewer = winning_db.get(User, reviewer.id)

        def apply_after_competing_approval(
            candidate_to_apply,
            case_to_update,
            actor,
            *,
            reason,
            conflict=None,
        ):
            ProjectDataService(winning_db).approve_candidate(
                winning_candidate.id,
                winning_reviewer,
                expected_current_revision=0,
                reason="先提交的审批",
            )
            return original_apply(
                candidate_to_apply,
                case_to_update,
                actor,
                reason=reason,
                conflict=conflict,
            )

        monkeypatch.setattr(service, "_apply_candidate", apply_after_competing_approval)

        with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
            service.approve_candidate(
                losing_candidate.id,
                reviewer,
                expected_current_revision=0,
                reason="后提交的审批",
            )

    db.expire_all()
    current = db.exec(select(ProjectDataValue)).one()
    revisions = db.exec(select(ProjectDataValueRevision)).all()
    assert current.value_json == "甲公司"
    assert current.revision == 1
    assert len(revisions) == 1
    assert db.get(ProjectDataCandidate, winning_candidate.id).status == "approved"
    assert db.get(ProjectDataCandidate, losing_candidate.id).status == "pending"


def test_interleaved_approval_loses_to_conflict_resolution_with_domain_error(
    service_context,
    monkeypatch,
) -> None:
    db, case, editor, reviewer = service_context
    current = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    selected = _submit(db, case, editor, "乙公司", expected_revision=current.revision)
    competing = _submit(db, case, editor, "丙公司", expected_revision=current.revision)

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            selected.id,
            reviewer,
            expected_current_revision=current.revision,
            reason="形成待解决冲突",
        )
    open_conflict = db.exec(select(ProjectDataConflict)).one()
    ProjectDataService(db).reject_candidate(competing.id, reviewer, "不采用竞争值")

    service = ProjectDataService(db)
    original_apply = service._apply_candidate
    with Session(db.get_bind()) as winning_db:
        winning_reviewer = winning_db.get(User, reviewer.id)

        def apply_after_conflict_resolution(
            candidate_to_apply,
            case_to_update,
            actor,
            *,
            reason,
            conflict=None,
        ):
            ProjectDataService(winning_db).resolve_conflict(
                open_conflict.id,
                selected.id,
                winning_reviewer,
                "冲突解决先提交",
            )
            return original_apply(
                candidate_to_apply,
                case_to_update,
                actor,
                reason=reason,
                conflict=conflict,
            )

        monkeypatch.setattr(service, "_apply_candidate", apply_after_conflict_resolution)

        with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
            service.approve_candidate(
                selected.id,
                reviewer,
                expected_current_revision=current.revision,
                reason="普通审批后提交",
            )

    db.expire_all()
    final_value = db.exec(select(ProjectDataValue)).one()
    revisions = db.exec(
        select(ProjectDataValueRevision).order_by(ProjectDataValueRevision.revision)
    ).all()
    resolved_conflict = db.get(ProjectDataConflict, open_conflict.id)
    assert final_value.value_json == "乙公司"
    assert final_value.revision == 2
    assert [revision.revision for revision in revisions] == [1, 2]
    assert resolved_conflict.status == "resolved"
    assert resolved_conflict.resolved_candidate_id == selected.id
