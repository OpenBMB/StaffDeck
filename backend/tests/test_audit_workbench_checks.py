from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.documents import AuditCaseDocumentService
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseDocumentCreate,
    AuditCaseDocumentVersionCreate,
)
from app.audit_cases.workbench_checks import (
    DocumentCheckError,
    check_is_stale,
    check_read,
    create_check,
    recover_document_checks,
    retry_check,
    run_document_check,
)
from app.db.models import (
    AuditCase,
    AuditCaseMemberRole,
    ProjectRuleBinding,
    RuleDefinition,
    Tenant,
    User,
)
from app.db.workbench_checks import AuditDocumentCheck


@pytest.fixture
def check_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'checks.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner = User(id="owner", tenant_id="demo", username="owner", password_hash="test")
        viewer = User(id="viewer", tenant_id="demo", username="viewer", password_hash="test")
        foreign = User(id="foreign", tenant_id="other", username="foreign", password_hash="test")
        case = AuditCase(
            id="case",
            tenant_id="demo",
            owner_user_id=owner.id,
            member_user_ids_json=[viewer.id],
            organization_name="企业甲",
            report_type="再认证",
        )
        db.add_all(
            [
                Tenant(id="demo", name="Demo"),
                Tenant(id="other", name="Other"),
                owner,
                viewer,
                foreign,
                case,
                AuditCaseMemberRole(
                    tenant_id="demo", audit_case_id=case.id, user_id=viewer.id, role="viewer"
                ),
            ]
        )
        db.commit()
        service = AuditCaseDocumentService(db)
        doc = service.create_document(
            case,
            owner,
            AuditCaseDocumentCreate(
                document_key="report",
                title="报告",
                document_type="audit_report",
                zone="workspace",
                content_format="markdown",
                content="企业名称：企业甲\n认证范围：机械制造\nscope: manufacturing",
            ),
        )
        ref = service.create_document(
            case,
            owner,
            AuditCaseDocumentCreate(
                document_key="plan",
                title="计划",
                document_type="audit_plan",
                zone="workspace",
                content_format="markdown",
                content="企业名称：企业甲\n认证范围：食品生产",
            ),
        )
        yield db, engine, owner, viewer, foreign, case, doc, ref


def test_check_pins_versions_and_reports_cross_file_evidence(check_context):
    db, engine, owner, _, _, case, doc, ref = check_context
    job = create_check(db, case, owner, doc.id, [ref.id], "first")
    run_document_check(engine, job.id, job.generation)
    db.refresh(job)
    assert job.status == "completed"
    mismatch = next(f for f in job.findings_json if f["code"] == "CROSS_FIELD_MISMATCH")
    assert "机械制造" in mismatch["evidence_excerpt"]
    assert "食品生产" in mismatch["evidence_excerpt"]
    assert mismatch["reference"]["document_version_id"] == ref.active_version_id
    assert any(f["code"] == "RULES_UNCONFIGURED" for f in job.findings_json)
    old_results = list(job.findings_json)
    AuditCaseDocumentService(db).create_version(
        case,
        owner,
        ref.id,
        AuditCaseDocumentVersionCreate(
            expected_version=1,
            content_format="markdown",
            content="认证范围：机械制造",
        ),
    )
    assert check_is_stale(db, case, job)
    assert job.findings_json == old_results


def test_check_idempotent_request_and_actor_scope(check_context):
    db, _, owner, viewer, foreign, case, doc, ref = check_context
    job = create_check(db, case, owner, doc.id, [ref.id], "same")
    assert create_check(db, case, owner, doc.id, [ref.id, ref.id], "same").id == job.id
    with pytest.raises(DocumentCheckError, match="CHECK_REQUEST_KEY_REUSED"):
        create_check(db, case, owner, doc.id, [], "same")
    db.rollback()
    for denied in [viewer, foreign]:
        with pytest.raises(AuditCaseAccessDenied):
            create_check(db, case, denied, doc.id, [], "denied")
    assert len(db.exec(select(AuditDocumentCheck)).all()) == 1


def test_cross_case_reference_is_rejected(check_context):
    db, _, owner, _, _, case, doc, _ = check_context
    other = AuditCase(
        id="other-case",
        tenant_id=case.tenant_id,
        owner_user_id=owner.id,
        organization_name="乙",
        report_type="初审",
    )
    db.add(other)
    db.commit()
    ref = AuditCaseDocumentService(db).create_document(
        other,
        owner,
        AuditCaseDocumentCreate(
            document_key="other",
            title="其他项目",
            document_type="work_document",
            zone="workspace",
            content_format="text",
            content="secret",
        ),
    )
    with pytest.raises(DocumentCheckError, match="CHECK_DOCUMENT_NOT_FOUND"):
        create_check(db, case, owner, doc.id, [ref.id], "foreign-ref")


def test_rule_snapshot_runs_exact_document_binding(check_context):
    db, engine, owner, _, _, case, doc, ref = check_context
    db.add_all(
        [
            ProjectRuleBinding(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                document_id=doc.id,
                document_version_id=doc.active_version_id,
                rule_set_id="rules",
                rule_set_version_id="v1",
                status="current",
                bound_by_user_id=owner.id,
            ),
            RuleDefinition(
                tenant_id=case.tenant_id,
                rule_set_version_id="v1",
                rule_key="scope.equal",
                name="范围校验",
                execution_level="mandatory",
                execution_method="deterministic",
                condition_json={"operator": "equals", "field_key": "scope", "value": "other"},
            ),
            RuleDefinition(
                tenant_id=case.tenant_id,
                rule_set_version_id="v1",
                rule_key="semantic",
                name="审核充分性",
                execution_level="mandatory",
                execution_method="model_assisted",
                condition_json={"operator": "required", "field_key": "scope"},
            ),
        ]
    )
    db.commit()
    job = create_check(db, case, owner, doc.id, [], "bound")
    run_document_check(engine, job.id, job.generation)
    db.refresh(job)
    assert job.status == "completed"
    assert any(f["code"] == "RULE_FAILED" and f["severity"] == "error" for f in job.findings_json)
    assert any(f["code"] == "RULE_REQUIRES_REVIEW" for f in job.findings_json)
    unrelated = create_check(db, case, owner, ref.id, [], "unbound")
    assert unrelated.rule_snapshot_json == []


def test_interrupted_check_is_retryable_without_duplicate_effects(check_context):
    db, engine, owner, _, _, case, doc, _ = check_context
    job = create_check(db, case, owner, doc.id, [], "interrupt")
    recover_document_checks(engine)
    db.refresh(job)
    assert job.status == "failed" and job.error_code == "CHECK_INTERRUPTED"
    retry = retry_check(db, case, owner, job.id, "retry-one")
    assert retry.generation == 2 and retry.status == "queued"
    assert retry_check(db, case, owner, job.id, "retry-one").generation == 2
    run_document_check(engine, job.id, 1)
    db.refresh(job)
    assert job.status == "queued"
    run_document_check(engine, job.id, 2)
    run_document_check(engine, job.id, 2)
    db.refresh(job)
    assert job.status == "completed"
    assert len(db.exec(select(AuditDocumentCheck)).all()) == 1


def test_stale_failed_check_must_be_recreated(check_context):
    db, engine, owner, _, _, case, doc, _ = check_context
    job = create_check(db, case, owner, doc.id, [], "stale")
    recover_document_checks(engine)
    AuditCaseDocumentService(db).create_version(
        case,
        owner,
        doc.id,
        AuditCaseDocumentVersionCreate(
            expected_version=1,
            content_format="text",
            content="new version",
        ),
    )
    with pytest.raises(DocumentCheckError, match="CHECK_STALE_CREATE_NEW"):
        retry_check(db, case, owner, job.id, "retry")
    db.rollback()
    assert check_read(db, case, job)["stale"]
