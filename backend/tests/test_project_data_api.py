from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import paths
from app.db import get_session
from app.db.models import AuditCase, Tenant, User
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
    admin = _user("data-admin", "tenant_demo", role="admin")
    member = _user("data-member", "tenant_demo")
    case = AuditCase(
        id="data-case",
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
        )
    finally:
        app.dependency_overrides.pop(get_session, None)


def _source() -> dict[str, str]:
    return {
        "material_id": "material-1",
        "location": "page:2",
        "evidence_excerpt": "甲公司",
    }


def _candidate(
    client: TestClient,
    headers: dict[str, str],
    case_id: str,
    value: str,
    expected_revision: int | None = None,
):
    payload: dict[str, object] = {
        "field_key": "organization.legal_name",
        "value": value,
        "source": _source(),
    }
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    return client.post(
        f"/api/audit-cases/{case_id}/field-candidates?tenant_id=tenant_demo",
        json=payload,
        headers=headers,
    )


def test_field_schema_and_fields_never_synthesize_missing_values(api_context) -> None:
    client, _admin_headers, member_headers, case = api_context
    schema = client.get(
        f"/api/audit-cases/{case.id}/field-schema?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert schema.status_code == 200
    assert any(item["field_key"] == "organization.legal_name" for item in schema.json())

    fields = client.get(
        f"/api/audit-cases/{case.id}/fields?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert fields.status_code == 200
    assert fields.json() == []


def test_candidate_submission_returns_pending_and_does_not_approve(api_context) -> None:
    client, _admin_headers, member_headers, case = api_context
    response = _candidate(client, member_headers, case.id, "甲公司")
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"

    fields = client.get(
        f"/api/audit-cases/{case.id}/fields?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert fields.status_code == 200
    assert fields.json() == []

    candidates = client.get(
        f"/api/audit-cases/{case.id}/field-candidates?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert candidates.status_code == 200
    assert candidates.json()[0]["status"] == "pending"
    assert candidates.json()[0]["source"]["location"] == "page:2"


def test_admin_approval_creates_revision_and_reject_requires_reason(api_context) -> None:
    client, admin_headers, member_headers, case = api_context
    submitted = _candidate(client, member_headers, case.id, "甲公司")
    candidate_id = submitted.json()["id"]
    approved = client.post(
        f"/api/field-candidates/{candidate_id}/approve?tenant_id=tenant_demo",
        json={"expected_current_revision": 0, "reason": "材料核验"},
        headers=admin_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["revision"] == 1
    assert approved.json()["value"] == "甲公司"

    rejected_candidate = _candidate(client, member_headers, case.id, "乙公司").json()
    rejected = client.post(
        f"/api/field-candidates/{rejected_candidate['id']}/reject?tenant_id=tenant_demo",
        json={"reason": "与正式材料不一致"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"


def test_stale_approval_returns_conflict_without_overwriting_value(api_context) -> None:
    client, admin_headers, member_headers, case = api_context
    first = _candidate(client, member_headers, case.id, "甲公司").json()
    approved_first = client.post(
        f"/api/field-candidates/{first['id']}/approve?tenant_id=tenant_demo",
        json={"expected_current_revision": 0, "reason": "首次核验"},
        headers=admin_headers,
    ).json()
    current = _candidate(
        client, member_headers, case.id, "丙公司", approved_first["revision"]
    ).json()
    approved_current = client.post(
        f"/api/field-candidates/{current['id']}/approve?tenant_id=tenant_demo",
        json={
            "expected_current_revision": approved_first["revision"],
            "reason": "更新核验",
        },
        headers=admin_headers,
    )
    assert approved_current.status_code == 200, approved_current.text

    stale = _candidate(
        client, member_headers, case.id, "乙公司", approved_first["revision"]
    ).json()
    response = client.post(
        f"/api/field-candidates/{stale['id']}/approve?tenant_id=tenant_demo",
        json={
            "expected_current_revision": approved_first["revision"],
            "reason": "过期材料",
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "PROJECT_DATA_CONFLICT"

    fields = client.get(
        f"/api/audit-cases/{case.id}/fields?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert fields.json()[0]["value"] == "丙公司"


def test_project_data_rejects_cross_tenant_query_before_case_lookup(api_context) -> None:
    client, _admin_headers, member_headers, case = api_context
    response = client.get(
        f"/api/audit-cases/{case.id}/fields?tenant_id=tenant_other",
        headers=member_headers,
    )
    assert response.status_code == 403


def test_candidate_validation_returns_stable_error_codes(api_context) -> None:
    client, _admin_headers, member_headers, case = api_context
    unknown = client.post(
        f"/api/audit-cases/{case.id}/field-candidates?tenant_id=tenant_demo",
        json={"field_key": "not.declared", "value": "x", "source": _source()},
        headers=member_headers,
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "UNKNOWN_FIELD_KEY"

    missing_source = client.post(
        f"/api/audit-cases/{case.id}/field-candidates?tenant_id=tenant_demo",
        json={"field_key": "organization.legal_name", "value": "x"},
        headers=member_headers,
    )
    assert missing_source.status_code == 422
    assert missing_source.json()["detail"] == "SOURCE_REQUIRED"
