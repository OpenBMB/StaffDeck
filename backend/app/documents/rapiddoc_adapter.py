from __future__ import annotations

import importlib
import os
import tempfile
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path

from .extraction import DocumentExtractionError, DocumentExtractionResult, ExtractedPage
from .model_manager import RapidDocModelManager


class RapidDocStructuredPdfAdapter:
    def __init__(
        self,
        *,
        model_manager: RapidDocModelManager | None = None,
        timeout_seconds: float = 180.0,
        worker_count: int = 1,
        max_pages: int = 64,
        max_pixels: int = 20_000_000,
        device: str = "cpu",
        engine: str = "ort",
    ) -> None:
        self.model_manager = model_manager or RapidDocModelManager()
        self.timeout_seconds = timeout_seconds
        self.worker_count = worker_count
        self.max_pages = max_pages
        self.max_pixels = max_pixels
        self.device = device
        self.engine = engine

    def extract_pdf(self, filename: str, content: bytes) -> DocumentExtractionResult:
        self._validate_pdf(content)
        readiness = self.model_manager.require_ready()
        saved_env = {key: os.environ.get(key) for key in _OFFLINE_ENV_UPDATES}
        os.environ.update(_OFFLINE_ENV_UPDATES)
        for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            saved_env.setdefault(proxy_key, os.environ.get(proxy_key))
            os.environ[proxy_key] = ""

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(content)
                temp_path = Path(handle.name)

            raw = self._call_rapiddoc(temp_path, readiness.model_dir)
            pages = _extract_pages(raw)
            extracted_pages = _normalize_pages(pages)
            if not any(page.text.strip() for page in extracted_pages):
                raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", "RapidDoc returned no OCR text")

            warnings = _extract_warnings(raw)
            return DocumentExtractionResult(
                text=_render_pdf_text(extracted_pages),
                pages=extracted_pages,
                page_refs=[page.ref for page in extracted_pages],
                source_sha256=_sha256_bytes(content),
                source_page_count=len(extracted_pages),
                method="structured",
                engine="rapiddoc-ort",
                engine_version=_package_version("rapiddoc"),
                warnings=warnings,
                char_count=sum(page.char_count for page in extracted_pages),
                table_count=sum(page.table_count for page in extracted_pages),
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            for key, old_value in saved_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value

    def _call_rapiddoc(self, pdf_path: Path, model_dir: Path):
        try:
            importlib.import_module("onnxruntime")
            rapiddoc = importlib.import_module("rapiddoc")
        except ImportError as exc:
            raise DocumentExtractionError("OCR_DEPENDENCY_MISSING", str(exc)) from exc

        analyze = getattr(rapiddoc, "doc_analyze", None)
        if not callable(analyze):
            raise DocumentExtractionError("OCR_DEPENDENCY_MISSING", "rapiddoc.doc_analyze is unavailable")

        try:
            return analyze(
                str(pdf_path),
                mode="auto",
                device=self.device,
                engine=self.engine,
                model_dir=str(model_dir),
                table_enable=True,
                reading_order=True,
                formula_enable=False,
                table_formula_enable=False,
                extract_images=False,
                checkbox_enable=False,
                timeout=self.timeout_seconds,
                workers=self.worker_count,
                max_pages=self.max_pages,
                max_pixels=self.max_pixels,
            )
        except TimeoutError as exc:
            raise DocumentExtractionError("OCR_TIMEOUT", str(exc) or "ocr timed out") from exc
        except TypeError as exc:
            raise DocumentExtractionError(
                "DOCUMENT_EXTRACTION_FAILED",
                f"incompatible RapidDoc API: {exc}",
            ) from exc
        except DocumentExtractionError:
            raise
        except Exception as exc:
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", str(exc) or "rapid extraction failed") from exc

    def _validate_pdf(self, content: bytes) -> None:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except Exception as exc:  # pragma: no cover - environment dependent
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", "missing pypdf") from exc

        try:
            reader = PdfReader(BytesIO(content))
        except PdfReadError as exc:
            raise DocumentExtractionError("PDF_CORRUPTED", str(exc) or "pdf is corrupted") from exc
        except Exception as exc:
            raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", str(exc) or "failed to inspect pdf") from exc

        if getattr(reader, "is_encrypted", False):
            raise DocumentExtractionError("PDF_ENCRYPTED", "pdf is encrypted")


_OFFLINE_ENV_UPDATES = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "STAFFDECK_RAPIDDOC_OFFLINE": "1",
}


def _extract_pages(raw) -> list[object]:
    if isinstance(raw, dict) and isinstance(raw.get("pages"), list):
        return raw["pages"]
    pages = getattr(raw, "pages", None)
    if isinstance(pages, list):
        return pages
    if isinstance(raw, list):
        return raw
    raise DocumentExtractionError(
        "DOCUMENT_EXTRACTION_FAILED",
        "RapidDoc did not return a pages collection",
    )


def _normalize_pages(raw_pages: list[object]) -> list[ExtractedPage]:
    pages: list[ExtractedPage] = []
    for index, page in enumerate(raw_pages, start=1):
        text = _page_text(page)
        pages.append(
            ExtractedPage(
                page_number=index,
                text=text,
                char_count=len(text),
                table_count=_page_table_count(page),
            )
        )
    return pages


def _page_text(page: object) -> str:
    if isinstance(page, dict):
        for key in ("text", "markdown", "content"):
            value = page.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    for attr in ("text", "markdown", "content"):
        value = getattr(page, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _page_table_count(page: object) -> int:
    tables = page.get("tables") if isinstance(page, dict) else getattr(page, "tables", [])
    return len(tables) if isinstance(tables, list) else 0


def _extract_warnings(raw) -> list[str]:
    warnings = raw.get("warnings") if isinstance(raw, dict) else getattr(raw, "warnings", [])
    if not isinstance(warnings, list):
        return []
    return [str(item) for item in warnings if str(item).strip()]


def _render_pdf_text(pages: list[ExtractedPage]) -> str:
    body = "\n\n".join(f"## 第 {page.page_number} 页\n\n{page.text}" for page in pages if page.text.strip())
    return "# PDF 文档\n\n" + body if body else ""


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def _sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
