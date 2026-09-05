from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import paths
from app.db import get_session
from app.db.models import User
from app.main import app
from app.security.auth import create_access_token


def _user(user_id: str, *, role: str) -> User:
    return User(
        id=user_id,
        tenant_id="tenant_demo",
        username=user_id,
        role=role,
        password_hash="test",
    )


@pytest.fixture
def phase1_client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    admin = _user("phase1-admin", role="admin")
    member = _user("phase1-member", role="member")
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([admin, member])
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
        )
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_phase1_project_rule_and_data_flow_is_auditable(phase1_client) -> None:
    client, admin_headers, member_headers = phase1_client
    rule_set = client.post(
        "/api/rule-sets",
        json={
            "tenant_id": "tenant_demo",
            "key": "phase1.audit",
            "name": "Phase 1 审核规则",
        },
        headers=admin_headers,
    ).json()
    rule = {
        "rule_key": "scope.required",
        "name": "认证范围必填",
        "workflow_nodes": ["collect"],
        "information_domains": ["certification_project"],
        "field_keys": ["certification_project.scope"],
        "execution_level": "mandatory",
        "execution_method": "deterministic",
        "condition": {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        "evidence_requirements": [{"kind": "project_field"}],
        "source_refs": [{"internal_policy_ref": "POL-001"}],
    }
    version = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions?tenant_id=tenant_demo",
        json=[rule],
        headers=admin_headers,
    ).json()
    published = client.post(
        f"/api/rule-sets/{rule_set['id']}/versions/{version['id']}/publish"
        "?tenant_id=tenant_demo",
        headers=admin_headers,
    ).json()
    created_case = client.post(
        "/api/audit-cases",
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "甲公司",
            "report_type": "认证审核",
            "member_user_ids": ["phase1-member"],
        },
        headers=admin_headers,
    )
    assert created_case.status_code == 200, created_case.text
    case = created_case.json()

    binding = client.put(
        f"/api/audit-cases/{case['id']}/rule-bindings?tenant_id=tenant_demo",
        json={
            "rule_set_version_ids": [published["id"]],
            "selection_source": "manual",
        },
        headers=admin_headers,
    )
    assert binding.status_code == 200, binding.text

    candidate = client.post(
        f"/api/audit-cases/{case['id']}/field-candidates?tenant_id=tenant_demo",
        json={
            "field_key": "organization.legal_name",
            "value": "甲公司",
            "source": {
                "material_id": "material-1",
                "location": "page:2",
                "evidence_excerpt": "甲公司",
            },
            "expected_revision": 0,
        },
        headers=member_headers,
    )
    assert candidate.status_code == 201, candidate.text
    approved = client.post(
        f"/api/field-candidates/{candidate.json()['id']}/approve?tenant_id=tenant_demo",
        json={"expected_current_revision": 0, "reason": "材料核验"},
        headers=admin_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    events = client.get(
        f"/api/audit-cases/{case['id']}/events?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert events.status_code == 200, events.text
    event_types = {item["event_type"] for item in events.json()}
    assert {
        "rule_binding_created",
        "project_field_candidate_created",
        "project_field_approved",
    } <= event_types
