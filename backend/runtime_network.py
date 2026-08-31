"""Side-effect-free network configuration contracts shared by the launcher and API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

NETWORK_MODES = frozenset({"local", "lan", "public"})


class NetworkSettingsValidationError(ValueError):
    """Raised when a next-launch network setting is not safe to persist."""


def normalize_network_settings(mode: str, port: int, public_url: str = "") -> dict[str, str | int]:
    """Return a canonical, persistable network configuration after complete validation."""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in NETWORK_MODES:
        raise NetworkSettingsValidationError("网络模式必须是 local、lan 或 public")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise NetworkSettingsValidationError("端口必须是 1 到 65535 之间的整数")

    # A single canonical host prevents callers from writing an arbitrary bind address.
    host = "127.0.0.1" if normalized_mode == "local" else "0.0.0.0"
    normalized_public_url = ""
    if normalized_mode == "public":
        normalized_public_url = normalize_public_origin(public_url)
    return {
        "mode": normalized_mode,
        "host": host,
        "port": port,
        "public_url": normalized_public_url,
    }


def normalize_public_origin(value: str) -> str:
    """Validate an externally managed HTTP(S) origin without accepting secret-bearing URL parts."""
    candidate = str(value or "").strip()
    if not candidate:
        raise NetworkSettingsValidationError("公网模式需要提供完整的公网 URL")
    if any(ord(char) < 32 for char in candidate):
        raise NetworkSettingsValidationError("公网 URL 不能包含控制字符")

    # urlsplit validates scheme/authority shape; accessing port validates its numeric bounds.
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise NetworkSettingsValidationError("公网 URL 的端口无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkSettingsValidationError("公网 URL 必须是包含主机名的 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkSettingsValidationError("公网 URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise NetworkSettingsValidationError("公网 URL 不能包含查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise NetworkSettingsValidationError("公网 URL 必须是站点根地址，不能包含路径")

    # Rebuild the origin so a trailing slash never creates a duplicated API root.
    netloc = parsed.hostname
    if ":" in parsed.hostname and not parsed.hostname.startswith("["):
        netloc = f"[{parsed.hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def runtime_network_snapshot(cfg: Mapping[str, Any]) -> str:
    """Serialize the exact launcher-selected listener state for the ASGI process."""
    public_url = str(cfg.get("public_url") or "")
    host = str(cfg.get("host") or "127.0.0.1")
    mode = str(cfg.get("mode") or _derive_mode(host, public_url))
    normalized = normalize_network_settings(mode, int(cfg["port"]), public_url)
    return json.dumps(normalized, separators=(",", ":"))


def parse_runtime_network_snapshot(value: str) -> dict[str, str | int]:
    """Parse a launcher-owned snapshot and derive a copyable same-device origin."""
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise NetworkSettingsValidationError("当前运行网络状态不可用") from exc
    if not isinstance(payload, dict):
        raise NetworkSettingsValidationError("当前运行网络状态不可用")
    normalized = normalize_network_settings(
        str(payload.get("mode") or ""),
        payload.get("port"),
        str(payload.get("public_url") or ""),
    )
    # A LAN/public bind address is never a usable client address; local loopback is.
    local_host = "127.0.0.1" if normalized["host"] == "0.0.0.0" else normalized["host"]
    return {**normalized, "local_origin": f"http://{local_host}:{normalized['port']}"}


def _derive_mode(host: str, public_url: str) -> str:
    """Infer a mode for legacy launcher callers that do not yet provide one explicitly."""
    if str(public_url or "").strip():
        return "public"
    return "local" if host in {"127.0.0.1", "localhost", "::1"} else "lan"
