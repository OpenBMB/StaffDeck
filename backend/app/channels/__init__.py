from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from app.config import get_settings

logger = logging.getLogger(__name__)

# 进程级 ingress 管理器单例(懒创建,测试可替换)
_wechat_poll_manager = None
_wecom_stream_manager = None
_binding_lifecycle_locks: dict[str, threading.RLock] = {}
_binding_lifecycle_locks_guard = threading.Lock()
_connector_lock_file: IO[bytes] | None = None
_connector_lock_pid: int | None = None
_intake_sweep_thread: threading.Thread | None = None


def _acquire_connector_process_lock() -> bool:
    global _connector_lock_file, _connector_lock_pid
    current_pid = os.getpid()
    if _connector_lock_file is not None and _connector_lock_pid == current_pid:
        return True
    if _connector_lock_file is not None:
        # preload 后 fork 的子进程不能把继承句柄当作自己已持有锁。
        _connector_lock_file.close()
        _connector_lock_file = None
        _connector_lock_pid = None
    from app.db import engine

    database_path = engine.url.database
    if engine.url.get_backend_name() != "sqlite" or not database_path or database_path == ":memory:":
        logger.warning("非文件 SQLite 无法启用 connector 进程锁，必须由部署层保证单实例")
        return True
    lock_path = Path(database_path).resolve().with_name(f"{Path(database_path).name}.connector.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return False
    _connector_lock_file = handle
    _connector_lock_pid = current_pid
    return True


def _release_connector_process_lock() -> None:
    global _connector_lock_file, _connector_lock_pid
    handle = _connector_lock_file
    if handle is None:
        return
    if _connector_lock_pid != os.getpid():
        handle.close()
        _connector_lock_file = None
        _connector_lock_pid = None
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        _connector_lock_file = None
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


def _ingress_manager(channel: str):
    if channel == "wechat":
        return get_wechat_poll_manager()
    if channel == "wecom":
        return get_wecom_stream_manager()
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
        from app.channels.service_intake import sweep_stale_inbound_events
        from app.channels.service_outbox import start_delivery_daemon

        get_wechat_poll_manager().start()
        get_wecom_stream_manager().start()
        start_delivery_daemon()
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
    from app.channels.service_outbox import stop_delivery_daemon

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
    sweep_thread = _intake_sweep_thread
    if sweep_thread and sweep_thread.is_alive():
        sweep_thread.join(timeout=max(0.0, deadline - time.monotonic()))
    sweep_stopped = not (sweep_thread and sweep_thread.is_alive())
    stopped = outbox_stopped and wechat_stopped and wecom_stopped and sweep_stopped
    if stopped:
        _release_connector_process_lock()
    else:
        logger.error("渠道线程未在期限内退出，保留 connector 锁直到进程结束")
    return stopped
