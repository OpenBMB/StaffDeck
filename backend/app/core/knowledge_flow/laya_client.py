"""Laya 决策头底层客户端（无 FastAPI 依赖）。

两处复用同一份出站实现，避免「企业端代理」与「SOP 走向旁路」各写一套：

- ``app/api/laya.py``：企业端 / 开放 API 的 HTTP 代理（把失败翻译成 HTTPException）。
- ``app/core/laya_router.py``：SOP 出边决策的旁路（失败静默降级回主模型）。

上游契约见 ``app/api/laya.py`` 模块文档；本模块只负责发请求与归一化异常。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import get_settings


class LayaError(RuntimeError):
    """调用 Laya 决策服务的统一异常。

    ``kind`` ∈ ``timeout`` / ``http`` / ``network`` / ``decode`` / ``shape``，
    供调用方决定降级口径——路由层要翻译成 5xx，SOP 旁路只需静默回退。
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


def describe_http_error(exc: httpx.HTTPStatusError) -> str:
    """把上游 4xx/5xx 的响应体压成一句可读错误。"""

    detail = ""
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or "")
    except ValueError:
        detail = exc.response.text[:300]
    suffix = f"：{detail}" if detail else ""
    return f"Laya 决策服务返回 {exc.response.status_code}{suffix}"


def health_url(predict_url: str) -> str:
    """由 ``/predict`` 推导 ``/health``；没有该后缀时不误删路径。"""

    parts = urlsplit(predict_url)
    path = parts.path.rstrip("/")
    if path.endswith("/predict"):
        path = path[: -len("/predict")]
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/health", "", ""))


def predict_raw(payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """POST 上游 ``/predict`` 并返回原始回包（至少含 ``answers``）。

    任何失败都抛 :class:`LayaError`——HTTP 语义翻译留在调用方，
    让「代理路由」和「SOP 旁路」各自选择正确的降级方式。
    """

    settings = get_settings()
    url = settings.laya_predict_url
    effective_timeout = timeout if timeout is not None else settings.laya_timeout_seconds
    try:
        with httpx.Client(timeout=effective_timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        raise LayaError(
            f"Laya 决策服务超时（{effective_timeout:g}s）", kind="timeout"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise LayaError(
            describe_http_error(exc), kind="http", status=exc.response.status_code
        ) from exc
    except httpx.HTTPError as exc:
        raise LayaError(
            f"无法连接 Laya 决策服务（{url}）：{exc}", kind="network"
        ) from exc
    except ValueError as exc:
        raise LayaError("Laya 决策服务返回了非 JSON 响应", kind="decode") from exc

    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise LayaError("Laya 决策服务返回结构不符合预期", kind="shape")
    return body


__all__ = [
    "LayaError",
    "describe_http_error",
    "health_url",
    "predict_raw",
]