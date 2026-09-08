"""基于 Redis 的分布式锁（SET NX PX + 唯一 token + Lua 释放/续期）。

- 释放与续期都用 Lua 校验 token，防止误删他人的锁；
- 内置守护线程按 TTL 的 1/3 周期自动续期，持锁进程崩溃后锁在 TTL 内
  自动过期（替代 PG advisory lock 的僵死连接问题）；
- Redis 不可用时调用方必须自行回退（本模块不做降级决策）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLock:
    """可自动续期的 Redis 分布式锁（非可重入）。"""

    def __init__(self, client: Any, name: str, ttl_seconds: float = 30.0) -> None:
        self._client = client
        self._name = f"staffdeck:lock:{name}"
        self._ttl_ms = int(ttl_seconds * 1000)
        self._token = uuid.uuid4().hex
        self._renew_interval = max(ttl_seconds / 3.0, 1.0)
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None
        self._held = False

    def acquire(self) -> bool:
        try:
            got = bool(
                self._client.set(self._name, self._token, nx=True, px=self._ttl_ms)
            )
        except Exception:
            logger.exception("Redis 锁获取失败：%s", self._name)
            return False
        if not got:
            return False
        self._held = True
        self._start_renew()
        return True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        self._stop_renew()
        try:
            self._client.eval(_RELEASE_LUA, 1, self._name, self._token)
        except Exception:
            logger.exception("Redis 锁释放失败：%s", self._name)

    # ---- 续期 ----

    def _start_renew(self) -> None:
        self._renew_thread = threading.Thread(
            target=self._renew_loop, name=f"redis-lock-renew:{self._name}", daemon=True
        )
        self._renew_thread.start()

    def _stop_renew(self) -> None:
        self._renew_stop.set()
        thread = self._renew_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._renew_thread = None
        self._renew_stop = threading.Event()

    def _renew_loop(self) -> None:
        while not self._renew_stop.wait(self._renew_interval):
            try:
                renewed = self._client.eval(
                    _RENEW_LUA, 1, self._name, self._token, self._ttl_ms
                )
            except Exception:
                logger.exception("Redis 锁续期失败：%s", self._name)
                continue
            if not renewed:
                logger.warning("Redis 锁已丢失（被过期/删除），停止续期：%s", self._name)
                return


def try_lock(name: str, ttl_seconds: float = 30.0) -> RedisLock | None:
    """便捷入口：拿到锁返回已 acquire 的 RedisLock；拿不到或 Redis 不可用返回 None。"""

    client = get_redis()
    if client is None:
        return None
    lock = RedisLock(client, name, ttl_seconds=ttl_seconds)
    return lock if lock.acquire() else None
