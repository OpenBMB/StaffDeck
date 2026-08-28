from __future__ import annotations

import importlib
import os
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
        # RapidDoc calls this value pdf_pages_batch. It is a processing window,
        # not a document page limit; all pages are still processed.
        self.max_pages = max(1, max_pages)
        self.max_pixels = max_pixels
        self.device = device
        self.engine = engine

    def extract_pdf(self, filename: str, content: bytes) -> DocumentExtractionResult:
        source_page_count = self._validate_pdf(content)
        readiness = self.model_manager.require_ready()
        saved_env = {
            key: os.environ.get(key)
            for key in (*_OFFLINE_ENV_UPDATES, "RAPID_MODELS_DIR", "MINERU_DEVICE_MODE", "MINERU_PROCESSING_WINDOW_SIZE")
        }
        env_updates = {
            **_OFFLINE_ENV_UPDATES,
            "RAPID_MODELS_DIR": str(readiness.model_dir),
            "MINERU_DEVICE_MODE": self.device,
            "MINERU_PROCESSING_WINDOW_SIZE": str(self.max_pages),
        }
        os.environ.update(env_updates)
        for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            saved_env.setdefault(proxy_key, os.environ.get(proxy_key))
            os.environ[proxy_key] = ""

        try:
            raw = self._call_rapiddoc(content, readiness.model_dir)
            extracted_pages = _normalize_pages(raw, source_page_count)
            if not any(page.text.strip() for page in extracted_pages):
                raise DocumentExtractionError("DOCUMENT_EXTRACTION_FAILED", "RapidDoc returned no OCR text")

            warnings = _extract_warnings(raw)
            return DocumentExtractionResult(
                text=_render_pdf_text(extracted_pages),
                pages=extracted_pages,
                page_refs=[page.ref for page in extracted_pages],
                source_sha256=_sha256_bytes(content),
                source_page_count=source_page_count,
                method="structured",
                engine="rapid-doc-onnxruntime",
                engine_version=_package_version("rapid-doc"),
                warnings=warnings,
                char_count=sum(page.char_count for page in extracted_pages),
                table_count=sum(page.table_count for page in extracted_pages),
            )
        finally:
            for key, old_value in saved_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value

    def _call_rapiddoc(self, content: bytes, model_dir: Path):
        if self.engine not in {"ort", "onnxruntime"}:
            raise DocumentExtractionError(
                "OCR_DEPENDENCY_MISSING",
                f"unsupported RapidDoc engine: {self.engine}; only ONNX Runtime CPU is enabled",
            )
        try:
            importlib.import_module("onnxruntime")
            rapid_doc = importlib.import_module("rapid_doc")
            rapidocr = importlib.import_module("rapidocr")
        except Exception as exc:
            raise DocumentExtractionError("OCR_DEPENDENCY_MISSING", str(exc)) from exc

        rapid_doc_cls = getattr(rapid_doc, "RapidDoc", None)
        engine_type = getattr(rapidocr, "EngineType", None)
        ort_engine = getattr(engine_type, "ONNXRUNTIME", None)
        if not callable(rapid_doc_cls) or ort_engine is None:
            raise DocumentExtractionError(
                "OCR_DEPENDENCY_MISSING",
                "rapid-doc RapidDoc or rapidocr ONNX Runtime engine is unavailable",
            )

        try:
            analyzer = rapid_doc_cls(
                parse_method="auto",
                formula_enable=False,
                table_enable=True,
                lang="ch",
                ocr_config={"engine_type": ort_engine},
                pdf_pages_batch=self.max_pages,
                image_output_mode="url",
            )
            return analyzer(
                content,
                image_output_mode="url",
                f_dump_middle_json=False,
                f_dump_content_list=False,
                f_draw_layout_bbox=False,
                f_draw_span_bbox=False,
            )
        except TimeoutError as exc:
            raise DocumentExtractionError("OCR_TIMEOUT", str(exc) or "ocr timed out") from exc
        except TypeError as exc:
            raise DocumentExtractionError(
                "DOCUMENT_EXTRACTION_FAILED",
                f"incompatible rapid-doc API: {exc}",
            ) from exc
        except DocumentExtractionError:
            raise
        except Exception as exc:
            raise DocumentExtractionError(
                "DOCUMENT_EXTRACTION_FAILED",
                str(exc) or "rapid extraction failed",
            ) from exc

    def _validate_pdf(self, content: bytes) -> int:
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
        return len(reader.pages)


_OFFLINE_ENV_UPDATES = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "STAFFDECK_RAPIDDOC_OFFLINE": "1",
}


def _normalize_pages(raw, page_count: int) -> list[ExtractedPage]:
    grouped: dict[int, list[str]] = {}
    table_counts: dict[int, int] = {}
    content_list = getattr(raw, "content_list_json", None)
    if isinstance(raw, dict):
        content_list = raw.get("content_list_json", content_list)
    if isinstance(content_list, list):
        for item in content_list:
            if not isinstance(item, dict):
                continue
            try:
                page_number = int(item.get("page_idx", 0)) + 1
            except (TypeError, ValueError):
                page_number = 1
            page_number = min(max(page_number, 1), max(page_count, 1))
            text = _content_item_text(item)
            if text:
                grouped.setdefault(page_number, []).append(text)
            if "table" in str(item.get("type", "")).lower():
                table_counts[page_number] = table_counts.get(page_number, 0) + 1

    if not grouped:
        markdown = getattr(raw, "markdown", None)
        if isinstance(raw, dict):
            markdown = raw.get("markdown", markdown)
        if isinstance(markdown, str) and markdown.strip():
            # This is only a compatibility fallback. The official output uses
            # content_list_json, which preserves page-level references.
            grouped[1] = [markdown]

    return [
        ExtractedPage(
            page_number=page_number,
            text="\n\n".join(grouped.get(page_number, [])),
            char_count=len("\n\n".join(grouped.get(page_number, []))),
            table_count=table_counts.get(page_number, 0),
        )
        for page_number in range(1, max(page_count, 1) + 1)
    ]


def _content_item_text(item: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("text", "table_body"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    for key in ("table_caption", "table_footnote", "image_caption", "image_footnote"):
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(entry) for entry in value if str(entry).strip())
    return "\n".join(parts).strip()


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
