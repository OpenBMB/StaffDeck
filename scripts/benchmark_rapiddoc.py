from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "engine",
    "package_bytes",
    "model_bytes",
    "peak_rss_bytes",
    "elapsed_seconds",
    "page_count",
    "non_empty_pages",
    "text_chars",
    "table_count",
    "offline_replay",
    "warnings",
)

ROOT_DIR = Path(__file__).resolve().parents[1]
RAPIDDOC_PAGE_BATCH = 64


class ProbeExecutionError(RuntimeError):
    pass


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_pdf_path(pdf_path: str | Path, repo_root: Path = ROOT_DIR) -> Path:
    candidate = Path(pdf_path).expanduser().resolve(strict=False)
    if _is_relative_to(candidate, repo_root.resolve()):
        raise ValueError("PDF input must stay outside the repository")
    if not candidate.is_file():
        raise FileNotFoundError(f"PDF input does not exist: {candidate}")
    return candidate


def resolve_model_dir(model_dir: str | Path) -> Path:
    candidate = Path(model_dir).expanduser().resolve(strict=False)
    if not candidate.is_dir():
        raise ProbeExecutionError(f"OCR_MODEL_MISSING: model directory does not exist: {candidate}")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"Expected JSON object in {path}")
    return raw


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def probe_python_path(probe_dir: Path, platform: str | None = None) -> Path:
    target_platform = sys.platform if platform is None else platform
    if target_platform.startswith("win"):
        return probe_dir / "Scripts" / "python.exe"
    return probe_dir / "bin" / "python"


def build_probe_create_command(probe_dir: Path, platform: str | None = None) -> list[str]:
    target_platform = sys.platform if platform is None else platform
    if target_platform.startswith("win"):
        return ["py", "-3.11", "-m", "venv", str(probe_dir)]
    return ["python3.11", "-m", "venv", str(probe_dir)]


def ensure_probe_environment(
    probe_dir: Path,
    *,
    platform: str | None = None,
    run_command: Any | None = None,
) -> Path:
    python_path = probe_python_path(probe_dir, platform=platform)
    if python_path.is_file():
        return python_path

    runner = _run_plain_command if run_command is None else run_command
    runner(build_probe_create_command(probe_dir, platform=platform))

    if not python_path.is_file():
        raise ProbeExecutionError(
            f"PROBE_ENV_CREATE_FAILED: probe environment python was not created at {python_path}"
        )
    return python_path


def _run_plain_command(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise ProbeExecutionError(f"PROBE_ENV_CREATE_FAILED: {details or 'venv creation failed'}")


def _probe_site_packages_bytes(probe_python: Path) -> int:
    probe_dir = probe_python.parent.parent
    if probe_python.parent.name == "Scripts":
        return _dir_size_bytes(probe_dir / "Lib" / "site-packages")
    lib_dir = probe_dir / "lib"
    for candidate in lib_dir.glob("python*/site-packages"):
        return _dir_size_bytes(candidate)
    return 0


def _peak_rss_bytes() -> int:
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError):
            pass
        return 0

    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(usage)
    return int(usage * 1024)


def _extract_warnings(raw: Any) -> list[str]:
    warnings = raw.get("warnings") if isinstance(raw, dict) else getattr(raw, "warnings", [])
    if warnings is None:
        return []
    return [str(item) for item in warnings if str(item).strip()]


def _extract_pages(raw: Any, page_count: int | None = None) -> list[Any]:
    content_list = raw.get("content_list_json") if isinstance(raw, dict) else getattr(raw, "content_list_json", None)
    if isinstance(content_list, list):
        grouped: dict[int, dict[str, Any]] = {}
        for item in content_list:
            if not isinstance(item, dict):
                continue
            try:
                page_idx = max(0, int(item.get("page_idx", 0)))
            except (TypeError, ValueError):
                page_idx = 0
            page = grouped.setdefault(page_idx, {"text_parts": [], "tables": []})
            text = _content_item_text(item)
            if text:
                page["text_parts"].append(text)
            if "table" in str(item.get("type", "")).lower():
                page["tables"].append(item)

        inferred_page_count = max(grouped, default=-1) + 1
        total_pages = max(page_count or 0, inferred_page_count)
        return [
            {
                "text": "\n\n".join(grouped.get(index, {}).get("text_parts", [])),
                "tables": grouped.get(index, {}).get("tables", []),
            }
            for index in range(total_pages)
        ]

    if isinstance(raw, dict) and isinstance(raw.get("pages"), list):
        return raw["pages"]
    pages = getattr(raw, "pages", None)
    if isinstance(pages, list):
        return pages
    if isinstance(raw, list):
        return raw
    raise ProbeExecutionError("DOCUMENT_EXTRACTION_FAILED: RapidDoc did not return a pages collection")


def _content_item_text(item: dict[str, Any]) -> str:
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


def _page_text(page: Any) -> str:
    if isinstance(page, dict):
        text = page.get("text")
        if isinstance(text, str) and text.strip():
            return text
        markdown = page.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        content = page.get("content")
        if isinstance(content, str):
            return content
        return ""

    for attr in ("text", "markdown", "content"):
        value = getattr(page, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _page_table_count(page: Any) -> int:
    tables = page.get("tables") if isinstance(page, dict) else getattr(page, "tables", [])
    if isinstance(tables, list):
        return len(tables)
    return 0


def _call_rapiddoc(pdf_path: Path, model_dir: Path) -> Any:
    try:
        importlib.import_module("onnxruntime")
        rapid_doc = importlib.import_module("rapid_doc")
        rapidocr = importlib.import_module("rapidocr")
    except Exception as exc:
        raise ProbeExecutionError(f"OCR_DEPENDENCY_MISSING: {exc}") from exc

    rapid_doc_cls = getattr(rapid_doc, "RapidDoc", None)
    engine_type = getattr(rapidocr, "EngineType", None)
    ort_engine = getattr(engine_type, "ONNXRUNTIME", None)
    if not callable(rapid_doc_cls) or ort_engine is None:
        raise ProbeExecutionError(
            "OCR_DEPENDENCY_MISSING: rapid_doc.RapidDoc or rapidocr ONNX Runtime engine is unavailable"
        )

    os.environ["RAPID_MODELS_DIR"] = str(model_dir)

    try:
        analyzer = rapid_doc_cls(
            parse_method="auto",
            formula_enable=False,
            table_enable=True,
            lang="ch",
            ocr_config={"engine_type": ort_engine},
            pdf_pages_batch=RAPIDDOC_PAGE_BATCH,
            image_output_mode="url",
        )
        return analyzer(
            pdf_path.read_bytes(),
            image_output_mode="url",
            f_dump_middle_json=False,
            f_dump_content_list=False,
            f_draw_layout_bbox=False,
            f_draw_span_bbox=False,
        )
    except TimeoutError as exc:
        raise ProbeExecutionError(f"OCR_TIMEOUT: {exc}") from exc
    except TypeError as exc:
        raise ProbeExecutionError(f"DOCUMENT_EXTRACTION_FAILED: incompatible RapidDoc API: {exc}") from exc
    except ProbeExecutionError:
        raise
    except Exception as exc:
        raise ProbeExecutionError(f"DOCUMENT_EXTRACTION_FAILED: {exc}") from exc


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        if getattr(reader, "is_encrypted", False):
            raise ProbeExecutionError("PDF_ENCRYPTED: pdf is encrypted")
        return len(reader.pages)
    except ProbeExecutionError:
        raise
    except Exception as exc:
        raise ProbeExecutionError(f"PDF_CORRUPTED: {exc}") from exc


def execute_probe(*, pdf_path: Path, model_dir: Path, offline: bool) -> dict[str, Any]:
    if not model_dir.is_dir():
        raise ProbeExecutionError(f"OCR_MODEL_MISSING: model directory does not exist: {model_dir}")

    offline_updates = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "STAFFDECK_RAPIDDOC_OFFLINE": "1",
    }
    runtime_updates = {
        "RAPID_MODELS_DIR": str(model_dir),
        "MINERU_DEVICE_MODE": "cpu",
        "MINERU_PROCESSING_WINDOW_SIZE": str(RAPIDDOC_PAGE_BATCH),
    }
    saved_env = {key: os.environ.get(key) for key in (*offline_updates, *runtime_updates)}
    if offline:
        runtime_updates.update({"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": ""})
        saved_env.update({key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")})

    os.environ.update(runtime_updates)
    if offline:
        os.environ.update(offline_updates)

    try:
        started = time.perf_counter()
        raw = _call_rapiddoc(pdf_path, model_dir)
        elapsed_seconds = time.perf_counter() - started
        pages = _extract_pages(raw, _pdf_page_count(pdf_path))
        page_texts = [_page_text(page) for page in pages]
        page_sha256 = [_sha256_text(text) for text in page_texts]
        non_empty_pages = sum(1 for text in page_texts if text.strip())
        table_count = sum(_page_table_count(page) for page in pages)
        warnings = _extract_warnings(raw)

        return {
            "engine": "rapid-doc-onnxruntime",
            "peak_rss_bytes": _peak_rss_bytes(),
            "elapsed_seconds": elapsed_seconds,
            "page_count": len(pages),
            "non_empty_pages": non_empty_pages,
            "text_chars": sum(len(text) for text in page_texts),
            "table_count": table_count,
            "warnings": warnings,
            "page_sha256": page_sha256,
        }
    finally:
        for key, old_value in saved_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def run_probe_subprocess(
    *,
    probe_python: Path,
    pdf_path: Path,
    model_dir: Path,
    offline: bool,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False, encoding="utf-8") as handle:
        probe_output_path = Path(handle.name)

    command = [
        str(probe_python),
        str(Path(__file__).resolve()),
        "--internal-probe",
        "--pdf",
        str(pdf_path),
        "--model-dir",
        str(model_dir),
        "--probe-json-output",
        str(probe_output_path),
    ]
    if offline:
        command.append("--offline")

    env = os.environ.copy()
    if offline:
        env.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "STAFFDECK_RAPIDDOC_OFFLINE": "1",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
            }
        )

    try:
        completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            raise ProbeExecutionError(details or "DOCUMENT_EXTRACTION_FAILED: probe subprocess failed")
        return _read_json(probe_output_path)
    finally:
        probe_output_path.unlink(missing_ok=True)


def compare_offline_replay(
    *,
    source_sha256: str,
    primary_result: dict[str, Any],
    replay_result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    mismatch_fields: list[str] = []
    for field in ("page_count", "page_sha256", "text_chars", "table_count"):
        if primary_result.get(field) != replay_result.get(field):
            mismatch_fields.append(field)

    replay_source_sha256 = replay_result.get("source_sha256")
    if replay_source_sha256 is not None and replay_source_sha256 != source_sha256:
        mismatch_fields.append("source_sha256")

    warnings: list[str] = []
    if mismatch_fields:
        stable = ",".join(mismatch_fields)
        warnings.append(f"OFFLINE_REPLAY_MISMATCH: {stable}")

    return (
        {
            "matched": not mismatch_fields,
            "source_sha256": source_sha256,
            "page_sha256": replay_result.get("page_sha256", []),
            "char_count": replay_result.get("text_chars", 0),
            "table_count": replay_result.get("table_count", 0),
            "mismatch_fields": mismatch_fields,
        },
        warnings,
    )


def _dedupe_warnings(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _require_non_negative_int(report: dict[str, Any], field: str) -> None:
    value = report.get(field)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _require_non_negative_number(report: dict[str, Any], field: str) -> None:
    value = report.get(field)
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")


def validate_benchmark_report(report: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in report]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")

    engine = report["engine"]
    if not isinstance(engine, str) or not engine.strip():
        raise ValueError("engine must be a non-empty string")

    for field in (
        "package_bytes",
        "model_bytes",
        "peak_rss_bytes",
        "page_count",
        "non_empty_pages",
        "text_chars",
        "table_count",
    ):
        _require_non_negative_int(report, field)

    _require_non_negative_number(report, "elapsed_seconds")

    page_count = report["page_count"]
    non_empty_pages = report["non_empty_pages"]
    if non_empty_pages > page_count:
        raise ValueError("non_empty_pages cannot exceed page_count")

    warnings = report["warnings"]
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("warnings must be a list of strings")

    offline_replay = report["offline_replay"]
    if not isinstance(offline_replay, dict):
        raise TypeError("offline_replay must be an object")

    matched = offline_replay.get("matched")
    if not isinstance(matched, bool):
        raise TypeError("offline_replay.matched must be a boolean")

    source_sha256 = offline_replay.get("source_sha256")
    if not isinstance(source_sha256, str) or not source_sha256.strip():
        raise ValueError("offline_replay.source_sha256 must be a non-empty string")

    page_sha256 = offline_replay.get("page_sha256")
    if not isinstance(page_sha256, list) or not all(
        isinstance(item, str) and item.strip() for item in page_sha256
    ):
        raise ValueError("offline_replay.page_sha256 must be a list of strings")
    if len(page_sha256) != page_count:
        raise ValueError("offline_replay.page_sha256 count must match page_count")

    char_count = offline_replay.get("char_count")
    if not isinstance(char_count, int) or char_count < 0:
        raise ValueError("offline_replay.char_count must be a non-negative integer")

    table_count = offline_replay.get("table_count")
    if not isinstance(table_count, int) or table_count < 0:
        raise ValueError("offline_replay.table_count must be a non-negative integer")

    mismatch_fields = offline_replay.get("mismatch_fields")
    if not isinstance(mismatch_fields, list) or not all(isinstance(item, str) for item in mismatch_fields):
        raise ValueError("offline_replay.mismatch_fields must be a list of strings")

    return report


def build_benchmark_report(
    *,
    pdf_path: Path,
    model_dir: Path,
    package_bytes: int,
    primary_result: dict[str, Any],
    replay_result: dict[str, Any],
) -> dict[str, Any]:
    source_sha256 = sha256_file(pdf_path)
    offline_replay, replay_warnings = compare_offline_replay(
        source_sha256=source_sha256,
        primary_result=primary_result,
        replay_result=replay_result,
    )

    report = {
        "engine": primary_result.get("engine"),
        "package_bytes": package_bytes,
        "model_bytes": _dir_size_bytes(model_dir),
        "peak_rss_bytes": primary_result.get("peak_rss_bytes"),
        "elapsed_seconds": primary_result.get("elapsed_seconds"),
        "page_count": primary_result.get("page_count"),
        "non_empty_pages": primary_result.get("non_empty_pages"),
        "text_chars": primary_result.get("text_chars"),
        "table_count": primary_result.get("table_count"),
        "offline_replay": offline_replay,
        "warnings": _dedupe_warnings(
            [*primary_result.get("warnings", []), *replay_result.get("warnings", []), *replay_warnings]
        ),
    }
    return validate_benchmark_report(report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an isolated RapidDoc benchmark probe")
    parser.add_argument("--pdf", required=True, help="Path to a local PDF outside the repository")
    parser.add_argument("--output", help="Path to write the benchmark JSON report")
    parser.add_argument("--model-dir", required=True, help="Model directory measured for the probe")
    parser.add_argument(
        "--offline-replay",
        help="Path where the fresh offline replay JSON result should be written",
    )
    parser.add_argument("--internal-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-json-output", help=argparse.SUPPRESS)
    parser.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pdf_path = resolve_pdf_path(args.pdf)
    model_dir = resolve_model_dir(args.model_dir)

    if args.internal_probe:
        if not args.probe_json_output:
            raise ProbeExecutionError("DOCUMENT_EXTRACTION_FAILED: missing --probe-json-output")
        probe_result = execute_probe(pdf_path=pdf_path, model_dir=model_dir, offline=args.offline)
        probe_result["source_sha256"] = sha256_file(pdf_path)
        _write_json(Path(args.probe_json_output).expanduser().resolve(strict=False), probe_result)
        return 0

    if not args.output:
        raise ProbeExecutionError("DOCUMENT_EXTRACTION_FAILED: missing --output")
    if not args.offline_replay:
        raise ProbeExecutionError("DOCUMENT_EXTRACTION_FAILED: missing --offline-replay")

    output_path = Path(args.output).expanduser().resolve(strict=False)
    replay_path = Path(args.offline_replay).expanduser().resolve(strict=False)

    probe_dir = ROOT_DIR / ".rapiddoc-probe"
    probe_python = ensure_probe_environment(probe_dir)
    package_bytes = _probe_site_packages_bytes(probe_python)

    primary_result = run_probe_subprocess(
        probe_python=probe_python,
        pdf_path=pdf_path,
        model_dir=model_dir,
        offline=False,
    )
    replay_result = run_probe_subprocess(
        probe_python=probe_python,
        pdf_path=pdf_path,
        model_dir=model_dir,
        offline=True,
    )

    _write_json(replay_path, replay_result)
    report = build_benchmark_report(
        pdf_path=pdf_path,
        model_dir=model_dir,
        package_bytes=package_bytes,
        primary_result=primary_result,
        replay_result=replay_result,
    )

    _write_json(output_path, report)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
