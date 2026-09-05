from __future__ import annotations

import hashlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit_cases.documents import AuditCaseDocumentService
from app.audit_cases.schema import (
    AuditCaseAccessDenied,
    AuditCaseDocumentConflict,
    AuditCaseDocumentCreate,
    AuditCaseDocumentFormatError,
    AuditCaseDocumentVersionCreate,
    AuditCaseReadOnly,
)
from app.db.models import (
    AuditCase,
    AuditCaseDocumentVersion,
    AuditCaseEvent,
    AuditCaseMaterial,
    AuditCaseMemberRole,
    Tenant,
    User,
)


@pytest.fixture
def document_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit-case-documents.db'}")
    SQLModel.metadata.create_all(engine)
    owner = User(
        id="user-owner",
        tenant_id="tenant-demo",
        username="owner",
        display_name="项目管理员",
        role="member",
        source="web",
        password_hash="test",
    )
    viewer = User(
        id="user-viewer",
        tenant_id="tenant-demo",
        username="viewer",
        display_name="查看者",
        role="member",
        source="web",
        password_hash="test",
    )
    case = AuditCase(
        id="case-documents",
        tenant_id="tenant-demo",
        owner_user_id=owner.id,
        member_user_ids_json=[viewer.id],
        organization_name="示例组织",
        report_type="再认证",
    )
    material = AuditCaseMaterial(
        id="material-source",
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        attachment_id="attachment-source",
        material_type="audit_plan",
        filename="原始审核计划.md",
        content_type="text/markdown",
        sha256="original-material-sha",
        size=42,
        storage_key="audit-cases/case-documents/material-source/raw",
        extracted_text_storage_key="audit-cases/case-documents/material-source/text",
        characters=42,
        extraction_status="completed",
        processing_status="completed",
    )
    viewer_role = AuditCaseMemberRole(
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        user_id=viewer.id,
        role="viewer",
        created_by_user_id=owner.id,
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id=case.tenant_id, name="Demo"))
        db.add_all([owner, viewer, case, material, viewer_role])
        db.commit()
        yield db, owner, viewer, case, material


def _create_request(*, content: str = "# 审核计划\n保留内部换行\n") -> AuditCaseDocumentCreate:
    return AuditCaseDocumentCreate(
        document_key="audit-plan",
        title="审核计划",
        document_type="audit_plan",
        zone="workspace",
        content_format="markdown",
        content=content,
        change_note="创建草稿",
        source_material_id="material-source",
    )


def test_create_document_creates_initial_version_and_leaves_source_material_unchanged(
    document_context,
) -> None:
    db, owner, _viewer, case, material = document_context
    before_material = {
        "sha256": material.sha256,
        "storage_key": material.storage_key,
        "extracted_text_storage_key": material.extracted_text_storage_key,
        "characters": material.characters,
    }

    document = AuditCaseDocumentService(db).create_document(case, owner, _create_request())
    loaded, versions = AuditCaseDocumentService(db).get_document(case, owner, document.id)

    assert loaded.active_version_id == versions[0].id
    assert [(version.version, version.content) for version in versions] == [
        (1, "# 审核计划\n保留内部换行\n")
    ]
    assert versions[0].content_sha256 == hashlib.sha256(
        "# 审核计划\n保留内部换行\n".encode()
    ).hexdigest()
    assert versions[0].characters == len("# 审核计划\n保留内部换行\n")
    assert db.get(AuditCaseMaterial, material.id).model_dump(
        include=before_material.keys()
    ) == before_material
    assert [event.event_type for event in db.exec(select(AuditCaseEvent)).all()] == [
        "audit_case.document_created"
    ]


def test_create_version_appends_snapshot_and_preserves_original_version(document_context) -> None:
    db, owner, _viewer, case, _material = document_context
    service = AuditCaseDocumentService(db)
    document = service.create_document(case, owner, _create_request(content="first\n"))

    version = service.create_version(
        case,
        owner,
        document.id,
        AuditCaseDocumentVersionCreate(
            expected_version=1,
            content_format="text",
            content="second\nwith a line break\n",
            change_note="补充范围",
        ),
    )
    _loaded, versions = service.get_document(case, owner, document.id)

    assert version.version == 2
    assert [(item.version, item.content) for item in versions] == [
        (1, "first\n"),
        (2, "second\nwith a line break\n"),
    ]
    assert db.get(AuditCaseDocumentVersion, versions[0].id).content == "first\n"
    assert [event.event_type for event in db.exec(select(AuditCaseEvent)).all()] == [
        "audit_case.document_created",
        "audit_case.document_version_created",
    ]


def test_viewer_can_list_work_documents_in_workspace_order(document_context) -> None:
    db, owner, viewer, case, _material = document_context
    service = AuditCaseDocumentService(db)
    service.create_document(
        case,
        owner,
        _create_request().model_copy(
            update={"document_key": "closing", "title": "末次会议", "zone": "report"}
        ),
    )
    service.create_document(
        case,
        owner,
        _create_request().model_copy(
            update={"document_key": "opening", "title": "首次会议", "zone": "meeting"}
        ),
    )

    documents = service.list_documents(case, viewer)

    assert [(document.zone, document.document_key) for document in documents] == [
        ("meeting", "opening"),
        ("report", "closing"),
    ]


def test_create_version_rejects_stale_expected_version(document_context) -> None:
    db, owner, _viewer, case, _material = document_context
    service = AuditCaseDocumentService(db)
    document = service.create_document(case, owner, _create_request(content="first"))
    service.create_version(
        case,
        owner,
        document.id,
        AuditCaseDocumentVersionCreate(
            expected_version=1,
            content_format="text",
            content="second",
        ),
    )

    with pytest.raises(AuditCaseDocumentConflict, match="^DOCUMENT_VERSION_CONFLICT$"):
        service.create_version(
            case,
            owner,
            document.id,
            AuditCaseDocumentVersionCreate(
                expected_version=1,
                content_format="text",
                content="stale write",
            ),
        )


def test_viewer_cannot_create_or_version_work_document(document_context) -> None:
    db, owner, viewer, case, _material = document_context
    service = AuditCaseDocumentService(db)
    document = service.create_document(case, owner, _create_request())

    with pytest.raises(AuditCaseAccessDenied, match="^PROJECT_ROLE_REQUIRED$"):
        service.create_document(case, viewer, _create_request())
    with pytest.raises(AuditCaseAccessDenied, match="^PROJECT_ROLE_REQUIRED$"):
        service.create_version(
            case,
            viewer,
            document.id,
            AuditCaseDocumentVersionCreate(
                expected_version=1,
                content_format="markdown",
                content="# 越权修改",
            ),
        )


def test_archived_case_cannot_accept_document_writes(document_context) -> None:
    db, owner, _viewer, case, _material = document_context
    case.status = "archived"
    db.add(case)
    db.commit()

    with pytest.raises(AuditCaseReadOnly, match="^AUDIT_CASE_READ_ONLY$"):
        AuditCaseDocumentService(db).create_document(case, owner, _create_request())


def test_archive_document_freezes_document_and_records_reason(document_context) -> None:
    db, owner, _viewer, case, _material = document_context
    service = AuditCaseDocumentService(db)
    document = service.create_document(case, owner, _create_request())

    archived = service.archive_document(case, owner, document.id, "审核结论已确认")

    assert archived.status == "archived"
    assert archived.archive_reason == "审核结论已确认"
    with pytest.raises(AuditCaseReadOnly, match="^AUDIT_CASE_DOCUMENT_READ_ONLY$"):
        service.create_version(
            case,
            owner,
            document.id,
            AuditCaseDocumentVersionCreate(
                expected_version=1,
                content_format="markdown",
                content="# 不应写入",
            ),
        )
    event = db.exec(
        select(AuditCaseEvent).where(AuditCaseEvent.event_type == "audit_case.document_archived")
    ).one()
    assert event.metadata_json == {"status": "archived"}


def test_document_content_only_accepts_markdown_or_text(document_context) -> None:
    db, owner, _viewer, case, _material = document_context

    with pytest.raises(
        AuditCaseDocumentFormatError,
        match="^DOCUMENT_CONTENT_FORMAT_UNSUPPORTED$",
    ):
        AuditCaseDocumentService(db).create_document(
            case,
            owner,
            _create_request(content="binary").model_copy(update={"content_format": "docx"}),
        )
