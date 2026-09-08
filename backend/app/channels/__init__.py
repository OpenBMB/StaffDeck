from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

from app.config import get_settings

logger = logging.getLogger(__name__)

# 进程级 ingress 管理器单例(懒创建,测试可替换)
_wechat_poll_manager = None
_wecom_stream_manager = None
_dingtalk_stream_manager = None
_binding_lifecycle_locks: dict[str, threading.RLock] = {}
_binding_lifecycle_locks_guard = threading.Lock()
_connector_pg_lock_conn = None  # PostgreSQL advisory lock 持有的连接
_connector_lock_pid: int | None = None
# advisory lock key（任选的稳定 int8；同库多个 connector 进程互斥）
_PG_CONNECTOR_LOCK_KEY = 749_102_331_977_001
# Redis 分布式锁（配置了 Redis 时优先使用；替代 advisory lock 的僵死连接问题）
_connector_redis_lock = None
_intake_sweep_thread: threading.Thread | None = None


def _acquire_connector_process_lock() -> bool:
    global _connector_lock_pid, _connector_pg_lock_conn, _connector_redis_lock
    current_pid = os.getpid()
    if _connector_lock_pid == current_pid and (
        _connector_pg_lock_conn is not None or _connector_redis_lock is not None
    ):
        return True
    # 清理旧状态
    if _connector_pg_lock_conn is not None:
        _connector_pg_lock_conn.close()
        _connector_pg_lock_conn = None
        _connector_lock_pid = None
    if _connector_redis_lock is not None:
        _connector_redis_lock = None
        _connector_lock_pid = None

    from app.db import engine

    backend = engine.url.get_backend_name()
    if backend != "postgresql":
        logger.error("渠道服务仅支持 PostgreSQL，当前：%s", backend)
        return False

    # 优先 Redis 分布式锁：进程崩溃后锁在 TTL 内自动过期，且本地开发
    # （SQLite 库）也能与远端 Redis 配合实现跨进程互斥
    from app.redis_lock import try_lock

    settings = get_settings()
    redis_lock = try_lock(
        "connector", ttl_seconds=float(settings.connector_lock_ttl_seconds)
    )
    if redis_lock is not None:
        _connector_redis_lock = redis_lock
        _connector_lock_pid = current_pid
        logger.info("connector 进程锁已通过 Redis 获取（ttl=%ss）", settings.connector_lock_ttl_seconds)
        return True

    # Redis 不可用/被占用：回退 PG advisory lock（原行为）
    from sqlalchemy import text
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_CONNECTOR_LOCK_KEY}).scalar()
    except Exception:
        conn.close()
        logger.exception("PostgreSQL advisory lock 获取失败")
        return False
    if not got:
        conn.close()
        logger.warning("PostgreSQL advisory lock 被占用")
        return False
    _connector_pg_lock_conn = conn
    _connector_lock_pid = current_pid
    return True


def _release_connector_process_lock() -> None:
    global _connector_pg_lock_conn, _connector_lock_pid, _connector_redis_lock
    if _connector_redis_lock is not None:
        if _connector_lock_pid == os.getpid():
            try:
                _connector_redis_lock.release()
            except Exception:
                logger.exception("Redis connector 锁释放失败")
        _connector_redis_lock = None
        _connector_lock_pid = None
        return
    pg_conn = _connector_pg_lock_conn
    if pg_conn is not None:
        if _connector_lock_pid == os.getpid():
            try:
                from sqlalchemy import text
                pg_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_CONNECTOR_LOCK_KEY})
            except Exception:
                logger.exception("PostgreSQL advisory lock 释放失败")
            finally:
                pg_conn.close()
        else:
            pg_conn.close()
        _connector_pg_lock_conn = None
        _connector_lock_pid = None


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


def get_dingtalk_stream_manager():
    global _dingtalk_stream_manager
    if _dingtalk_stream_manager is None:
        from app.channels.adapters.dingtalk import DingTalkStreamManager

        _dingtalk_stream_manager = DingTalkStreamManager()
    return _dingtalk_stream_manager


def channel_services_enabled() -> bool:
    # staffdeck_role 预留角色拆分：all=单体全量，connector=仅渠道连接器
    return get_settings().staffdeck_role in {"all", "connector"}


def _ensure_adapters_registered() -> None:
    # 各适配器模块导入即自注册(模块级 register_channel_adapter)
    import app.channels.adapters.dingtalk  # noqa: F401
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


def _ingress_manager(channel: str):
    if channel == "wechat":
        return get_wechat_poll_manager()
    if channel == "wecom":
        return get_wecom_stream_manager()
    if channel == "dingtalk":
        return get_dingtalk_stream_manager()
    return None


@contextmanager
def binding_lifecycle_lock(binding_id: str):
    """串行化同一 binding 的重配/删除,避免两个 HTTP 请求交错切换代际。"""
    with _binding_lifecycle_locks_guard:
        lock = _binding_lifecycle_locks.setdefault(binding_id, threading.RLock())
    with lock:
        yield


def pause_binding_ingress(channel: str, binding_id: str) -> None:
    """暂停 reconcile 并停止当前 producer/consumer。"""
    _ensure_adapters_registered()
    manager = _ingress_manager(channel)
    pause = getattr(manager, "pause_binding", None)
    if callable(pause):
        pause(binding_id)
        return
    stop_binding_ingress(channel, binding_id)


def resume_binding_ingress(channel: str, binding_id: str, *, start: bool = True) -> None:
    """解除 reconcile 暂停;start=False 时由后续 reconcile 按数据库旧配置恢复。"""
    _ensure_adapters_registered()
    manager = _ingress_manager(channel)
    resume = getattr(manager, "resume_binding", None)
    if callable(resume):
        resume(binding_id, start=start)
        return
    if start:
        start_binding_ingress(channel, binding_id)


def wait_binding_ingress_stopped(channel: str, binding_id: str, timeout_seconds: float = 5.0) -> bool:
    """有界等待指定绑定的 ingress 线程退出(重配凭证前调用)。"""
    _ensure_adapters_registered()
    if channel == "wechat":
        return get_wechat_poll_manager().wait_binding_stopped(binding_id, timeout_seconds)
    if channel == "wecom":
        return get_wecom_stream_manager().wait_binding_stopped(binding_id, timeout_seconds)
    if channel == "dingtalk":
        return get_dingtalk_stream_manager().wait_binding_stopped(binding_id, timeout_seconds)
    return True


def restart_binding_ingress(channel: str, binding_id: str, *, wait_seconds: float = 5.0) -> bool:
    """兼容入口:只有旧代际完全退出才启动新代际。"""
    pause_binding_ingress(channel, binding_id)
    stopped = wait_binding_ingress_stopped(channel, binding_id, wait_seconds)
    resume_binding_ingress(channel, binding_id, start=stopped)
    return stopped


def start_channel_services() -> None:
    global _intake_sweep_thread
    if not channel_services_enabled():
        logger.info("staffdeck_role=%s,渠道服务不启动", get_settings().staffdeck_role)
        return
    if not _acquire_connector_process_lock():
        raise RuntimeError("检测到另一 connector 进程正在运行；每个数据库仅允许一个 connector")
    try:
        _ensure_adapters_registered()
        from app.channels.service_intake import (
            start_staged_inbound_daemon,
            sweep_stale_inbound_events,
        )
        from app.channels.service_outbox import start_delivery_daemon

        get_wechat_poll_manager().start()
        get_wecom_stream_manager().start()
        get_dingtalk_stream_manager().start()
        start_delivery_daemon()
        start_staged_inbound_daemon()
        # 启动恢复:一次性清扫崩溃残留的 processing 入站事件(独立线程,不阻塞启动)
        _intake_sweep_thread = threading.Thread(
            target=sweep_stale_inbound_events,
            name="staffdeck-channel-intake-sweep",
            daemon=True,
        )
        _intake_sweep_thread.start()
    except Exception:
        stop_channel_services()
        raise


def stop_channel_services(timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    from app.channels.service_intake import stop_staged_inbound_daemon
    from app.channels.service_outbox import stop_delivery_daemon

    intake_stopped = stop_staged_inbound_daemon(
        timeout_seconds=max(0.0, deadline - time.monotonic())
    )

    outbox_stopped = stop_delivery_daemon(
        timeout_seconds=max(0.0, deadline - time.monotonic())
    )
    poll_manager = _wechat_poll_manager
    wechat_stopped = poll_manager is None or poll_manager.stop(
        timeout_seconds=max(0.0, deadline - time.monotonic())
    )
    stream_manager = _wecom_stream_manager
    wecom_stopped = stream_manager is None or stream_manager.stop(
        timeout_seconds=max(0.0, deadline - time.monotonic())
    )
    dingtalk_manager = _dingtalk_stream_manager
    dingtalk_stopped = dingtalk_manager is None or dingtalk_manager.stop(
        timeout_seconds=max(0.0, deadline - time.monotonic())
    )
    sweep_thread = _intake_sweep_thread
    if sweep_thread and sweep_thread.is_alive():
        sweep_thread.join(timeout=max(0.0, deadline - time.monotonic()))
    sweep_stopped = not (sweep_thread and sweep_thread.is_alive())
    stopped = (
        intake_stopped
        and outbox_stopped
        and wechat_stopped
        and wecom_stopped
        and dingtalk_stopped
        and sweep_stopped
    )
    if stopped:
        _release_connector_process_lock()
    else:
        logger.error("渠道线程未在期限内退出，保留 connector 锁直到进程结束")
    return stopped
