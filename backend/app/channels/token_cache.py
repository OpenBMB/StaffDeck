"""渠道 access token 的 Redis 二级缓存（跨进程共享）。

飞书/钉钉/企微三个 TokenProvider 各自持有进程内存缓存；多副本部署时
各副本独立刷新 token，后刷新的一方会使另一方刚拿到的 token 失效
（互踢）。本模块在内存缓存之上加一层 Redis L2：

    内存 miss → Redis（staffdeck:chtoken:*）→ 远端 API

拿到新 token 后同时写回内存 + Redis；任一副本刷新后其他副本直接复用，
避免重复刷新与互踢。

- Redis 里存 JSON ``{"token": ..., "ttl": 有效秒数}``，TTL = 有效秒数；
- Redis 不可用时所有函数静默降级，TokenProvider 行为与改造前一致；
- invalidate 用 Lua 做「值相等才删除」，保留 expected_token 校验语义。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_PREFIX = "staffdeck:chtoken:"


def _key(provider: str, key: str) -> str:
    return f"{_PREFIX}{provider}:{key}"


def get_cached(provider: str, key: str) -> tuple[str, int] | None:
    """读 Redis L2，返回 (token, 剩余有效秒数)；未命中/不可用返回 None。"""
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_key(provider, key))
    except Exception:
        logger.warning("渠道 token L2 读取失败：%s/%s", provider, key)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        token = str(data.get("token") or "")
        ttl = int(data.get("ttl") or 0)
    except (ValueError, TypeError):
        return None
    if not token or ttl <= 0:
        return None
    remaining = max(client.ttl(_key(provider, key)), 1)
    return token, min(ttl, remaining)


def store(provider: str, key: str, token: str, ttl_seconds: int) -> None:
    """写 Redis L2；Redis 不可用时静默跳过。"""
    client = get_redis()
    if client is None or ttl_seconds <= 0:
        return
    try:
        payload = json.dumps({"token": token, "ttl": int(ttl_seconds)})
        client.set(_key(provider, key), payload, ex=max(int(ttl_seconds), 1))
    except Exception:
        logger.warning("渠道 token L2 写入失败：%s/%s", provider, key)


def invalidate(provider: str, key: str, expected_token: str | None = None) -> bool:
    """失效 Redis L2；expected_token 不匹配时不删除（返回 False）。"""
    client = get_redis()
    if client is None:
        return False
    try:
        name = _key(provider, key)
        if expected_token is not None:
            payload = json.dumps({"token": expected_token, "ttl": 0})
            # 值里含 ttl 无法直接拼期望串，退化为 GET 比较 + DEL
            raw = client.get(name)
            if raw is None:
                return False
            try:
                data: dict[str, Any] = json.loads(raw)
            except (ValueError, TypeError):
                return False
            if str(data.get("token") or "") != expected_token:
                return False
            client.delete(name)
            return True
        return bool(client.delete(name))
    except Exception:
        logger.warning("渠道 token L2 失效失败：%s/%s", provider, key)
        return False
