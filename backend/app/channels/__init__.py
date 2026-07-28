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
_feishu_process_manager = None
_dingtalk_stream_manager = None
_binding_lifecycle_locks: dict[str, threading.RLock] = {}
_binding_lifecycle_locks_guard = threading.Lock()
# connector 单实例锁:统一锁 key;PG 持锁会话常驻(_connector_lock_session)
_CONNECTOR_LOCK_KEY = "staffdeck-connector"
_connector_lock_pid: int | None = None
_connector_lock_session = None
_intake_sweep_thread: threading.Thread | None = None
_connector_lock_watchdog_thread: threading.Thread | None = None
# PG advisory lock 存活校验周期(秒):连接被服务端掐断后锁会静默释放,须定期核实
_CONNECTOR_LOCK_CHECK_SECONDS = 15.0


def _acquire_connector_process_lock() -> bool:
    """单实例 connector 锁:走方言提供者(PG=advisory lock,其它=数据目录文件锁)。

    preload+fork 部署下,子进程不得把继承的锁状态当作自己持有。
    """
    global _connector_lock_pid, _connector_lock_session
    current_pid = os.getpid()
    if _connector_lock_pid == current_pid:
        return True
    if _connector_lock_pid is not None:
        # fork 子进程:继承的会话/句柄只丢弃引用,不在共享连接上做任何操作
        _connector_lock_session = None
        _connector_lock_pid = None
    from sqlmodel import Session

    from app.db import engine
    from app.db.dialect import get_dialect

    dialect = get_dialect(engine.url.get_backend_name())
    if dialect.session_scoped_advisory_lock:
        # PG advisory lock 随连接存活:持锁会话常驻模块级,release 时才关闭
        session = Session(engine)
        if not dialect.acquire_advisory_lock(session, _CONNECTOR_LOCK_KEY):
            session.close()
            logger.error("另一 connector 进程已持有该数据库的 advisory lock")
            return False
        _connector_lock_session = session
    else:
        # SQLite/其它:数据目录文件锁(锁句柄由方言实例持有,行为不变)
        with Session(engine) as session:
            if not dialect.acquire_advisory_lock(session, _CONNECTOR_LOCK_KEY):
                return False
    _connector_lock_pid = current_pid
    return True


def _release_connector_process_lock() -> None:
    global _connector_lock_pid, _connector_lock_session
    if _connector_lock_pid is None:
        return
    if _connector_lock_pid != os.getpid():
        # fork 子进程:仅丢弃引用,不解父进程的锁
        _connector_lock_session = None
        _connector_lock_pid = None
        return
    from sqlmodel import Session

    from app.db import engine
    from app.db.dialect import get_dialect

    dialect = get_dialect(engine.url.get_backend_name())
    session, _connector_lock_session = _connector_lock_session, None
    try:
        try:
            if session is not None:
                dialect.release_advisory_lock(session, _CONNECTOR_LOCK_KEY)
            else:
                with Session(engine) as fallback_session:
                    dialect.release_advisory_lock(fallback_session, _CONNECTOR_LOCK_KEY)
        except Exception:
            # 会话可能已随连接中断死亡(锁实际已被服务端释放),释放失败只记录不抛出
            logger.exception("释放 connector 锁失败(锁可能已随断连释放)")
    finally:
        if session is not None:
            session.close()
        _connector_lock_pid = None


def _connector_lock_healthy() -> bool:
    """会话级 advisory lock 存活校验;文件锁无静默失效模式,恒为 True。"""
    if _connector_lock_session is None:
        return True
    from app.db import engine
    from app.db.dialect import get_dialect

    dialect = get_dialect(engine.url.get_backend_name())
    try:
        return dialect.check_advisory_lock(_connector_lock_session, _CONNECTOR_LOCK_KEY)
    except Exception:
        logger.exception("connector 锁存活校验异常")
        return False


def _connector_lock_watchdog() -> None:
    """PG advisory lock 断连检测:锁静默失效后主动降级(停渠道服务,避免双 connector)。"""
    while True:
        time.sleep(_CONNECTOR_LOCK_CHECK_SECONDS)
        if _connector_lock_pid != os.getpid() or _connector_lock_session is None:
            return  # 锁已正常释放或本进程不再持有,看门狗退出
        if _connector_lock_healthy():
            continue
        logger.error(
            "connector advisory lock 已失效(数据库连接中断),渠道服务主动降级停止;"
            "恢复后请重启进程重新接管"
        )
        try:
            stop_channel_services()
        except Exception:
            logger.exception("渠道服务降级停止失败")
        return


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


def get_feishu_process_manager():
    global _feishu_process_manager
    if _feishu_process_manager is None:
        from app.channels.feishu_manager import FeishuProcessManager

        _feishu_process_manager = FeishuProcessManager()
    return _feishu_process_manager


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
    import app.channels.adapters.feishu  # noqa: F401
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
    if channel == "feishu":
        return get_feishu_process_manager()
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
    if channel == "feishu":
        return get_feishu_process_manager().wait_binding_stopped(binding_id, timeout_seconds)
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
    global _intake_sweep_thread, _connector_lock_watchdog_thread
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
        get_feishu_process_manager().start()
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
        if _connector_lock_session is not None:
            # 会话级 advisory lock(PG):定期核实锁仍持有,断连静默失效时主动降级
            _connector_lock_watchdog_thread = threading.Thread(
                target=_connector_lock_watchdog,
                name="staffdeck-connector-lock-watchdog",
                daemon=True,
            )
            _connector_lock_watchdog_thread.start()
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
    feishu_manager = _feishu_process_manager
    feishu_stopped = feishu_manager is None or feishu_manager.stop(
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
        and feishu_stopped
        and dingtalk_stopped
        and sweep_stopped
    )
    if stopped:
        _release_connector_process_lock()
    else:
        logger.error("渠道线程未在期限内退出，保留 connector 锁直到进程结束")
    return stopped
