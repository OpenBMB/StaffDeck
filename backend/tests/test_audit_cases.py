from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.audit_cases.chunking import chunk_text
from app.audit_cases.schema import AuditCaseCreate, AuditCaseNotFound, AuditCaseReadOnly
from app.audit_cases.service import AuditCaseService
from app.audit_cases.storage import read_case_blob, write_case_blob
from app.db.models import AuditCase, AuditCaseMaterial, AuditCaseMaterialChunk, User
from app.security.auth import hash_password


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _service_with_case(
    tmp_path,
    monkeypatch,
    *,
    organization_name: str = "示例企业",
) -> tuple[AuditCaseService, User, AuditCase]:
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    db = _test_session()
    owner = User(
        id="user_a",
        tenant_id="tenant_a",
        username="owner",
        password_hash=hash_password("secret"),
    )
    db.add(owner)
    db.commit()
    service = AuditCaseService(db)
    case = service.create_case(
        owner,
        AuditCaseCreate(
            tenant_id="tenant_a",
            organization_name=organization_name,
            report_type="再认证",
        ),
    )
    return service, owner, case


def test_audit_case_tables_enforce_material_identity() -> None:
    with _test_session() as db:
        case = AuditCase(
            tenant_id="tenant_a",
            owner_user_id="user_a",
            organization_name="示例企业",
            report_type="再认证",
        )
        db.add(case)
        db.commit()

        material = AuditCaseMaterial(
            audit_case_id=case.id,
            tenant_id="tenant_a",
            attachment_id="attachment-1",
            material_type="audit_record",
            filename="审核记录.pdf",
            content_type="application/pdf",
            sha256="a" * 64,
            size=1024,
            storage_key="case/material/raw",
            version=1,
        )
        db.add(material)
        db.commit()

        assert material.extraction_status == "pending"
        assert material.processing_status == "pending"


def test_audit_case_material_sha_is_unique_within_tenant_and_case() -> None:
    with _test_session() as db:
        case = AuditCase(
            tenant_id="tenant_a",
            owner_user_id="user_a",
            organization_name="示例企业",
            report_type="再认证",
        )
        db.add(case)
        db.commit()

        first = AuditCaseMaterial(
            audit_case_id=case.id,
            tenant_id="tenant_a",
            attachment_id="attachment-1",
            material_type="audit_record",
            filename="审核记录.pdf",
            content_type="application/pdf",
            sha256="b" * 64,
            size=1,
            storage_key="case/material/first",
            version=1,
        )
        duplicate = AuditCaseMaterial(
            audit_case_id=case.id,
            tenant_id="tenant_a",
            attachment_id="attachment-2",
            material_type="audit_record",
            filename="审核记录-副本.pdf",
            content_type="application/pdf",
            sha256="b" * 64,
            size=1,
            storage_key="case/material/duplicate",
            version=1,
        )
        db.add(first)
        db.commit()
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()


def test_case_blob_path_cannot_escape_tenant_or_case(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)

    key = write_case_blob(
        tenant_id="../../tenant",
        audit_case_id="../case",
        material_id="material",
        name="raw",
        data=b"audit evidence",
    )

    assert read_case_blob(key) == b"audit evidence"
    assert (tmp_path / key).resolve().is_relative_to((tmp_path / "audit_cases").resolve())


def test_chunk_text_covers_every_character_without_overlap() -> None:
    text = ("第一段审核记录。\n" * 90) + ("超长段落" * 700)

    chunks = chunk_text(text)

    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    assert all(left.end_char == right.start_char for left, right in zip(chunks, chunks[1:]))
    assert "".join(chunk.content for chunk in chunks) == text
    assert all(
        chunk.content_sha256 == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        for chunk in chunks
    )


def test_same_sha_is_reused_and_changed_file_creates_version(tmp_path, monkeypatch) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)

    first = service.add_material(
        case,
        owner,
        "audit_record",
        "审核记录.txt",
        "text/plain",
        b"version-one",
    )
    duplicate = service.add_material(
        case,
        owner,
        "audit_record",
        "审核记录.txt",
        "text/plain",
        b"version-one",
    )
    second = service.add_material(
        case,
        owner,
        "audit_record",
        "审核记录.txt",
        "text/plain",
        b"version-two",
    )

    assert duplicate.id == first.id
    assert second.version == 2
    assert second.supersedes_material_id == first.id
    assert second.is_current is True
    assert first.is_current is False


def test_case_access_never_falls_back_to_organization_name(tmp_path, monkeypatch) -> None:
    service, owner, first_case = _service_with_case(
        tmp_path,
        monkeypatch,
        organization_name="同名企业",
    )
    second_case = service.create_case(
        owner,
        AuditCaseCreate(
            tenant_id=owner.tenant_id,
            organization_name="同名企业",
            report_type="监督",
        ),
    )
    material = service.add_material(
        first_case,
        owner,
        "audit_record",
        "记录.txt",
        "text/plain",
        b"secret",
    )

    with pytest.raises(AuditCaseNotFound):
        service.get_material(second_case.id, material.id, owner)


def test_process_material_persists_full_text_and_contiguous_chunks(tmp_path, monkeypatch) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)
    text = ("审核证据段落。\n" * 120) + ("末尾证据" * 300)
    material = service.add_material(
        case,
        owner,
        "audit_record",
        "记录.txt",
        "text/plain",
        text.encode("utf-8"),
    )

    processed = service.process_material(case, material)

    assert processed.extraction_status == "succeeded"
    assert processed.processing_status == "succeeded"
    assert processed.characters == len(text)
    assert processed.extracted_text_storage_key
    assert read_case_blob(processed.extracted_text_storage_key).decode("utf-8") == text.encode("utf-8").decode("utf-8")
    chunks = service.db.exec(
        select(AuditCaseMaterialChunk).where(AuditCaseMaterialChunk.material_id == material.id)
    ).all()
    assert chunks
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    assert all(left.end_char == right.start_char for left, right in zip(chunks, chunks[1:]))
    assert "".join(chunk.content for chunk in chunks) == text


def test_process_material_failure_keeps_source_and_error_state(tmp_path, monkeypatch) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)
    source = b"legacy binary document"
    material = service.add_material(
        case,
        owner,
        "audit_record",
        "记录.doc",
        "application/msword",
        source,
    )

    processed = service.process_material(case, material)

    assert processed.extraction_status == "failed"
    assert processed.processing_status == "failed"
    assert processed.error_code
    assert read_case_blob(processed.storage_key) == source


def test_archived_case_rejects_new_material(tmp_path, monkeypatch) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)
    service.archive_case(case, owner)

    with pytest.raises(AuditCaseReadOnly, match="AUDIT_CASE_READ_ONLY"):
        service.add_material(
            case,
            owner,
            "audit_record",
            "补充记录.txt",
            "text/plain",
            b"new material",
        )
