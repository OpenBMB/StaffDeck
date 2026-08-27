from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.config import Settings, get_settings
from app.documents.extraction import DocumentExtractionError, DocumentExtractor
from app.documents.model_manager import RapidDocModelManager
from app.documents.rapiddoc_adapter import RapidDocStructuredPdfAdapter

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".doc"}


class KnowledgeParseError(ValueError):
    pass


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    try:
        result = _build_document_extractor().extract(filename, content)
    except DocumentExtractionError as exc:
        if suffix == ".pdf" and exc.code == "OCR_DEPENDENCY_MISSING":
            return _extract_pdf_legacy(content), "pdf"
        raise KnowledgeParseError(str(exc)) from exc
    return result.text, suffix.lstrip(".") if suffix != ".htm" else "html"


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
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency availability differs by env.
        raise KnowledgeParseError("缺少 pypdf，无法解析 PDF。") from exc
    reader = PdfReader(BytesIO(content))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"## 第 {index + 1} 页\n\n{page_text}")
    if not pages:
        return ""
    return "# PDF 文档\n\n" + "\n\n".join(pages)
