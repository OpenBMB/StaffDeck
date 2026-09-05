from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.api import audit_cases as audit_cases_module
from app.audit_cases import service as audit_case_service_module
from app.audit_cases.evidence_schema import ProcessingSummary
from app.audit_cases.knowledge import KnowledgeRetrievalSummary
from app.db import get_session
from app.db.models import (
    AuditCase,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    KnowledgeBaseVersion,
    ModelConfig,
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
        db.add(
            KnowledgeBaseVersion(
                id="kbver-1",
                tenant_id="tenant_demo",
                knowledge_base_id="kb-1",
                name="测试知识库",
                version="1.0.0",
                status="active",
            )
        )
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


def upload_material(
    client: TestClient,
    headers: dict[str, str],
    case_id: str,
    material_type: str,
    filename: str,
    data: bytes,
):
    return client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo"
        f"&material_type={material_type}",
        headers=headers,
        files={"files": (filename, data, "text/plain")},
    )


def test_same_hash_in_other_material_type_returns_category_conflict(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    admin = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "分类去重企业",
            "report_type": "监督",
        },
    )
    assert created.status_code == 200
    case_id = created.json()["id"]
    first = upload_material(client, admin, case_id, "audit_record", "same.txt", b"same")
    assert first.status_code == 200
    conflict = upload_material(client, admin, case_id, "audit_plan", "plan.txt", b"same")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "MATERIAL_CATEGORY_CONFLICT"


def test_explicit_replace_preserves_old_version_and_processes_only_new_material(
    api_context,
) -> None:
    client, _engine, users, _tmp_path = api_context
    admin = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "版本企业",
            "report_type": "再认证",
        },
    )
    assert created.status_code == 200
    case_id = created.json()["id"]
    first = upload_material(
        client, admin, case_id, "audit_record", "记录.txt", "第一版记录".encode("utf-8")
    )
    assert first.status_code == 200
    material_id = first.json()[0]["id"]
    replaced = client.post(
        f"/api/audit-cases/{case_id}/materials/{material_id}/replace"
        "?tenant_id=tenant_demo",
        headers=admin,
        files={"file": ("记录-v2.txt", "第二版记录".encode("utf-8"), "text/plain")},
    )
    assert replaced.status_code == 200
    replacement = replaced.json()
    assert replacement["version"] == 2
    assert replacement["supersedes_material_id"] == material_id
    history = client.get(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&include_history=true",
        headers=admin,
    )
    assert history.status_code == 200
    assert {item["version"] for item in history.json()} == {1, 2}
    assert [item["id"] for item in history.json() if item["is_current"]] == [
        replacement["id"]
    ]
    processed = client.post(
        f"/api/audit-cases/{case_id}/materials/{replacement['id']}/process"
        "?tenant_id=tenant_demo",
        headers=admin,
    )
    assert processed.status_code == 200
    assert processed.json()["id"] == replacement["id"]


def test_material_upload_rejects_legacy_doc_and_oversized_file(api_context, monkeypatch) -> None:
    client, _engine, users, _tmp_path = api_context
    admin = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "格式校验企业",
            "report_type": "监督",
        },
    )
    assert created.status_code == 200
    case_id = created.json()["id"]

    legacy = upload_material(client, admin, case_id, "audit_record", "旧记录.doc", b"data")
    assert legacy.status_code == 422
    assert legacy.json()["detail"] == "UNSUPPORTED_DOCUMENT_FORMAT"

    monkeypatch.setattr(audit_case_service_module, "MAX_AUDIT_MATERIAL_BYTES", 3)
    oversized = upload_material(client, admin, case_id, "audit_record", "大文件.txt", b"1234")
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "AUDIT_MATERIAL_TOO_LARGE"


def test_audit_case_management_api_is_admin_only(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    payload = {
        "tenant_id": "tenant_demo",
        "organization_name": "管理页面企业",
        "report_type": "再认证",
        "management_systems": ["GB/T 23331-2020"],
        "member_user_ids": ["user-member"],
    }

    member_create = client.post(
        "/api/audit-cases",
        headers=_headers(users["owner"]),
        json=payload,
    )
    assert member_create.status_code == 403

    created = client.post(
        "/api/audit-cases",
        headers=_headers(users["admin"]),
        json=payload,
    )
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    forbidden_update = client.patch(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=_headers(users["owner"]),
        json={"organization_name": "不应修改"},
    )
    assert forbidden_update.status_code == 403

    updated = client.patch(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
        json={"organization_name": "已修改企业"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["organization_name"] == "已修改企业"

    member_read = client.get(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=_headers(users["member"]),
    )
    assert member_read.status_code == 200

    members = client.put(
        f"/api/audit-cases/{case_id}/members?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
        json={"member_user_ids": ["user-member"]},
    )
    assert members.status_code == 200, members.text

    management = client.get(
        "/api/audit-cases/management?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
    )
    assert management.status_code == 200, management.text
    assert management.json()["total"] == 1
    assert management.json()["items"][0]["material_total"] == 0

    filtered_management = client.get(
        "/api/audit-cases/management?tenant_id=tenant_demo&q=不存在的企业",
        headers=_headers(users["admin"]),
    )
    assert filtered_management.status_code == 200
    assert filtered_management.json()["total"] == 0

    options = client.get(
        "/api/audit-cases/management-options?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
    )
    assert options.status_code == 200, options.text
    assert ".docx" in options.json()["supported_extensions"]

    events = client.get(
        f"/api/audit-cases/{case_id}/events?tenant_id=tenant_demo",
        headers=_headers(users["admin"]),
    )
    assert events.status_code == 200, events.text
    assert {event["event_type"] for event in events.json()} >= {
        "audit_case.created",
        "audit_case.updated",
        "audit_case.members_replaced",
    }


def test_audit_case_material_mutations_require_admin(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    created = client.post(
        "/api/audit-cases",
        headers=_headers(users["admin"]),
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "材料权限企业",
            "report_type": "再认证",
            "member_user_ids": ["user-member"],
        },
    )
    assert created.status_code == 200
    case_id = created.json()["id"]
    member_headers = _headers(users["member"])

    upload = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=member_headers,
        files={"files": ("记录.txt", b"audit record", "text/plain")},
    )
    assert upload.status_code == 403

    admin_upload = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=_headers(users["admin"]),
        files={"files": ("记录.txt", b"audit record", "text/plain")},
    )
    assert admin_upload.status_code == 200

    process = client.post(
        f"/api/audit-cases/{case_id}/process?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert process.status_code == 403

    archive = client.post(
        f"/api/audit-cases/{case_id}/archive?tenant_id=tenant_demo",
        headers=member_headers,
    )
    assert archive.status_code == 403


def test_audit_case_api_round_trip(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    headers = _headers(users["admin"])

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


def test_process_coverage_and_report_api_flow(api_context, monkeypatch) -> None:
    client, engine, users, _tmp_path = api_context
    headers = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "流程测试企业",
            "report_type": "再认证",
            "management_systems": ["GB/T 23331-2020"],
            "knowledge_base_version_ids": ["kbver-1"],
        },
    )
    case_id = created.json()["id"]
    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=headers,
        files={"files": ("记录.txt", b"audit record", "text/plain")},
    )
    assert uploaded.status_code == 200
    prepared = client.post(
        f"/api/audit-cases/{case_id}/process?tenant_id=tenant_demo",
        headers=headers,
    )
    assert prepared.status_code == 200

    with Session(engine) as db:
        db.add(
            ModelConfig(
                id="model-1",
                tenant_id="tenant_demo",
                name="测试模型",
                api_key_encrypted="not-used",
                model="test-model",
            )
        )
        db.commit()

    class _NoopEvidenceProcessor:
        def __init__(self, _db):
            pass

        def process_pending_chunks(self, _case, _model_config):
            return ProcessingSummary(total=1, succeeded=0, failed=0, skipped=1)

    class _NoopKnowledgeOrchestrator:
        def __init__(self, _db):
            pass

        def retrieve(self, _case, _model_config):
            return KnowledgeRetrievalSummary()

    monkeypatch.setattr(audit_cases_module, "AuditEvidenceProcessor", _NoopEvidenceProcessor)
    monkeypatch.setattr(audit_cases_module, "AuditKnowledgeOrchestrator", _NoopKnowledgeOrchestrator)

    processed = client.post(
        f"/api/audit-cases/{case_id}/process?tenant_id=tenant_demo",
        headers=headers,
        json={"model_config_id": "model-1"},
    )
    assert processed.status_code == 202, processed.text
    coverage = client.get(
        f"/api/audit-cases/{case_id}/coverage?tenant_id=tenant_demo",
        headers=headers,
    )
    assert coverage.status_code == 200
    assert set(coverage.json()) >= {
        "file_coverage",
        "chunk_coverage",
        "element_coverage",
        "publish_allowed",
    }


def test_audit_case_api_enforces_project_scope_and_archive_read_only(api_context) -> None:
    client, _engine, users, _tmp_path = api_context
    headers = _headers(users["admin"])
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
    admin_headers = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin_headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "待删除企业",
            "report_type": "再认证",
        },
    )
    case_id = created.json()["id"]
    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=admin_headers,
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
        headers=_headers(users["owner"]),
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
