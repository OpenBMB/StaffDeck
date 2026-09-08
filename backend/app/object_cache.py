"""通用 ORM 模型 Redis 缓存（cache-aside）。

- 序列化用 ``model_dump(mode="json")``，重建用 ``Model(**data)``
  （pydantic 会自动把 ISO 字符串还原为 datetime 等类型）；
- 写路径必须调用 :func:`invalidate_key` 主动失效，否则最长 TTL 内读到旧值；
- Redis 不可用 → get 返回 None、set/invalidate 静默跳过，行为与无缓存一致。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_PREFIX = "staffdeck:obj:"


def cached_model(
    key: str, model_cls: type, ttl_seconds: int = 30, hydrate: dict | None = None
) -> Any | None:
    """按 key 读缓存并重建模型实例；未命中/不可用/重建失败返回 None。

    ``hydrate`` 用于填充被 exclude 掉的必填字段占位值（如 password_hash）。
    """
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_PREFIX + key)
    except Exception:
        logger.warning("Redis 模型缓存读取失败：%s", key)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        data.update(hydrate or {})
        # 必须走 model_validate（pydantic 校验路径）而非 Model(**data)：
        # SQLModel 表模型构造跳过校验，ISO 字符串不会还原为 datetime，
        # 后续 .isoformat()/比较会炸（'str' object has no attribute ...）
        return model_cls.model_validate(data)
    except Exception:
        # 反序列化失败（如模型结构变更）：当作未命中，静默回源
        return None


def store_model(
    key: str,
    instance: Any,
    ttl_seconds: int = 30,
    exclude_fields: set[str] | None = None,
) -> None:
    """缓存模型实例；Redis 不可用时静默跳过。

    ``exclude_fields`` 指定不落 Redis 的敏感字段（如 password_hash）。
    """
    client = get_redis()
    if client is None:
        return
    try:
        data = instance.model_dump(mode="json")
        for field in exclude_fields or set():
            data.pop(field, None)
        client.set(_PREFIX + key, json.dumps(data, ensure_ascii=False, default=str),
                   ex=max(ttl_seconds, 1))
    except Exception:
        logger.warning("Redis 模型缓存写入失败：%s", key)


def invalidate_key(*keys: str) -> None:
    """精确失效若干模型缓存 key（跨进程生效）；Redis 不可用时静默跳过。"""
    client = get_redis()
    if client is None or not keys:
        return
    try:
        client.delete(*[_PREFIX + k for k in keys])
    except Exception:
        logger.warning("Redis 模型缓存失效失败：%s", keys)
