from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.audit_cases.schema import AuditCaseCreate
from app.audit_cases.service import AuditCaseService
from app.audit_cases.storage import read_case_blob
from app.db.models import AuditCaseMaterialChunk, User
from app.documents.extraction import DocumentExtractionError
from app.security.auth import hash_password


def _service_with_case(tmp_path, monkeypatch) -> tuple[AuditCaseService, User, object]:
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="ocr-owner",
        tenant_id="ocr-tenant",
        username="ocr-owner",
        password_hash=hash_password("secret"),
    )
    db.add(owner)
    db.commit()
    service = AuditCaseService(db)
    case = service.create_case(
        owner,
        AuditCaseCreate(
            tenant_id="ocr-tenant",
            organization_name="OCR 测试企业",
            report_type="认证审核",
        ),
    )
    return service, owner, case


def test_material_processing_persists_full_text_metadata_and_page_refs(tmp_path, monkeypatch) -> None:
    service, _owner, case = _service_with_case(tmp_path, monkeypatch)
    source_text = "BEGIN-SENTINEL\n" + ("审核记录段落。\n" * 2_500) + "END-SENTINEL"
    material = service.add_material(
        case,
        case_owner := service.db.get(User, "ocr-owner"),
        "audit_record_form",
        "扫描审核记录.txt",
        "text/plain",
        source_text.encode("utf-8"),
    )

    processed = service.process_material(case, material)

    assert processed.extraction_status == "succeeded"
    assert processed.processing_status == "succeeded"
    assert processed.characters == len(source_text)
    assert processed.page_count == 1
    assert processed.extraction_method == "native"
    assert processed.extraction_engine == "text"
    assert processed.extracted_text_sha256
    assert processed.extracted_text_storage_key
    assert read_case_blob(processed.extracted_text_storage_key).decode("utf-8") == source_text
    chunks = service.db.exec(
        select(AuditCaseMaterialChunk)
        .where(AuditCaseMaterialChunk.material_id == processed.id)
        .order_by(AuditCaseMaterialChunk.chunk_index)
    ).all()
    assert chunks
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(source_text)
    assert "BEGIN-SENTINEL" in "".join(chunk.content for chunk in chunks)
    assert "END-SENTINEL" in "".join(chunk.content for chunk in chunks)
    assert all(chunk.page_refs_json == ["page:1"] for chunk in chunks)
    assert case_owner is not None


def test_material_processing_failure_preserves_raw_blob_and_retry_resets_state(
    tmp_path, monkeypatch
) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)
    material = service.add_material(
        case,
        owner,
        "audit_record_form",
        "扫描审核记录.pdf",
        "application/pdf",
        b"raw-pdf-bytes",
    )
    monkeypatch.setattr(
        "app.audit_cases.service.extract_document",
        lambda _filename, _data: (_ for _ in ()).throw(
            DocumentExtractionError("OCR_TIMEOUT", "timed out")
        ),
    )

    failed = service.process_material(case, material)

    assert failed.extraction_status == "failed"
    assert failed.processing_status == "failed"
    assert failed.error_code == "OCR_TIMEOUT"
    assert service.read_material_source(material.id) == b"raw-pdf-bytes"

    queued = service.retry_material(case, owner, material.id)

    assert queued.processing_status == "pending"
    assert queued.extraction_status == "pending"
    assert queued.error_code is None
    assert queued.processing_job_id


def test_enqueue_is_idempotent_for_current_material(tmp_path, monkeypatch) -> None:
    service, owner, case = _service_with_case(tmp_path, monkeypatch)
    material = service.add_material(
        case,
        owner,
        "audit_record_form",
        "记录.txt",
        "text/plain",
        b"audit record",
    )
    jobs: list[dict[str, str]] = []

    def fake_enqueue(name, func, *args, metadata=None, **kwargs):
        jobs.append({"name": name, "material_id": metadata["material_id"], "job_id": metadata["job_id"]})
        return type("Job", (), {"id": metadata["job_id"]})()

    monkeypatch.setattr("app.audit_cases.service.enqueue_async_job", fake_enqueue)

    first = service.enqueue_material_processing(case, owner, material.id)
    second = service.enqueue_material_processing(case, owner, material.id)

    assert first.id == second.id
    assert len(jobs) == 1
    assert service.get_material(case.id, material.id, owner).processing_status == "pending"
