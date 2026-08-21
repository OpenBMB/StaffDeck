"""正向代理(企业私有化外网出口)统一收口。

取值优先级:no_proxy 命中/私网自动绕过 > 显式配置(.env/settings)> 进程环境变量。

- 显式配置生效时,启动期收敛进 os.environ(HTTP_PROXY/HTTPS_PROXY/NO_PROXY,
  大小写双写),httpx(trust_env)、websockets(getproxies/proxy_bypass)、pip
  等栈自动生效——企微 WS 这类不支持显式代理参数的 SDK 也因此被覆盖;
- httpx 主路径另外显式注入(按目标 URL 先查绕过名单),保证"显式配置优先于
  环境变量"的语义在打包版里也可靠;
- 私网地址(localhost/无点主机名/10·172.16-31·192.168·169.254/::1)恒直连,
  内网模型服务绝不被代理出去。
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
    )
)

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _settings_proxy(kind: str) -> str:
    """settings 里的显式代理配置(kind: http/https/no_proxy);未配置返回 ""。"""
    from app.config import get_settings

    settings = get_settings()
    return str(getattr(settings, kind, "") or "").strip()


def no_proxy_entries() -> list[str]:
    """显式配置的绕过名单(逗号分隔,域名后缀/主机名/IP 段,*=全部直连)。"""
    raw = _settings_proxy("no_proxy")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def is_private_host(host: str) -> bool:
    """私网地址恒直连:localhost、无点主机名、RFC1918/环回/链路本地 IP。"""
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return True
    if host == "localhost" or "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名:靠 no_proxy 名单匹配
    return any(address in network for network in _PRIVATE_NETWORKS)


def should_bypass_proxy(host: str) -> bool:
    """是否绕过代理:私网自动绕过 + no_proxy 名单(后缀/精确/*)命中。"""
    host = (host or "").strip().lower()
    if not host:
        return True
    if is_private_host(host):
        return True
    for entry in no_proxy_entries():
        if entry == "*":
            return True
        if entry.startswith("*."):  # *.corp.internal 形态
            suffix = entry[1:]  # .corp.internal
            if host.endswith(suffix) or host == suffix[1:]:
                return True
            continue
        if entry.startswith("."):  # .corp.internal 后缀形态
            if host.endswith(entry) or host == entry[1:]:
                return True
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def proxy_for_url(url: str) -> str | None:
    """按目标 URL 决定显式代理:绕过名单命中返回 None,未配置显式代理也返回 None
    (此时由 httpx trust_env / websockets getproxies 读环境变量兜底)。"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname or ""
    if should_bypass_proxy(host):
        return None
    if parsed.scheme == "https":
        proxy = _settings_proxy("https_proxy") or _settings_proxy("http_proxy")
    else:
        proxy = _settings_proxy("http_proxy") or _settings_proxy("https_proxy")
    return proxy or None


def httpx_proxy_kwargs(url: str) -> dict[str, Any]:
    """httpx 客户端构造参数:

    - 绕过名单/私网命中:返回精确主机的无代理 mounts(强制直连,不读环境代理;
      其余 trust_env 行为如 SSL_CERT_FILE 保留);
    - 显式配置代理:返回 {"proxy": ...}(显式配置优先于环境变量);
    - 均未配置:返回 {}(trust_env 读环境变量,NO_PROXY 环境语义兜底)。
    """
    import httpx

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if should_bypass_proxy(host):
        return {"mounts": {f"all://{host}": httpx.HTTPTransport()}}
    proxy = proxy_for_url(url)
    return {"proxy": proxy} if proxy else {}


def apply_proxy_env() -> None:
    """启动期把显式代理配置收敛进进程环境(大小写双写)。

    仅覆盖显式配置的键;未配置时不动既有环境变量(系统级代理照常工作)。
    覆盖后:httpx trust_env、websockets proxy_bypass/getproxies、pip 等全部生效。
    """
    http_proxy = _settings_proxy("http_proxy")
    https_proxy = _settings_proxy("https_proxy")
    no_proxy = _settings_proxy("no_proxy")
    applied: list[str] = []
    for key, value in (
        ("HTTP_PROXY", http_proxy),
        ("HTTPS_PROXY", https_proxy or http_proxy),
        ("NO_PROXY", no_proxy),
    ):
        if not value:
            continue
        os.environ[key] = value
        os.environ[key.lower()] = value
        applied.append(key)
    if applied:
        logger.info("正向代理已生效(%s);no_proxy 名单:%s", ",".join(applied), no_proxy or "空")


def proxy_host_for_allowlist() -> str:
    """代理主机名:沙箱 allowlist 网络模式下需要并入允许域。"""
    proxy = _settings_proxy("https_proxy") or _settings_proxy("http_proxy")
    if not proxy:
        return ""
    return (urlparse(proxy if "://" in proxy else f"http://{proxy}").hostname or "").lower()
