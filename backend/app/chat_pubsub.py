"""聊天 SSE relay 的 Redis Pub/Sub 推送。

- DB（AgentEvent relay 行）仍是唯一事实源，Pub/Sub 只做「有新事件」的唤醒信号，
  丢消息不影响正确性（SSE 循环有兜底轮询）；
- 发布端：_persist_relay_only_event 提交 relay 行后调用 :func:`publish_relay_wake`；
- 订阅端：SSE 循环用 :class:`RelaySubscriber` 等待唤醒，Redis 不可用时回退原轮询。
"""

from __future__ import annotations

import logging
import time

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "staffdeck:chat-relay:"


def publish_relay_wake(session_id: str) -> None:
    """relay 行落库后唤醒正在监听该会话的 SSE 流（fire-and-forget）。"""
    client = get_redis()
    if client is None or not session_id:
        return
    try:
        client.publish(CHANNEL_PREFIX + session_id, "1")
    except Exception:
        logger.warning("relay 唤醒推送失败：session=%s", session_id)


class RelaySubscriber:
    """按会话订阅 relay 唤醒；Redis 不可用时 ``wait`` 退化为普通 sleep。"""

    def __init__(self) -> None:
        self._client = get_redis()
        self._pubsub = None
        self._channel: str | None = None

    @property
    def active(self) -> bool:
        return self._client is not None

    def wait(self, session_id: str, timeout: float) -> bool:
        """阻塞至多 timeout 秒等待该会话的推送；返回是否被推送唤醒。

        - Redis 不可用或 session 为空 → sleep(timeout) 并返回 False；
        - 会话切换时自动换订阅通道。
        """
        if self._client is None or not session_id:
            time.sleep(timeout)
            return False
        channel = CHANNEL_PREFIX + session_id
        try:
            if self._pubsub is None:
                self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                self._channel = channel
                self._pubsub.subscribe(channel)
            elif self._channel != channel:
                self._pubsub.unsubscribe(self._channel)
                self._channel = channel
                self._pubsub.subscribe(channel)
            message = self._pubsub.get_message(timeout=timeout)
            return message is not None
        except Exception:
            logger.warning("relay 订阅异常，退化为纯轮询：session=%s", session_id)
            self.close()
            time.sleep(timeout)
            return False

    def close(self) -> None:
        try:
            if self._pubsub is not None:
                self._pubsub.close()
        except Exception:
            pass
        self._pubsub = None
        self._channel = None
