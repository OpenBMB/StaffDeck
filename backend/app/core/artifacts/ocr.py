"""Indra OCR 服务客户端（Agent 广场 ``ocr`` 能力）。

上游契约见仓库根目录 ``indra.md``：``POST {base}/v1/ocr``，multipart 上传若干
``files``，返回 ``{"model": "indra-ocr", "results": [...]}``，每个 result 再按页给出
``markdown`` / ``text`` / ``lines``（带坐标）/ ``blocks``（版面块）/ ``timings``。

为什么必须走服务端代理（而不是浏览器直连）：

1. 上游要求 ``X-API-Key``，放进前端产物等于公开密钥；
2. OCR 服务在内网，浏览器直连会被 CORS 与网络挡住（与 Laya 决策服务同一类问题）；
3. 文件数量与体积要先在服务端挡一道，否则一次拖进来 30 个 50MB 扫描件会把上游打满。

本模块只负责「发请求 + 归一化回包」，HTTP 语义翻译留给 ``app/api/agent_apps.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from app.config import get_settings

#: 上游识别入口（indra.md 的 multipart 形态；base64 形态当前不用）。
OCR_RUN_PATH = "/v1/ocr"

#: 允许的输出格式。``json`` 带行坐标与版面块，``md`` / ``text`` 是纯文本形态。
ALLOWED_FORMATS = ("json", "md", "text")

#: 单页最多保留多少行 / 版面块。极端扫描件一页能出上千行，全量回包会把浏览器打顿；
#: 这里只截断展示用的结构，``markdown`` / ``text`` 仍然是完整的。
MAX_LINES_PER_PAGE = 2000

_PAGE_SOURCES = {"ocr", "text", "hybrid", "error"}


class OcrError(RuntimeError):
    """调用 OCR 服务的统一异常。

    ``kind`` ∈ ``config`` / ``timeout`` / ``http`` / ``network`` / ``decode`` / ``payload``，
    供调用方翻译成合适的状态码与可读文案。
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "network",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class OcrOptions:
    """与 indra.md 的请求字段一一对应；``None`` 表示不传，由上游用默认值。"""

    format: str = "json"
    mode: str = ""
    dpi: int | None = None
    max_pages: int | None = None
    password: str = ""
    math: bool | None = None
    furniture: bool | None = None
    image_ocr: bool | None = None
    include_lines: bool | None = None
    include_blocks: bool | None = None
    include_timings: bool | None = None


def _as_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ocr_response(body: Any, *, filenames: Sequence[str] = ()) -> dict[str, Any]:
    """把上游回包收敛成前端能直接消费的结构。

    上游字段是「宽松 JSON」：``lines`` / ``blocks`` 可能缺失，``elapsed_ms`` 可能是
    ``null``，单文件失败时 ``error`` 有值但 ``pages`` 仍可能带着已识别成功的页。
    这里逐层补默认值，前端不需要写防御代码。
    """

    if not isinstance(body, dict):
        raise OcrError("OCR 服务返回结构异常（期望 JSON 对象）", kind="decode")

    raw_results = body.get("results")
    if raw_results is None:
        # 单文件时上游也可能直接返回一个 result；两种形态都接受
        raw_results = [body] if body.get("pages") is not None else []
    if not isinstance(raw_results, list):
        raise OcrError("OCR 服务返回结构异常（results 不是数组）", kind="decode")

    results: list[dict[str, Any]] = []
    total_pages = 0
    error_count = 0
    total_elapsed = 0.0
    for index, item in enumerate(raw_results):
        if not isinstance(item, dict):
            continue
        pages: list[dict[str, Any]] = []
        for position, page in enumerate(item.get("pages") or []):
            if not isinstance(page, dict):
                continue
            source = str(page.get("source") or "ocr").strip().lower()
            timings = page.get("timings")
            lines = page.get("lines")
            blocks = page.get("blocks")
            # 上游在纯文本形态（format=md/text）可能不回首页码，这时按数组顺序兜底，
            # 否则整份文档每页都会显示「第 1 页」。
            raw_index = _as_int(page.get("index"))
            pages.append(
                {
                    "index": raw_index if raw_index is not None else position,
                    "source": source if source in _PAGE_SOURCES else "ocr",
                    "note": str(page.get("note") or ""),
                    "rotation": _as_int(page.get("rotation")) or 0,
                    "skew": _as_float(page.get("skew")) or 0.0,
                    "vertical": bool(page.get("vertical")),
                    "n_lines": _as_int(page.get("n_lines")) or 0,
                    "markdown": str(page.get("markdown") or ""),
                    "text": str(page.get("text") or ""),
                    "lines": [row for row in (lines if isinstance(lines, list) else []) if isinstance(row, dict)][
                        :MAX_LINES_PER_PAGE
                    ],
                    "blocks": [row for row in (blocks if isinstance(blocks, list) else []) if isinstance(row, dict)][
                        :MAX_LINES_PER_PAGE
                    ],
                    "timings": timings if isinstance(timings, dict) else {},
                }
            )

        error = item.get("error")
        elapsed = _as_float(item.get("elapsed_ms"))
        if error:
            error_count += 1
        if elapsed:
            total_elapsed += elapsed
        total_pages += len(pages)
        fallback_name = filenames[index] if index < len(filenames) else f"文件 {index + 1}"
        results.append(
            {
                "filename": str(item.get("filename") or fallback_name),
                "n_pages": _as_int(item.get("n_pages")) or len(pages),
                "elapsed_ms": elapsed,
                "error": str(error) if error else None,
                "pages": pages,
            }
        )

    return {
        "model": str(body.get("model") or "indra-ocr"),
        "results": results,
        "summary": {
            "file_count": len(results),
            "page_count": total_pages,
            "elapsed_ms": round(total_elapsed, 1) if total_elapsed else None,
            "error_count": error_count,
        },
    }


def _describe_http_error(exc: httpx.HTTPStatusError) -> str:
    """把上游 4xx/5xx 压成一句可读错误（正文可能是 HTML，截断即可）。"""

    detail = ""
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or body.get("error") or "")
        elif isinstance(body, str):
            detail = body[:300]
    except ValueError:
        detail = (exc.response.text or "")[:300]
    suffix = f"：{detail}" if detail else ""
    return f"OCR 服务返回 {exc.response.status_code}{suffix}"


def _form_data(options: OcrOptions) -> dict[str, str]:
    data: dict[str, str] = {"format": options.format}
    if options.mode:
        data["mode"] = options.mode
    if options.dpi:
        data["dpi"] = str(options.dpi)
    if options.max_pages:
        data["max_pages"] = str(options.max_pages)
    if options.password:
        data["password"] = options.password
    for key in ("math", "furniture", "image_ocr", "include_lines", "include_blocks", "include_timings"):
        value = getattr(options, key)
        if value is not None:
            data[key] = "true" if value else "false"
    return data


def _validate_files(files: Sequence[tuple[str, bytes, str]]) -> None:
    if not files:
        raise OcrError("请至少上传一个文件", kind="payload")
    settings = get_settings()
    if len(files) > settings.indra_ocr_max_files:
        raise OcrError(
            f"单次最多识别 {settings.indra_ocr_max_files} 个文件，当前 {len(files)} 个",
            kind="payload",
        )
    max_bytes = max(1, settings.indra_ocr_max_file_mb) * 1024 * 1024
    for name, content, _mime in files:
        if not content:
            raise OcrError(f"文件「{name}」内容为空", kind="payload")
        if len(content) > max_bytes:
            raise OcrError(
                f"文件「{name}」超过 {settings.indra_ocr_max_file_mb}MB 上限",
                kind="payload",
            )


def run_ocr(
    files: Sequence[tuple[str, bytes, str]],
    options: OcrOptions,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """上传若干文件到 OCR 服务并返回归一化结果。

    ``files`` 是 ``(filename, content, content_type)`` 三元组；任何失败都抛
    :class:`OcrError`，由 API 层翻译成对应 HTTP 语义。
    """

    _validate_files(files)
    if options.format not in ALLOWED_FORMATS:
        raise OcrError(f"不支持的输出格式：{options.format}", kind="payload")

    settings = get_settings()
    base_url = (settings.indra_ocr_base_url or "").strip().rstrip("/")
    if not base_url:
        raise OcrError(
            "OCR 服务尚未配置，请联系平台维护者设置 INDRA_OCR_BASE_URL",
            kind="config",
        )

    headers: dict[str, str] = {}
    api_key = (settings.indra_ocr_api_key or "").strip()
    if api_key:
        headers["X-API-Key"] = api_key

    multipart = [
        ("files", (name, content, mime or "application/octet-stream"))
        for name, content, mime in files
    ]
    effective_timeout = timeout if timeout is not None else settings.indra_ocr_timeout_seconds
    try:
        with httpx.Client(timeout=effective_timeout) as client:
            response = client.post(
                f"{base_url}{OCR_RUN_PATH}",
                files=multipart,
                data=_form_data(options),
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        raise OcrError(
            f"OCR 识别超时（超过 {int(effective_timeout)} 秒），请减少页数或稍后重试",
            kind="timeout",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OcrError(
            _describe_http_error(exc),
            kind="http",
            status=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise OcrError(
            f"无法连接 OCR 服务（{base_url}）：{exc.__class__.__name__}",
            kind="network",
        ) from exc
    except ValueError as exc:
        raise OcrError("OCR 服务返回了非 JSON 响应", kind="decode") from exc

    return normalize_ocr_response(body, filenames=[name for name, _content, _mime in files])
