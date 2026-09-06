from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.documents import AuditCaseDocumentService
from app.audit_cases.schema import AuditCaseDocumentCreate, AuditCaseDocumentVersionCreate
from app.db import get_session
from app.db.models import AuditCase, AuditCaseDocument, AuditCaseMemberRole, User
from app.main import app
from app.security.auth import create_access_token


@pytest.fixture
def ctx(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    users = {
        name: User(
            id=name,
            tenant_id="t",
            username=name,
            password_hash="x",
            role="admin" if name == "admin" else "member",
        )
        for name in ["owner", "editor", "reviewer", "viewer", "outsider", "admin"]
    }
    case = AuditCase(
        id="case",
        tenant_id="t",
        owner_user_id="owner",
        member_user_ids_json=["editor", "reviewer", "viewer"],
        organization_name="企业",
        report_type="再认证",
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([*users.values(), case])
        db.add_all(
            [
                AuditCaseMemberRole(tenant_id="t", audit_case_id="case", user_id=name, role=name)
                for name in ["editor", "reviewer", "viewer"]
            ]
        )
        db.commit()
        for key in ["primary", "reference"]:
            AuditCaseDocumentService(db).create_document(
                case,
                users["owner"],
                AuditCaseDocumentCreate(
                    document_key=key,
                    title=key,
                    document_type="audit_plan",
                    zone="workspace",
                    content_format="text",
                    content=key,
                ),
            )
        docs = {d.document_key: d.id for d in db.exec(select(AuditCaseDocument)).all()}

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session
    client = TestClient(app)

    def call(method, path="/cases/case", actor="owner", **kw):
        query = {"tenant_id": "t", **kw.pop("params", {})}
        return client.request(
            method,
            f"/api/audit-workbench{path}",
            params=query,
            headers={"Authorization": f"Bearer {create_access_token(users[actor])}"},
            **kw,
        )

    yield call, engine, docs
    app.dependency_overrides.pop(get_session, None)


def create(ctx, **overrides):
    call, _, docs = ctx
    response = call(
        "POST",
        "/cases/case/items",
        json={
            "document_id": docs["primary"],
            "process_number": 15,
            "assigned_to_user_id": "editor",
            "reviewer_user_id": "reviewer",
            "reference_document_ids": [docs["reference"]],
            **overrides,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def transition(ctx, item, action, actor, key=None, expected=None):
    return ctx[0](
        "POST",
        f"/cases/case/items/{item['id']}/transition",
        actor=actor,
        json={
            "action": action,
            "expected_revision": item["revision"] if expected is None else expected,
            "request_key": key or action,
        },
    )


def test_snapshot_is_read_only_and_denies_nonmembers(ctx):
    call, engine, _ = ctx
    response = call("GET")
    assert response.status_code == 200
    assert len(response.json()["processes"]) == 36
    assert response.json()["work_items"] == []
    assert call("GET", actor="outsider").status_code == 403
    assert (
        call(
            "POST",
            "/cases/case/items",
            actor="viewer",
            json={
                "document_id": "x",
                "process_number": 15,
                "assigned_to_user_id": "editor",
                "reviewer_user_id": "reviewer",
            },
        ).status_code
        == 403
    )
    with Session(engine) as db:
        from app.db.models import AuditWorkItem

        assert db.exec(select(AuditWorkItem)).all() == []


def test_process_17_requires_approved_process_16_and_reference(ctx):
    call, _, docs = ctx
    process_16 = create(
        ctx,
        process_number=16,
        document_id=docs["reference"],
        reference_document_ids=[],
    )
    process_16 = transition(ctx, process_16, "submit", "editor", key="p16-submit").json()
    assert transition(ctx, process_16, "approve", "reviewer", key="p16-approve").status_code == 200

    missing_reference = call(
        "POST",
        "/cases/case/items",
        json={
            "document_id": docs["primary"],
            "process_number": 17,
            "assigned_to_user_id": "editor",
            "reviewer_user_id": "reviewer",
            "reference_document_ids": [],
        },
    )
    assert missing_reference.status_code == 409
    assert missing_reference.json()["detail"] == "PROCESS_REFERENCE_REQUIRED"

    created = call(
        "POST",
        "/cases/case/items",
        json={
            "document_id": docs["primary"],
            "process_number": 17,
            "assigned_to_user_id": "editor",
            "reviewer_user_id": "reviewer",
            "reference_document_ids": [docs["reference"]],
        },
    )
    assert created.status_code == 200, created.text


def test_process_21_check_is_required_at_approval_not_submission(ctx):
    _, engine, docs = ctx
    process_16 = create(ctx, process_number=16)
    process_16 = transition(ctx, process_16, "submit", "editor", key="p16-for-p17-submit").json()
    process_16 = transition(ctx, process_16, "approve", "reviewer", key="p16-for-p17-approve").json()
    process_17 = create(ctx, process_number=17)
    process_17 = transition(ctx, process_17, "submit", "editor", key="p17-submit").json()
    process_17 = transition(ctx, process_17, "approve", "reviewer", key="p17-approve").json()
    process_21 = create(ctx, process_number=21)
    submitted = transition(ctx, process_21, "submit", "editor", key="p21-submit")
    assert submitted.status_code == 200, submitted.text

    blocked = transition(ctx, submitted.json(), "approve", "reviewer", key="p21-approve")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "PROCESS_CHECK_REQUIRED"

    from app.db.workbench_checks import AuditDocumentCheck

    with Session(engine) as db:
        db.add(
            AuditDocumentCheck(
                id="check-p21",
                tenant_id="t",
                audit_case_id="case",
                document_id=docs["primary"],
                document_version_id="",
                created_by_user_id="owner",
                request_key="check-p21",
                request_hash="hash",
                status="completed",
                findings_json=[],
            )
        )
        document = db.get(AuditCaseDocument, docs["primary"])
        db.get(AuditDocumentCheck, "check-p21").document_version_id = document.active_version_id
        db.commit()
    assert transition(ctx, submitted.json(), "approve", "reviewer", key="p21-approve2").status_code == 200


def test_process_23_does_not_require_process_19(ctx):
    process_18 = create(ctx, process_number=18)
    process_18 = transition(ctx, process_18, "submit", "editor", key="p18-submit").json()
    process_18 = transition(ctx, process_18, "approve", "reviewer", key="p18-approve").json()
    process_23 = create(ctx, process_number=23)
    assert process_23["process_number"] == 23


def test_process_gate_endpoint_reports_selected_document_gate(ctx):
    call, _, docs = ctx
    response = call("GET", "/cases/case/process-gates")
    assert response.status_code == 422

    response = call(
        "GET",
        "/cases/case/process-gates",
        params={"document_id": docs["primary"]},
    )
    assert response.status_code == 200, response.text
    rows = {row["process_number"]: row for row in response.json()}
    assert rows[17]["enabled"] is True
    assert rows[17]["ready"] is False
    assert rows[28]["enabled"] is False


def test_submit_approve_idempotency_revision_and_history(ctx):
    item = create(ctx)
    assert create(ctx)["id"] == item["id"]
    assert transition(ctx, item, "submit", "viewer").status_code == 403
    submitted = transition(ctx, item, "submit", "editor")
    assert submitted.status_code == 200, submitted.text
    submitted = submitted.json()
    assert transition(ctx, item, "submit", "editor").json() == submitted
    assert transition(ctx, submitted, "approve", "reviewer", key="submit").status_code == 409
    assert transition(ctx, submitted, "approve", "reviewer", expected=0).status_code == 409
    assert transition(ctx, submitted, "approve", "owner").status_code == 403
    approved = transition(ctx, submitted, "approve", "reviewer")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_version_id"] == submitted["document_version_id"]
    events = ctx[0]("GET", "/cases/case/events").json()
    assert [e["event_type"] for e in events] == ["item.created", "item.submit", "item.approve"]


def test_reference_staleness_blocks_approval_and_reopen_preserves_snapshot(ctx):
    item = create(ctx)
    submitted = transition(ctx, item, "submit", "editor").json()
    with Session(ctx[1]) as db:
        AuditCaseDocumentService(db).create_version(
            db.get(AuditCase, "case"),
            db.get(User, "owner"),
            ctx[2]["reference"],
            AuditCaseDocumentVersionCreate(
                expected_version=1, content_format="text", content="revised"
            ),
        )
    assert ctx[0]("GET").json()["work_items"][0]["stale"] is True
    assert transition(ctx, submitted, "approve", "reviewer").status_code == 409
    returned = transition(ctx, submitted, "request_changes", "reviewer").json()
    new_submission = transition(ctx, returned, "submit", "editor", key="submit2").json()
    approved = transition(ctx, new_submission, "approve", "reviewer", key="approve2").json()
    reopened = transition(ctx, approved, "reopen", "reviewer").json()
    assert reopened["status"] == "draft"
    assert reopened["approved_version_id"] == approved["approved_version_id"]


def test_issue_current_response_and_blocking_approval(ctx):
    item = create(ctx)
    call, engine, docs = ctx
    payload = {
        "work_item_id": item["id"],
        "kind": "nonconformity",
        "title": "核对范围",
        "blocking": True,
        "assigned_to_user_id": "editor",
    }
    assert call("POST", "/cases/case/issues", actor="editor", json=payload).status_code == 403
    issue_response = call("POST", "/cases/case/issues", actor="reviewer", json=payload)
    assert issue_response.status_code == 200, issue_response.text
    issue = issue_response.json()
    path = f"/cases/case/issues/{issue['id']}/transition"
    assert (
        call(
            "POST",
            path,
            actor="editor",
            json={"action": "respond", "response": "已修正", "request_key": "respond1"},
        ).status_code
        == 200
    )
    with Session(engine) as db:
        AuditCaseDocumentService(db).create_version(
            db.get(AuditCase, "case"),
            db.get(User, "owner"),
            docs["primary"],
            AuditCaseDocumentVersionCreate(
                expected_version=1, content_format="text", content="updated"
            ),
        )
    assert (
        call(
            "POST", path, actor="reviewer", json={"action": "close", "request_key": "close1"}
        ).status_code
        == 409
    )
    submitted = transition(ctx, item, "submit", "editor").json()
    assert transition(ctx, submitted, "approve", "reviewer").status_code == 409
    assert (
        call(
            "POST",
            path,
            actor="editor",
            json={"action": "respond", "response": "已核对当前版", "request_key": "respond2"},
        ).status_code
        == 200
    )
    assert (
        call(
            "POST", path, actor="reviewer", json={"action": "close", "request_key": "close2"}
        ).status_code
        == 200
    )
    assert transition(ctx, submitted, "approve", "reviewer").status_code == 200


def test_assignments_inbox_and_role_demotion_guard(ctx):
    item = create(ctx)
    call = ctx[0]
    assert call("GET", "/inbox", actor="admin").json()["items"] == []
    assert call("GET", "/inbox", actor="editor").json()["items"][0]["task_reason"] == "submit"
    assert (
        call("PUT", "/cases/case/members/reviewer/role", json={"role": "editor"}).status_code == 409
    )
    assert call("PUT", "/cases/case/members/owner/role", json={"role": "viewer"}).status_code == 409
    assert (
        call("PUT", "/cases/case/members/outsider/role", json={"role": "reviewer"}).status_code
        == 403
    )
    assert (
        call(
            "PUT", "/cases/case/members/viewer/role", actor="editor", json={"role": "reviewer"}
        ).status_code
        == 403
    )
    assert (
        call("PUT", "/cases/case/members/viewer/role", json={"role": "reviewer"}).status_code == 200
    )
    transition(ctx, item, "submit", "editor")
    assert call("GET", "/inbox", actor="reviewer").json()["items"][0]["task_reason"] == "review"


def test_self_review_and_out_of_scope_references_rejected(ctx):
    call, _, docs = ctx
    base = {
        "document_id": docs["primary"],
        "process_number": 15,
        "assigned_to_user_id": "editor",
        "reviewer_user_id": "owner",
    }
    assert call("POST", "/cases/case/items", json=base).status_code == 403
    assert (
        call(
            "POST",
            "/cases/case/items",
            json={
                **base,
                "reviewer_user_id": "reviewer",
                "reference_document_ids": ["foreign-document"],
            },
        ).status_code
        == 404
    )
    assert (
        call(
            "POST",
            "/cases/case/items",
            json={**base, "reviewer_user_id": "reviewer", "process_number": 1},
        ).status_code
        == 422
    )


def test_completed_report_exports_one_versioned_work_document(ctx):
    from app.db.models import AuditReportVersion, AuditReportSection

    call, engine, docs = ctx
    with Session(engine) as db:
        primary = db.get(AuditCaseDocument, docs["primary"])
        db.add(
            AuditReportVersion(
                id="report",
                tenant_id="t",
                audit_case_id="case",
                version=1,
                source_document_id=primary.id,
                source_document_version_id=primary.active_version_id,
                rule_set_version_ids_json=["rule-version-1"],
            )
        )
        db.add(
            AuditReportSection(
                id="section",
                tenant_id="t",
                audit_case_id="case",
                report_version_id="report",
                section_id="summary",
                title="审核结论",
                sequence=1,
                status="pending",
                draft_markdown="待复核结论",
                citation_ids_json=["evidence-1"],
            )
        )
        db.commit()
    path = "/cases/case/reports/report/work-document"
    assert call("POST", path, actor="viewer").status_code == 403
    assert call("POST", path).status_code == 409
    with Session(engine) as db:
        section = db.get(AuditReportSection, "section")
        section.status = "succeeded"
        db.add(section)
        db.commit()
    response = call("POST", path, actor="editor")
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["active_version"]["version"] == 1
    assert "待复核结论" in document["active_version"]["content"]
    assert "evidence-1" in document["active_version"]["content"]
    assert "rule-version-1" in document["active_version"]["content"]
    assert call("POST", path, actor="editor").json()["id"] == document["id"]
    assert call("POST", "/cases/case/reports/foreign/work-document").status_code == 404


def test_review_locks_only_primary_document_until_reopened(ctx):
    from app.audit_cases.schema import AuditCaseDocumentConflict

    item = create(ctx)
    submitted = transition(ctx, item, "submit", "editor").json()
    for status_item in [submitted, None]:
        if status_item is None:
            status_item = transition(ctx, submitted, "approve", "reviewer").json()
        with Session(ctx[1]) as db:
            with pytest.raises(AuditCaseDocumentConflict, match="DOCUMENT_REVIEW_LOCKED"):
                AuditCaseDocumentService(db).create_version(
                    db.get(AuditCase, "case"),
                    db.get(User, "owner"),
                    ctx[2]["primary"],
                    AuditCaseDocumentVersionCreate(
                        expected_version=1, content_format="text", content="changed"
                    ),
                )
    reopened = transition(ctx, status_item, "reopen", "reviewer").json()
    assert reopened["status"] == "draft"
    with Session(ctx[1]) as db:
        version = AuditCaseDocumentService(db).create_version(
            db.get(AuditCase, "case"),
            db.get(User, "owner"),
            ctx[2]["primary"],
            AuditCaseDocumentVersionCreate(
                expected_version=1, content_format="text", content="changed"
            ),
        )
        assert version.version == 2


def test_submitted_document_rules_cannot_change_through_legacy_service(ctx):
    from app.rules.service import RuleBindingError, RuleBindingService

    item = create(ctx)
    transition(ctx, item, "submit", "editor")
    with Session(ctx[1]) as db:
        with pytest.raises(RuleBindingError, match="DOCUMENT_REVIEW_LOCKED"):
            RuleBindingService(db).replace_current_bindings(
                db.get(AuditCase, "case"),
                ["unused"],
                db.get(User, "reviewer"),
                "manual",
                document_id=ctx[2]["primary"],
            )


def test_legacy_member_removal_cannot_strand_review(ctx):
    from app.audit_cases.schema import AuditCaseAccessDenied, AuditCaseMemberUpdate
    from app.audit_cases.service import AuditCaseService

    create(ctx)
    with Session(ctx[1]) as db:
        with pytest.raises(AuditCaseAccessDenied, match="PENDING_ASSIGNMENT_MEMBER_REQUIRED"):
            AuditCaseService(db).replace_members(
                db.get(AuditCase, "case"),
                db.get(User, "admin"),
                AuditCaseMemberUpdate(member_user_ids=["editor", "viewer"]),
            )


def test_project_rule_catalog_exposes_published_versions_only(ctx):
    from app.db.models import RuleSet, RuleSetVersion

    with Session(ctx[1]) as db:
        db.add(RuleSet(id="rules", tenant_id="t", key="rules", name="测试规则", status="active"))
        db.add_all(
            [
                RuleSetVersion(
                    id=status,
                    tenant_id="t",
                    rule_set_id="rules",
                    version=n,
                    status=status,
                    content_sha256="test",
                )
                for n, status in enumerate(["published", "draft"], 1)
            ]
        )
        db.commit()
    result = ctx[0]("GET", "/cases/case/rule-options", actor="viewer")
    assert result.status_code == 200, result.text
    assert len(result.json()) == 1
    assert result.json()[0]["version"]["id"] == "published"
    assert ctx[0]("GET", "/cases/case/rule-options", actor="outsider").status_code == 403


def test_approval_cannot_bypass_failed_machine_check(ctx):
    from app.db.workbench_checks import AuditDocumentCheck

    item = create(ctx)
    submitted = transition(ctx, item, "submit", "editor").json()
    with Session(ctx[1]) as db:
        db.add(
            AuditDocumentCheck(
                id="check",
                tenant_id="t",
                audit_case_id="case",
                document_id=item["document_id"],
                document_version_id=item["document_version_id"],
                created_by_user_id="editor",
                request_key="check",
                request_hash="hash",
                status="completed",
                findings_json=[{"code": "DOCUMENT_EMPTY", "severity": "error"}],
            )
        )
        db.commit()
    response = transition(ctx, submitted, "approve", "reviewer")
    assert response.status_code == 409
    assert response.json()["detail"] == "DOCUMENT_CHECK_BLOCKING"
    with Session(ctx[1]) as db:
        check = db.get(AuditDocumentCheck, "check")
        check.findings_json = [{"severity": "warning", "code": "RULES_UNCONFIGURED"}]
        db.add(check)
        db.commit()
    assert transition(ctx, submitted, "approve", "reviewer").status_code == 200

