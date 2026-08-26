from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.db import get_session
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    Tenant,
    User,
)
from app.main import app
from app.security.auth import create_access_token


@pytest.fixture
def api_context(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    users = {
        "owner": User(
            id="user-owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="test",
        ),
        "member": User(
            id="user-member",
            tenant_id="tenant_demo",
            username="member",
            password_hash="test",
        ),
        "admin": User(
            id="user-admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash="test",
        ),
        "other_tenant": User(
            id="user-other",
            tenant_id="tenant_other",
            username="other",
            password_hash="test",
        ),
    }
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(Tenant(id="tenant_other", name="Other"))
        db.add_all(users.values())
        db.commit()

    def override_get_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = override_get_session
    try:
        yield TestClient(app), engine, users, tmp_path
    finally:
        app.dependency_overrides.pop(get_session, None)


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def test_audit_case_api_round_trip(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    headers = _headers(users["owner"])

    created = client.post(
        "/api/audit-cases",
        headers=headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "示例企业",
            "report_type": "再认证",
            "management_systems": ["GB/T 23331-2020"],
            "knowledge_base_version_ids": ["kbver-1"],
            "member_user_ids": ["user-member"],
        },
    )
    assert created.status_code == 200
    case = created.json()
    case_id = case["id"]
    assert case["member_user_ids"] == ["user-member"]

    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=headers,
        files={"files": ("记录.txt", "审核记录全文".encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()[0]["sha256"]
    assert "storage_key" not in uploaded.json()[0]

    processed = client.post(
        f"/api/audit-cases/{case_id}/process?tenant_id=tenant_demo",
        headers=headers,
    )
    assert processed.status_code == 200
    assert processed.json()[0]["processing_status"] == "succeeded"

    materials = client.get(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo",
        headers=headers,
    )
    assert materials.status_code == 200
    assert materials.json()[0]["extraction_status"] == "succeeded"
    assert "extracted_text_storage_key" not in materials.json()[0]

    coverage = client.get(
        f"/api/audit-cases/{case_id}/coverage?tenant_id=tenant_demo",
        headers=headers,
    )
    assert coverage.status_code == 200
    assert coverage.json()["file_coverage"] == 1.0
    assert coverage.json()["chunk_coverage"] == 1.0

    listed = client.get("/api/audit-cases?tenant_id=tenant_demo", headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [case_id]


def test_audit_case_api_enforces_project_scope_and_archive_read_only(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    headers = _headers(users["owner"])
    created = client.post(
        "/api/audit-cases",
        headers=headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "同名企业",
            "report_type": "监督",
        },
    )
    case_id = created.json()["id"]

    other_tenant = client.get(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_other",
        headers=_headers(users["other_tenant"]),
    )
    assert other_tenant.status_code == 404

    archived = client.post(
        f"/api/audit-cases/{case_id}/archive?tenant_id=tenant_demo",
        headers=headers,
    )
    assert archived.status_code == 200
    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=headers,
        files={"files": ("补充.txt", b"new", "text/plain")},
    )
    assert uploaded.status_code == 409
    assert uploaded.json()["detail"] == "AUDIT_CASE_READ_ONLY"


def test_tenant_admin_delete_removes_materials_and_storage_but_keeps_event(api_context) -> None:
    client, engine, users, tmp_path = api_context
    owner_headers = _headers(users["owner"])
    created = client.post(
        "/api/audit-cases",
        headers=owner_headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "待删除企业",
            "report_type": "再认证",
        },
    )
    case_id = created.json()["id"]
    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=owner_headers,
        files={"files": ("记录.txt", b"delete me", "text/plain")},
    )
    assert uploaded.status_code == 200

    with Session(engine) as db:
        material = db.exec(
            select(AuditCaseMaterial).where(AuditCaseMaterial.audit_case_id == case_id)
        ).one()
        storage_path = tmp_path / material.storage_key
        case_storage_path = storage_path.parent.parent
        assert storage_path.exists()
        assert case_storage_path.exists()

    forbidden = client.delete(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=owner_headers,
    )
    assert forbidden.status_code == 403

    deleted = client.delete(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
    )
    assert deleted.status_code == 204

    with Session(engine) as db:
        assert db.get(AuditCase, case_id) is None
        assert db.exec(
            select(AuditCaseMaterial).where(AuditCaseMaterial.audit_case_id == case_id)
        ).all() == []
        assert db.exec(
            select(AuditCaseMaterialChunk).where(AuditCaseMaterialChunk.audit_case_id == case_id)
        ).all() == []
        events = db.exec(
            select(AuditCaseEvent).where(
                AuditCaseEvent.audit_case_id == case_id,
                AuditCaseEvent.event_type == "audit_case.deleted",
            )
        ).all()
        assert len(events) == 1
    assert not storage_path.exists()
    assert not case_storage_path.exists()
