from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.documents.extraction import DocumentExtractionError, DocumentExtractor


SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".doc"}


class KnowledgeParseError(ValueError):
    pass


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    try:
        result = DocumentExtractor().extract(filename, content)
    except DocumentExtractionError as exc:
        if suffix == ".pdf" and exc.code == "OCR_DEPENDENCY_MISSING":
            return _extract_pdf_legacy(content), "pdf"
        raise KnowledgeParseError(str(exc)) from exc
    return result.text, suffix.lstrip(".") if suffix != ".htm" else "html"


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
