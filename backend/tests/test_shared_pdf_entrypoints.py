from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.documents.extraction import (
    DocumentExtractionError,
    DocumentExtractionResult,
    ExtractedPage,
)
from app.core.harness_attachments import materialize_task_attachments
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.harness import (
    HarnessExecutor,
    HarnessLimits,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
)
from app.knowledge.parser import KnowledgeParseError, ParsedKnowledgeDocument
from app.session import attachments
from app.session import attachment_store
from app.session.session_schema import ChatAttachmentRead


def _result(text: str, *, page_count: int = 1, method: str = "native") -> DocumentExtractionResult:
    pages = [
        ExtractedPage(page_number=index + 1, text=f"第 {index + 1} 页 {text}", char_count=len(text) + 5)
        for index in range(page_count)
    ]
    full_text = "\n\n".join(page.text for page in pages)
    source = b"shared-pdf-fixture"
    return DocumentExtractionResult(
        text=full_text,
        pages=pages,
        page_refs=[page.ref for page in pages],
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_page_count=page_count,
        method=method,
        engine="test-engine",
        engine_version="1.2.3",
        warnings=["TEST_WARNING"],
        char_count=len(full_text),
        table_count=0,
    )


def test_chat_pdf_uses_shared_extractor_and_only_returns_bounded_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bytes]] = []
    full_text = "全文哨兵 " + ("审核材料内容。" * 5000)
    result = _result(full_text, page_count=31, method="structured")

    def fake_extract(filename: str, content: bytes) -> ParsedKnowledgeDocument:
        calls.append((filename, content))
        return ParsedKnowledgeDocument(extraction=result, file_type="pdf")

    monkeypatch.setattr(attachments, "extract_document", fake_extract)

    attachment = attachments.parse_chat_attachment(
        "scan.pdf",
        "application/pdf",
        b"pdf-bytes",
    )

    assert calls == [("scan.pdf", b"pdf-bytes")]
    assert attachment.kind == "pdf"
    assert attachment.text is not None
    assert len(attachment.text) <= attachments.MAX_EXTRACTED_TEXT_CHARS + 20
    assert "内容已截断" in attachment.text
    assert attachment.preview == attachment.text[: attachments.MAX_PREVIEW_CHARS].rstrip() + "\n...（内容已截断）"
    assert attachment.page_count == 31
    assert attachment.non_empty_page_count == 31
    assert attachment.extracted_characters == len(result.text)
    assert attachment.extraction_method == "structured"
    assert attachment.extraction_engine == "test-engine"
    assert attachment.extraction_engine_version == "1.2.3"
    assert attachment.extraction_warnings == ["TEST_WARNING"]
    assert "第 31 页" not in attachment.text


def test_chat_pdf_missing_ocr_is_readable_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_extract(_filename: str, _content: bytes) -> ParsedKnowledgeDocument:
        raise KnowledgeParseError("OCR_MODEL_MISSING: 请先准备离线 OCR 模型")

    monkeypatch.setattr(attachments, "extract_document", fail_extract)

    attachment = attachments.parse_chat_attachment(
        "scan.pdf",
        "application/pdf",
        b"pdf-bytes",
    )

    assert attachment.error == "OCR_MODEL_MISSING: 请先准备离线 OCR 模型"
    assert attachment.text is None
    assert attachment.preview == "文件已上传，请通过沙箱路径读取。"
    assert attachment.extraction_status == "failed"


def test_harness_uses_same_shared_result_and_read_file_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_bytes = b"scan-pdf"
    result = _result("首部哨兵 " + ("长文本。" * 10000), page_count=2, method="structured")

    def fake_extract(filename: str, content: bytes) -> ParsedKnowledgeDocument:
        assert filename == "scan.pdf"
        assert content == source_bytes
        return ParsedKnowledgeDocument(extraction=result, file_type="pdf")

    monkeypatch.setattr("app.harness.filesystem.extract_document", fake_extract)
    context = HarnessToolContext(
        run_id="shared-pdf",
        task_frame_id="frame",
        workspace_root=(tmp_path / "workspace").resolve(),
        limits=HarnessLimits(max_read_bytes=128, max_result_bytes=4096),
    )
    context.workspace_root.mkdir(parents=True)
    source = context.workspace_root / "scan.pdf"
    source.write_bytes(source_bytes)
    executor = HarnessExecutor(build_file_tool_registry())

    extracted = executor.execute(
        context,
        HarnessToolCall(
            call_id="extract",
            name="extract_document_text",
            arguments={"path": "/workspace/scan.pdf"},
        ),
    )

    assert extracted.success is True
    assert extracted.data is not None
    assert extracted.data["page_count"] == 2
    assert extracted.data["non_empty_page_count"] == 2
    assert extracted.data["extraction_method"] == "structured"
    assert extracted.data["extraction_engine"] == "test-engine"
    assert extracted.data["warnings"] == ["TEST_WARNING"]
    assert extracted.data["characters"] == len(result.text)

    read = executor.execute(
        context,
        HarnessToolCall(
            call_id="read-1",
            name="read_file",
            arguments={"path": extracted.data["extracted_text_path"], "max_bytes": 128},
        ),
    )
    assert read.success is True
    assert read.data is not None
    assert read.data["truncated"] is True
    assert read.data["continuation_token"]
    assert "首部哨兵" in str(read.data["content"])

    continued = executor.execute(
        context,
        HarnessToolCall(
            call_id="read-2",
            name="read_file",
            arguments={
                "path": extracted.data["extracted_text_path"],
                "max_bytes": 128,
                "continuation_token": read.data["continuation_token"],
            },
        ),
    )
    assert continued.success is True
    assert continued.data is not None
    assert continued.data["requested_offset"] == read.data["next_offset"]


def test_harness_maps_shared_ocr_failure_to_readable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_extract(_filename: str, _content: bytes) -> ParsedKnowledgeDocument:
        raise DocumentExtractionError("OCR_MODEL_MISSING", "离线 OCR 模型不存在")

    monkeypatch.setattr("app.harness.filesystem.extract_document", fail_extract)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "scan.pdf").write_bytes(b"pdf")
    context = HarnessToolContext(run_id="ocr-failure", workspace_root=workspace)
    result = HarnessExecutor(build_file_tool_registry()).execute(
        context,
        HarnessToolCall(
            call_id="extract",
            name="extract_document_text",
            arguments={"path": "scan.pdf"},
        ),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "DOCUMENT_EXTRACTION_FAILED"
    assert "OCR_MODEL_MISSING" in result.error.message


def test_attachment_metadata_round_trip_preserves_extraction_state() -> None:
    attachment = ChatAttachmentRead(
        id="pdf-1",
        filename="scan.pdf",
        content_type="application/pdf",
        size=10,
        kind="pdf",
        extraction_status="succeeded",
        extraction_method="structured",
        extraction_engine="rapiddoc",
        extraction_engine_version="0.1",
        page_count=31,
        non_empty_page_count=30,
        extracted_characters=50000,
        extracted_text_sha256="a" * 64,
        extraction_warnings=["OCR_USED"],
    )

    normalized = attachments.validate_chat_turn_attachments(
        [attachment],
        max_attachments=1,
        max_attachment_bytes=100,
    )[0]

    assert normalized.extraction_status == "succeeded"
    assert normalized.page_count == 31
    assert normalized.extracted_characters == 50000
    assert normalized.extraction_warnings == ["OCR_USED"]


def test_staging_preserves_original_bytes_and_complete_derived_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = b"original-pdf-bytes"
    complete_text = "完整派生文本\n" + ("最后页哨兵。" * 4000)
    result = _result(complete_text, page_count=31, method="structured")

    monkeypatch.setattr(
        attachments,
        "extract_document",
        lambda _filename, _content: ParsedKnowledgeDocument(extraction=result, file_type="pdf"),
    )
    monkeypatch.setattr(attachment_store.paths, "user_data_dir", lambda: tmp_path / "data")

    parsed = attachments.parse_chat_attachment("scan.pdf", "application/pdf", source)
    staged = attachment_store.stage_chat_attachment(
        parsed,
        source,
        tenant_id="tenant-1",
        user_id="user-1",
    )

    assert attachment_store.read_staged_chat_attachment(
        staged,
        tenant_id="tenant-1",
        user_id="user-1",
    ) == source
    assert attachment_store.read_staged_chat_attachment_text(
        staged,
        tenant_id="tenant-1",
        user_id="user-1",
    ) == result.text


def test_materialized_pdf_uses_complete_staged_text_not_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = b"original-pdf-bytes"
    result = _result("首部哨兵 " + ("完整报告内容。" * 5000), page_count=31, method="structured")
    monkeypatch.setattr(
        attachments,
        "extract_document",
        lambda _filename, _content: ParsedKnowledgeDocument(extraction=result, file_type="pdf"),
    )
    monkeypatch.setattr(attachment_store.paths, "user_data_dir", lambda: tmp_path / "data")

    parsed = attachments.parse_chat_attachment("scan.pdf", "application/pdf", source)
    staged = attachment_store.stage_chat_attachment(
        parsed,
        source,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    descriptors = materialize_task_attachments(
        [staged],
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        task_frame_id="task-1",
    )
    workspace = harness_task_workspace_path(
        tenant_id="tenant-1",
        session_id="session-1",
        task_frame_id="task-1",
    )

    extracted_path = workspace / str(descriptors[0]["extracted_text_path"]).removeprefix("/workspace/")
    assert extracted_path.read_text(encoding="utf-8") == result.text
    assert len(extracted_path.read_text(encoding="utf-8")) > attachments.MAX_EXTRACTED_TEXT_CHARS
