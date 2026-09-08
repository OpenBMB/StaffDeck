"""阿里云 APIG 读端点的 Redis 缓存/节流助手（cache-aside）。

语义约定（务必保持）：
- 只做「隐式同步」的节流与读缓存；显式同步端点（mirror=True）成功后必须
  调用 :func:`invalidate_tenant` 清掉该租户的全部节流 key；
- key 前缀统一 ``staffdeck:aigw:``，租户维度隔离；
- Redis 不可用时所有函数都返回「未命中/不节流」，端点行为与改造前完全一致。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_PREFIX = "staffdeck:aigw:"


def _ttl() -> int:
    return max(int(get_settings().aigw_cache_ttl_seconds), 1)


def acquire_sync_throttle(key: str) -> bool:
    """隐式同步节流：TTL 内已有进程同步过时返回 True（本次应跳过云端调用）。

    Redis 不可用 → 恒返回 False（每次都同步，与改造前一致）。
    """
    client = get_redis()
    if client is None:
        return False
    try:
        return not bool(client.set(_PREFIX + key, "1", nx=True, ex=_ttl()))
    except Exception:
        logger.warning("Redis 节流 key 写入失败，按未节流处理：%s", key)
        return False


def get_json(key: str) -> Any | None:
    """读 JSON 缓存；未命中或 Redis 不可用返回 None。"""
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_PREFIX + key)
    except Exception:
        logger.warning("Redis 读缓存失败：%s", key)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """写 JSON 缓存；Redis 不可用时静默跳过。"""
    client = get_redis()
    if client is None:
        return
    try:
        client.set(_PREFIX + key, json.dumps(value, ensure_ascii=False),
                   ex=ttl_seconds or _ttl())
    except Exception:
        logger.warning("Redis 写缓存失败：%s", key)


def delete_keys(*keys: str) -> None:
    """精确删除若干缓存 key（Redis 不可用时静默跳过）。"""
    client = get_redis()
    if client is None or not keys:
        return
    try:
        client.delete(*[_PREFIX + k for k in keys])
    except Exception:
        logger.warning("Redis 删除缓存失败：%s", keys)


def invalidate_tenant(tenant_id: str) -> None:
    """清掉某租户的全部 aigw 缓存/节流 key（显式同步、审批等写路径后调用）。"""
    client = get_redis()
    if client is None:
        return
    pattern = f"{_PREFIX}{tenant_id}:*"
    try:
        keys = list(client.scan_iter(match=pattern, count=200))
        if keys:
            client.delete(*keys)
    except Exception:
        logger.warning("Redis 清理租户缓存失败：%s", tenant_id)
