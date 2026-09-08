"""Redis 客户端单例（可选底座）。

约定：
- ``REDIS_HOST`` 留空 = 完全禁用，所有使用方必须能优雅回退到现状行为；
- 连接失败时同样返回 None 并缓存结果，避免每个请求都重试拖慢响应
  （重新启用需要重启进程，属预期行为）；
- 使用方一律通过 :func:`get_redis` 取客户端，返回 None 表示「Redis 不可用」。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 全局禁用标记（未配置时避免重复构造）
_disabled = False


def get_redis() -> Any | None:
    """返回共享的 redis.Redis 实例；未配置或连不上时返回 None。"""

    global _disabled
    if _disabled:
        return None

    settings = get_settings()
    if not settings.redis_host:
        _disabled = True
        return None

    try:
        import redis  # 延迟导入：未安装 redis 包时同样走禁用路径
    except ImportError:  # pragma: no cover - 依赖缺失属环境问题
        logger.warning("未安装 redis 包，Redis 功能禁用（pip install redis）")
        _disabled = True
        return None

    try:
        client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            decode_responses=True,
        )
        client.ping()
    except Exception:
        logger.warning("Redis 连接失败（%s:%s），相关功能回退为无 Redis 模式",
                       settings.redis_host, settings.redis_port)
        _disabled = True
        return None

    logger.info("Redis 已连接：%s:%s db=%s", settings.redis_host, settings.redis_port,
                settings.redis_db)
    return client
