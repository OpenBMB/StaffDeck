from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.db import get_session
from app.db.models import AuditCase, RuleDefinition, RuleEvaluation, Tenant, User
from app.main import app
from app.security.auth import create_access_token


def _user(user_id: str, tenant_id: str, *, role: str = "member") -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role=role,
        password_hash="test",
    )


@pytest.fixture
def api_context(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = _user("api-admin", "tenant_demo", role="admin")
    member = _user("api-member", "tenant_demo")
    case = AuditCase(
        id="api-case",
        tenant_id="tenant_demo",
        owner_user_id=member.id,
        member_user_ids_json=[member.id],
        organization_name="甲公司",
        report_type="认证审核",
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add_all([admin, member, case])
        db.commit()

    def override_get_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = override_get_session
    try:
        yield (
            TestClient(app),
            {"Authorization": f"Bearer {create_access_token(admin)}"},
            {"Authorization": f"Bearer {create_access_token(member)}"},
            case,
            engine,
        )
    finally:
        app.dependency_overrides.pop(get_session, None)


def _rule_payload() -> dict[str, object]:
    return {
        "rule_key": "scope.required",
        "name": "认证范围必填",
        "workflow_nodes": ["collect"],
        "information_domains": ["certification_project"],
        "document_types": ["audit_plan"],
        "field_keys": ["certification_project.scope"],
        "execution_level": "mandatory",
        "execution_method": "deterministic",
        "condition": {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        "evidence_requirements": [{"kind": "project_field"}],
        "source_refs": [{"internal_policy_ref": "POL-001"}],
        "sequence": 0,
        "enabled": True,
    }


def _create_rule_set(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/api/rule-sets",
        json={
            "tenant_id": "tenant_demo",
            "key": "api.audit",
            "name": "API 审核规则",
            "management_systems": ["能源管理体系"],
            "audit_types": ["认证审核"],
            "business_domain": "audit",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_and_publish(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    rule_set = _create_rule_set(client, headers)
    version_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        json=[_rule_payload()],
        headers=headers,
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    publish_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{version['id']}/publish"
        "?tenant_id=tenant_demo",
        headers=headers,
    )
    assert publish_response.status_code == 200, publish_response.text
    return rule_set, publish_response.json()


def test_non_admin_cannot_create_rule_set(api_context) -> None:
    client, _admin_headers, member_headers, _case, _engine = api_context
    response = client.post(
        "/api/rule-sets",
        json={
            "tenant_id": "tenant_demo",
            "key": "member.audit",
            "name": "越权规则",
        },
        headers=member_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "RULE_ADMIN_REQUIRED"


def test_admin_can_create_validate_publish_and_list_rule_versions(api_context) -> None:
    client, admin_headers, _member_headers, _case, _engine = api_context
    rule_set = _create_rule_set(client, admin_headers)
    version_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        json={"rules": [_rule_payload()]},
        headers=admin_headers,
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()

    validate_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{version['id']}/validate"
        "?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert validate_response.status_code == 200
    assert validate_response.json()["errors"] == []

    publish_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{version['id']}/publish"
        "?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert publish_response.status_code == 200
    assert publish_response.json()["status"] == "published"

    listed = client.get(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == version["id"]


def test_project_binding_requires_published_version_and_pins_it(api_context) -> None:
    client, admin_headers, member_headers, case, _engine = api_context
    rule_set = _create_rule_set(client, admin_headers)
    draft_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        json=[_rule_payload()],
        headers=admin_headers,
    )
    assert draft_response.status_code == 201
    draft = draft_response.json()

    rejected = client.put(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_demo",
        json={"version_ids": [draft["id"]], "selection_source": "manual"},
        headers=member_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "PUBLISHED_RULE_VERSION_REQUIRED"

    published = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{draft['id']}/publish"
        "?tenant_id=tenant_demo",
        headers=admin_headers,
    ).json()
    bound = client.put(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_demo",
        json={"version_ids": [published["id"]], "selection_source": "recommended"},
        headers=member_headers,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()[0]["rule_set_version_id"] == published["id"]


def test_project_api_does_not_leak_cross_tenant_case(api_context) -> None:
    client, _admin_headers, member_headers, case, _engine = api_context
    response = client.get(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_other",
        headers=member_headers,
    )
    assert response.status_code == 403


def test_project_rule_migration_preview_and_evaluation_listing(api_context) -> None:
    client, admin_headers, member_headers, case, _engine = api_context
    rule_set, first = _create_and_publish(client, admin_headers)
    second_rule = _rule_payload()
    second_rule["name"] = "认证范围必填（修订版）"
    draft_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        json=[second_rule],
        headers=admin_headers,
    )
    assert draft_response.status_code == 201, draft_response.text
    second_draft = draft_response.json()
    second_response = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{second_draft['id']}/publish"
        "?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()

    bound = client.put(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_demo",
        json={"version_ids": [first["id"]]},
        headers=member_headers,
    )
    assert bound.status_code == 200, bound.text
    direct_replace = client.put(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_demo",
        json={"version_ids": [second["id"]]},
        headers=member_headers,
    )
    assert direct_replace.status_code == 409
    assert direct_replace.json()["detail"] == "RULE_MIGRATION_CONFIRMATION_REQUIRED"

    preview = client.post(
        f"/api/audit-cases/{case.id}/rule-bindings/migration-preview"
        "?tenant_id=tenant_demo",
        json={"version_ids": [second["id"]]},
        headers=member_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["changed_rule_keys"] == ["scope.required"]

    migrated = client.post(
        f"/api/audit-cases/{case.id}/rule-bindings/migrate?tenant_id=tenant_demo",
        json={"version_ids": [second["id"]], "reason": "规则文字修订"},
        headers=member_headers,
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()[0]["rule_set_version_id"] == second["id"]

    evaluations = client.get(
        f"/api/audit-cases/{case.id}/rule-evaluations?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert evaluations.status_code == 200
    assert evaluations.json() == []


def test_rule_exception_requires_explicit_rule_opt_in(api_context) -> None:
    client, admin_headers, member_headers, case, engine = api_context
    _rule_set, published = _create_and_publish(client, admin_headers)
    bound = client.put(
        f"/api/audit-cases/{case.id}/rule-bindings?tenant_id=tenant_demo",
        json={"version_ids": [published["id"]]},
        headers=member_headers,
    )
    assert bound.status_code == 200, bound.text
    with Session(engine, expire_on_commit=False) as db:
        rule = db.exec(select(RuleDefinition)).first()
        evaluation = RuleEvaluation(
            tenant_id="tenant_demo",
            audit_case_id=case.id,
            rule_set_version_id=published["id"],
            rule_definition_id=rule.id,
            workflow_node="collect",
            information_domain="certification_project",
            target_ref="",
            input_revision=1,
            status="failed",
            result_json={"status": "failed", "blocking": True},
            evidence_refs_json=[],
            executor_type="deterministic",
        )
        db.add(evaluation)
        db.commit()
        evaluation_id = evaluation.id

    response = client.post(
        f"/api/rule-evaluations/{evaluation_id}/exception?tenant_id=tenant_demo",
        json={"reason": "人工确认"},
        headers=member_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "RULE_EXCEPTION_NOT_ALLOWED"
