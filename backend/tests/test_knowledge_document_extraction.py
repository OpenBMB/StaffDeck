from __future__ import annotations

import base64
import hashlib

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Tenant,
    utc_now,
)
from app.documents.extraction import (
    DocumentExtractionError,
    DocumentExtractionResult,
    ExtractedPage,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import IngestPayload, KnowledgeService


def test_scanned_pdf_ingest_persists_full_text_page_refs_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_one = "## 第 1 页\n\n这是扫描件第一页。"
    page_two_tail = "TAIL-NEVER-TRUNCATE"
    page_two = "## 第 2 页\n\n" + ("第二页完整正文。" * 2500) + page_two_tail
    full_text = "# PDF 文档\n\n" + page_one + "\n\n" + page_two
    source_bytes = b"%PDF-1.4 scanned"

    result = DocumentExtractionResult(
        text=full_text,
        pages=[
            ExtractedPage(page_number=1, text="这是扫描件第一页。", char_count=len("这是扫描件第一页。")),
            ExtractedPage(page_number=2, text=("第二页完整正文。" * 2500) + page_two_tail, char_count=len(("第二页完整正文。" * 2500) + page_two_tail)),
        ],
        page_refs=["page:1", "page:2"],
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_page_count=2,
        method="structured",
        engine="rapiddoc-ort",
        engine_version="0.0-test",
        warnings=["OCR_USED", "LOW_CONFIDENCE_TABLE"],
        char_count=len(full_text),
        table_count=1,
    )

    class FakeExtractor:
        def extract(self, filename: str, content: bytes) -> DocumentExtractionResult:
            assert filename == "scan.pdf"
            assert content == source_bytes
            return result

    monkeypatch.setattr("app.knowledge.parser._build_document_extractor", lambda settings=None: FakeExtractor())

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        db.commit()
        service = KnowledgeService(db)
        job = service.create_ingest_job(
            IngestPayload(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                filename="scan.pdf",
                content_base64=_b64_bytes(source_bytes),
            )
        )

        service._run_ingest_job(job.id)

        refreshed_job = db.get(KnowledgeIngestJob, job.id)
        assert refreshed_job is not None
        assert refreshed_job.status == "succeeded"
        document = db.get(KnowledgeDocument, refreshed_job.document_id)
        assert document is not None
        assert document.metadata_json["raw_text"] == full_text
        assert page_two_tail in document.metadata_json["raw_text"]
        assert document.file_type == "pdf"

        extraction = document.metadata_json["extraction"]
        assert extraction["method"] == "structured"
        assert extraction["engine"] == "rapiddoc-ort"
        assert extraction["engine_version"] == "0.0-test"
        assert extraction["warnings"] == ["OCR_USED", "LOW_CONFIDENCE_TABLE"]
        assert extraction["page_refs"] == ["page:1", "page:2"]

        source = document.metadata_json["source"]
        assert source["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert source["text_sha256"] == hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        assert source["source_page_count"] == 2

        chunks = db.exec(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document.id)
            .order_by(KnowledgeChunk.chunk_index)
        ).all()
        assert chunks
        assert any(page_two_tail in chunk.content for chunk in chunks)
        assert any(chunk.metadata_json["page_refs"] == ["page:2"] for chunk in chunks)
        assert any("第 2 页" in str(chunk.source_ref) for chunk in chunks)

        source_document = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.document_id == document.id,
                KnowledgeConcept.concept_type == "Source Document",
            )
        ).one()
        assert source_document.source_refs_json[0]["source_sha256"] == source["source_sha256"]
        assert source_document.source_refs_json[0]["page_refs"] == ["page:1", "page:2"]


def test_failed_ingest_preserves_blob_and_duplicate_source_sha_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes = b"%PDF-1.4 retry"
    full_text = "# PDF 文档\n\n## 第 1 页\n\n重试成功后的完整正文。"
    success_result = DocumentExtractionResult(
        text=full_text,
        pages=[ExtractedPage(page_number=1, text="重试成功后的完整正文。", char_count=len("重试成功后的完整正文。"))],
        page_refs=["page:1"],
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_page_count=1,
        method="structured",
        engine="rapiddoc-ort",
        engine_version="0.0-test",
        warnings=["OCR_USED"],
        char_count=len(full_text),
        table_count=0,
    )

    class FlakyExtractor:
        def __init__(self) -> None:
            self.calls = 0

        def extract(self, filename: str, content: bytes) -> DocumentExtractionResult:
            assert filename == "retry.pdf"
            assert content == source_bytes
            self.calls += 1
            if self.calls == 1:
                raise DocumentExtractionError("OCR_TIMEOUT", "OCR_TIMEOUT: timed out")
            return success_result

    extractor = FlakyExtractor()
    monkeypatch.setattr("app.knowledge.parser._build_document_extractor", lambda settings=None: extractor)

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        db.commit()
        service = KnowledgeService(db)
        first_job = service.create_ingest_job(
            IngestPayload(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                filename="retry.pdf",
                content_base64=_b64_bytes(source_bytes),
            )
        )

        service._run_ingest_job(first_job.id)

        failed_job = db.get(KnowledgeIngestJob, first_job.id)
        assert failed_job is not None
        assert failed_job.status == "failed"
        assert failed_job.error == "OCR_TIMEOUT: OCR_TIMEOUT: timed out"
        assert failed_job.metadata_json["content_base64"] == _b64_bytes(source_bytes)
        assert db.exec(select(KnowledgeDocument)).all() == []

        service._run_ingest_job(first_job.id)

        retried_job = db.get(KnowledgeIngestJob, first_job.id)
        assert retried_job is not None
        assert retried_job.status == "succeeded"
        first_document = db.get(KnowledgeDocument, retried_job.document_id)
        assert first_document is not None
        first_chunk_count = len(
            db.exec(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == first_document.id)
            ).all()
        )
        assert first_chunk_count > 0

        duplicate_job = service.create_ingest_job(
            IngestPayload(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                filename="retry.pdf",
                content_base64=_b64_bytes(source_bytes),
            )
        )
        service._run_ingest_job(duplicate_job.id)

        duplicate_refreshed = db.get(KnowledgeIngestJob, duplicate_job.id)
        assert duplicate_refreshed is not None
        assert duplicate_refreshed.status == "succeeded"
        assert duplicate_refreshed.document_id == first_document.id

        documents = db.exec(select(KnowledgeDocument)).all()
        assert len(documents) == 1
        chunks = db.exec(select(KnowledgeChunk).where(KnowledgeChunk.document_id == first_document.id)).all()
        assert len(chunks) == first_chunk_count


def test_failed_ingest_after_partial_persistence_cleans_derived_rows_and_hides_concepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes = b"%PDF-1.4 partial-failure"
    full_text = "# 扫描文档\n\n## 第 1 页\n\n这里有一段必须在失败后清理的正文。"
    extraction = DocumentExtractionResult(
        text=full_text,
        pages=[
            ExtractedPage(
                page_number=1,
                text="这里有一段必须在失败后清理的正文。",
                char_count=len("这里有一段必须在失败后清理的正文。"),
            )
        ],
        page_refs=["page:1"],
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_page_count=1,
        method="structured",
        engine="rapiddoc-ort",
        engine_version="0.0-test",
        warnings=["OCR_USED"],
        char_count=len(full_text),
        table_count=0,
    )

    class StableExtractor:
        def extract(self, filename: str, content: bytes) -> DocumentExtractionResult:
            assert filename == "partial.pdf"
            assert content == source_bytes
            return extraction

    failure_message = "SIMULATED_DISCOVERY_FAILURE"

    def fail_after_partial_persistence(
        self: KnowledgeService,
        tenant_id: str,
        knowledge_base_id: str,
        document: KnowledgeDocument,
        buckets: list[KnowledgeBucket],
        job: KnowledgeIngestJob | None,
    ) -> None:
        assert document.id
        assert buckets
        assert self.db.exec(select(KnowledgeConcept).where(KnowledgeConcept.document_id == document.id)).all()
        assert self.db.exec(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)).all()
        self.db.add(
            KnowledgeDiscoverySuggestion(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=document.knowledge_base_version_id,
                document_id=document.id,
                bucket_id=buckets[0].id,
                suggestion_type="warning",
                title="Injected failure suggestion",
                status="pending",
                payload_json={"failure": True},
                source_refs_json=[{"document_id": document.id}],
                reason="Injected before failure",
                updated_at=utc_now(),
            )
        )
        self.db.commit()
        raise RuntimeError(failure_message)

    monkeypatch.setattr("app.knowledge.parser._build_document_extractor", lambda settings=None: StableExtractor())
    monkeypatch.setattr(KnowledgeService, "_discover_from_document", fail_after_partial_persistence)

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        db.commit()
        service = KnowledgeService(db)
        job = service.create_ingest_job(
            IngestPayload(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                filename="partial.pdf",
                content_base64=_b64_bytes(source_bytes),
            )
        )

        service._run_ingest_job(job.id)

        failed_job = db.get(KnowledgeIngestJob, job.id)
        assert failed_job is not None
        assert failed_job.status == "failed"
        assert failed_job.error == failure_message
        assert failed_job.metadata_json["content_base64"] == _b64_bytes(source_bytes)
        assert failed_job.document_id is not None

        failed_document = db.get(KnowledgeDocument, failed_job.document_id)
        assert failed_document is not None
        assert failed_document.status == "failed"
        assert failed_document.error == failure_message
        assert failed_document.metadata_json["raw_text"] == full_text
        assert failed_document.bucket_count == 0
        assert failed_document.chunk_count == 0

        assert db.exec(select(KnowledgeConcept).where(KnowledgeConcept.document_id == failed_document.id)).all() == []
        assert db.exec(select(KnowledgeBucket).where(KnowledgeBucket.document_id == failed_document.id)).all() == []
        assert db.exec(select(KnowledgeChunk).where(KnowledgeChunk.document_id == failed_document.id)).all() == []
        assert (
            db.exec(
                select(KnowledgeDiscoverySuggestion).where(
                    KnowledgeDiscoverySuggestion.document_id == failed_document.id
                )
            ).all()
            == []
        )

        loaded_concepts = service._load_concepts_for_search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="失败后清理",
                mode="debug",
            )
        )
        assert loaded_concepts == []


def _b64_bytes(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
