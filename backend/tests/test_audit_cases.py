from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import paths
from app.audit_cases.chunking import chunk_text
from app.audit_cases.storage import read_case_blob, write_case_blob
from app.db.models import AuditCase, AuditCaseMaterial


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


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
