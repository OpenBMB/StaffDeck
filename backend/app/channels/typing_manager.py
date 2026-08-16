from __future__ import annotations

import json
import logging
import threading
from typing import Any

from app.channels.adapters.base import (
    ChannelCapability,
    channel_capabilities_of,
    get_channel_adapter,
)
from app.db import engine as default_engine
from app.db.models import ChannelBinding

logger = logging.getLogger(__name__)

TYPING_INTERVAL_SECONDS = 8.0


def _typing_key(binding: ChannelBinding, target: dict[str, Any]) -> tuple[str, str]:
    """同一绑定的同一回复目标对应一个 typing 定时器(按 target 内容区分)。"""
    return binding.id, json.dumps(target, ensure_ascii=False, sort_keys=True)


def _binding_features_typing(binding: ChannelBinding) -> bool:
    """§3.1 features.typing 开关:缺失时默认开启,保持存量行为。"""
    features = (binding.config_json or {}).get("features") or {}
    return bool(features.get("typing", True))


def _adapter_supports_typing(adapter: object) -> bool:
    """typing 能力门禁:适配器必须同时具备 send_typing 方法与 TYPING 能力声明。

    hasattr 只是协议存在性检查;能力声明(ChannelCapabilityAdapter 协议)进一步
    限定仅 Discord 等显式声明 TYPING 的渠道启用周期性 typing。微信虽然实现了
    send_typing(一次性 1/2 状态调用),但未声明 TYPING 能力,因此不会被本管理器
    接管,其现有 intake 行为保持不变。
    """
    if not callable(getattr(adapter, "send_typing", None)):
        return False
    return ChannelCapability.TYPING in channel_capabilities_of(adapter)


class TypingManager:
    """周期性 typing 指示器管理:入站处理期间每 TYPING_INTERVAL_SECONDS 触发一次。

    单进程内存实现:每个 (binding, target) 一条 daemon 定时器链,处理结束由
    end() 取消。Discord 无「停止 typing」语义,typing 在最后一次触发约 10s 后
    自动消失,因此 end() 只需停止重复触发,不发送结束状态。
    """

    def __init__(
        self,
        interval_seconds: float = TYPING_INTERVAL_SECONDS,
        db_engine=None,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._db_engine = db_engine
        self._timers: dict[tuple[str, str], threading.Timer] = {}
        self._lock = threading.Lock()

    def begin(self, binding: ChannelBinding, target: dict[str, Any]) -> None:
        """进入入站处理时启动周期性 typing;能力不满足或无 send_typing 时 no-op。"""
        if not _binding_features_typing(binding):
            return
        try:
            adapter = get_channel_adapter(binding.channel)
        except ValueError:
            logger.debug("typing 跳过:未注册渠道适配器 channel=%s", binding.channel)
            return
        if not _adapter_supports_typing(adapter):
            return
        key = _typing_key(binding, target)
        with self._lock:
            if key in self._timers:
                return
            timer = threading.Timer(
                self._interval_seconds,
                self._pulse,
                args=(binding.id, target),
            )
            timer.daemon = True
            self._timers[key] = timer
        # 处理开始先立即触发一次,短处理(<8s)也能展示 typing
        self._send_pulse(binding, target)
        self._timers[key].start()

    def end(self, binding: ChannelBinding, target: dict[str, Any]) -> None:
        """处理完成或异常时停止周期性 typing(不发送结束状态,Discord 无此语义)。"""
        if not _binding_features_typing(binding):
            return
        key = _typing_key(binding, target)
        with self._lock:
            timer = self._timers.pop(key, None)
        if timer is None:
            return
        timer.cancel()

    def _pulse(self, binding_id: str, target: dict[str, Any]) -> None:
        """定时器回调:触发一次 typing 并重排下一轮;已取消或绑定删除则不重排。"""
        from sqlmodel import Session

        try:
            with Session(self._db_engine or default_engine) as session:
                binding = session.get(ChannelBinding, binding_id)
                if binding is None:
                    logger.debug("typing 停止:绑定已删除 binding=%s", binding_id)
                    return
                session.expunge(binding)
                self._send_pulse(binding, target)
        except Exception:
            logger.exception("typing 脉冲发送失败 binding=%s", binding_id)
            return
        key = (binding_id, json.dumps(target, ensure_ascii=False, sort_keys=True))
        with self._lock:
            if key not in self._timers:
                return
            self._timers[key].cancel()
            timer = threading.Timer(
                self._interval_seconds,
                self._pulse,
                args=(binding_id, target),
            )
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _send_pulse(self, binding: ChannelBinding, target: dict[str, Any]) -> None:
        try:
            adapter = get_channel_adapter(binding.channel)
            send_typing = getattr(adapter, "send_typing", None)
            if not callable(send_typing):
                return
            send_typing(binding, target, 1)
        except Exception:
            logger.debug(
                "渠道 typing 状态发送失败(忽略) binding=%s status=1",
                binding.id,
                exc_info=True,
            )

    def active_keys(self) -> list[tuple[str, str]]:
        """当前活跃的 (binding_id, target) 键列表(测试/诊断用)。"""
        with self._lock:
            return list(self._timers)


# 模块级单例:intake/outbox 共用同一批定时器
typing_manager = TypingManager()


def begin_typing(binding: ChannelBinding, target: dict[str, Any]) -> None:
    typing_manager.begin(binding, target)


def end_typing(binding: ChannelBinding, target: dict[str, Any]) -> None:
    typing_manager.end(binding, target)
