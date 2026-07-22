from __future__ import annotations

import logging
import threading

from app.config import get_settings

logger = logging.getLogger(__name__)

# 进程级 ingress 管理器单例(懒创建,测试可替换)
_wechat_poll_manager = None
_wecom_stream_manager = None


def get_wechat_poll_manager():
    global _wechat_poll_manager
    if _wechat_poll_manager is None:
        from app.channels.adapters.wechat import WeChatPollManager

        _wechat_poll_manager = WeChatPollManager()
    return _wechat_poll_manager


def get_wecom_stream_manager():
    global _wecom_stream_manager
    if _wecom_stream_manager is None:
        from app.channels.adapters.wecom import WeComStreamManager

        _wecom_stream_manager = WeComStreamManager()
    return _wecom_stream_manager


def channel_services_enabled() -> bool:
    # staffdeck_role 预留角色拆分：all=单体全量，connector=仅渠道连接器
    return get_settings().staffdeck_role in {"all", "connector"}


def _ensure_adapters_registered() -> None:
    # 各适配器模块导入即自注册(模块级 register_channel_adapter)
    import app.channels.adapters.wechat  # noqa: F401
    import app.channels.adapters.wecom  # noqa: F401


def start_binding_ingress(channel: str, binding_id: str) -> None:
    """按注册表经适配器协议拉起指定绑定的 ingress。"""
    _ensure_adapters_registered()
    from app.channels.adapters.base import get_channel_adapter

    starter = getattr(get_channel_adapter(channel), "start_ingress", None)
    if callable(starter):
        starter(binding_id)


def stop_binding_ingress(channel: str, binding_id: str) -> None:
    _ensure_adapters_registered()
    from app.channels.adapters.base import get_channel_adapter

    stopper = getattr(get_channel_adapter(channel), "stop_ingress", None)
    if callable(stopper):
        stopper(binding_id)


def wait_binding_ingress_stopped(channel: str, binding_id: str, timeout_seconds: float = 5.0) -> bool:
    """有界等待指定绑定的 ingress 线程退出(重配凭证前调用)。"""
    _ensure_adapters_registered()
    if channel == "wechat":
        return get_wechat_poll_manager().wait_binding_stopped(binding_id, timeout_seconds)
    if channel == "wecom":
        return get_wecom_stream_manager().wait_binding_stopped(binding_id, timeout_seconds)
    return True


def restart_binding_ingress(channel: str, binding_id: str, *, wait_seconds: float = 5.0) -> None:
    """stop → 有界等待退出 → start:重配凭证时确保旧连接真正退出再拉起新连接。"""
    stop_binding_ingress(channel, binding_id)
    wait_binding_ingress_stopped(channel, binding_id, wait_seconds)
    start_binding_ingress(channel, binding_id)


def start_channel_services() -> None:
    if not channel_services_enabled():
        logger.info("staffdeck_role=%s,渠道服务不启动", get_settings().staffdeck_role)
        return
    _ensure_adapters_registered()
    from app.channels.service_intake import sweep_stale_inbound_events
    from app.channels.service_outbox import start_delivery_daemon

    get_wechat_poll_manager().start()
    get_wecom_stream_manager().start()
    start_delivery_daemon()
    # 启动恢复:一次性清扫崩溃残留的 processing 入站事件(独立线程,不阻塞启动)
    threading.Thread(
        target=sweep_stale_inbound_events,
        name="staffdeck-channel-intake-sweep",
        daemon=True,
    ).start()


def stop_channel_services() -> None:
    poll_manager = _wechat_poll_manager
    if poll_manager is not None:
        poll_manager.stop()
    stream_manager = _wecom_stream_manager
    if stream_manager is not None:
        stream_manager.stop()
    from app.channels.service_outbox import stop_delivery_daemon

    stop_delivery_daemon()
