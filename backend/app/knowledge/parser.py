from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from app.config import Settings, get_settings
from app.documents.extraction import (
    DocumentExtractionError,
    DocumentExtractionResult,
    DocumentExtractor,
    ExtractedPage,
)
from app.documents.model_manager import RapidDocModelManager
from app.documents.rapiddoc_adapter import RapidDocStructuredPdfAdapter

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".doc"}


class KnowledgeParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedKnowledgeDocument:
    extraction: DocumentExtractionResult
    file_type: str

    @property
    def text(self) -> str:
        return self.extraction.text


def extract_document(filename: str, content: bytes) -> ParsedKnowledgeDocument:
    suffix = Path(filename).suffix.lower()
    file_type = suffix.lstrip(".") if suffix != ".htm" else "html"
    try:
        result = _build_document_extractor().extract(filename, content)
    except DocumentExtractionError as exc:
        if suffix == ".pdf" and exc.code == "OCR_DEPENDENCY_MISSING":
            result = _extract_pdf_legacy_result(content)
        else:
            raise KnowledgeParseError(str(exc)) from exc
    return ParsedKnowledgeDocument(extraction=result, file_type=file_type)


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    result = extract_document(filename, content)
    return result.text, result.file_type


def _build_document_extractor(settings: Settings | None = None) -> DocumentExtractor:
    resolved_settings = settings or get_settings()
    if not resolved_settings.structured_pdf_enabled:
        return DocumentExtractor()

    if resolved_settings.structured_pdf_engine != "rapiddoc":
        raise DocumentExtractionError(
            "OCR_DEPENDENCY_MISSING",
            f"unsupported structured pdf engine: {resolved_settings.structured_pdf_engine}",
        )

    adapter = RapidDocStructuredPdfAdapter(
        model_manager=RapidDocModelManager(model_dir=resolved_settings.rapid_models_dir or None),
        timeout_seconds=resolved_settings.structured_pdf_timeout_seconds,
        worker_count=resolved_settings.structured_pdf_worker_count,
        max_pages=resolved_settings.structured_pdf_max_pages,
        max_pixels=resolved_settings.structured_pdf_max_pixels,
    )
    return DocumentExtractor(
        structured_pdf_enabled=True,
        structured_pdf_adapter=adapter,
    )


def _extract_pdf_legacy(content: bytes) -> str:
    return _extract_pdf_legacy_result(content).text


def _extract_pdf_legacy_result(content: bytes) -> DocumentExtractionResult:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency availability differs by env.
        raise KnowledgeParseError("缺少 pypdf，无法解析 PDF。") from exc
    reader = PdfReader(BytesIO(content))
    pages: list[ExtractedPage] = []
    for index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(
                ExtractedPage(
                    page_number=index + 1,
                    text=page_text,
                    char_count=len(page_text),
                )
            )
    text = ""
    if not pages:
        text = ""
    else:
        text = "# PDF 文档\n\n" + "\n\n".join(
            f"## 第 {page.page_number} 页\n\n{page.text}" for page in pages
        )
    return DocumentExtractionResult(
        text=text,
        pages=pages,
        page_refs=[page.ref for page in pages],
        source_sha256=hashlib.sha256(content).hexdigest(),
        source_page_count=len(reader.pages),
        method="native",
        engine="pypdf",
        engine_version="builtin",
        warnings=[],
        char_count=sum(page.char_count for page in pages),
        table_count=0,
    )
