from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sqlalchemy import update
from sqlmodel import Session, select

from app.channels.feishu_process import ConnectorState, FeishuProcessSupervisor
from app.db import engine
from app.db.models import ChannelBinding, utc_now

logger = logging.getLogger(__name__)


class FeishuProcessManager:
    """Reconcile active Feishu bindings onto one spawn child per binding."""

    def __init__(
        self,
        *,
        db_engine=None,
        supervisor_factory: Callable[..., FeishuProcessSupervisor] = FeishuProcessSupervisor,
        reconcile_seconds: float = 5.0,
    ):
        self._engine = db_engine or engine
        database = self._engine.url.database
        if self._engine.url.get_backend_name() == "sqlite" and (
            not database or database == ":memory:"
        ):
            raise RuntimeError("飞书长连接子进程无法共享 SQLite 内存数据库")
        # 子进程按完整 SQLAlchemy URL 自建引擎(密码不遮蔽)
        self._database_url = self._engine.url.render_as_string(hide_password=False)
        self._supervisor_factory = supervisor_factory
        self._supervisor: FeishuProcessSupervisor | None = None
        self._reconcile_seconds = reconcile_seconds
        self._paused: set[str] = set()
        self._known: dict[str, int] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _get_supervisor(self) -> FeishuProcessSupervisor:
        with self._lock:
            if self._supervisor is None:
                self._supervisor = self._supervisor_factory(database_url=self._database_url)
            return self._supervisor

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._reconcile_loop,
                name="staffdeck-feishu-reconcile",
                daemon=True,
            )
            self._thread.start()

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except Exception:
                logger.exception("飞书绑定 reconcile 失败")
            self._wake.wait(self._reconcile_seconds)
            self._wake.clear()

    def reconcile_once(self) -> None:
        with Session(self._engine) as db:
            rows = db.exec(
                select(ChannelBinding).where(ChannelBinding.channel == "feishu")
            ).all()
            snapshots = {
                row.id: (row.status, row.config_revision, row.connected) for row in rows
            }
        with self._lock:
            paused = set(self._paused)
            known = dict(self._known)
        supervisor = self._get_supervisor() if snapshots or known else None
        for binding_id, (status, revision, connected) in snapshots.items():
            should_run = status == "active" and binding_id not in paused
            if should_run:
                try:
                    if binding_id in known and known[binding_id] != revision:
                        if not supervisor.stop_binding(binding_id, timeout=2.0):
                            continue
                    supervisor.start_binding(binding_id, revision)
                    with self._lock:
                        self._known[binding_id] = revision
                except Exception:
                    logger.warning("飞书 binding 启动失败 binding=%s", binding_id, exc_info=True)
            elif supervisor and binding_id in known:
                if supervisor.stop_binding(binding_id, timeout=2.0):
                    with self._lock:
                        self._known.pop(binding_id, None)
            actual_connected = bool(
                supervisor
                and supervisor.connected(binding_id)
            )
            if actual_connected != bool(connected):
                with Session(self._engine) as db:
                    db.exec(
                        update(ChannelBinding)
                        .where(
                            ChannelBinding.id == binding_id,
                            ChannelBinding.channel == "feishu",
                            ChannelBinding.config_revision == revision,
                            ChannelBinding.status == status,
                        )
                        .values(connected=actual_connected, updated_at=utc_now())
                    )
                    db.commit()
        for binding_id in set(known) - set(snapshots):
            stopped = not supervisor or supervisor.stop_binding(binding_id, timeout=2.0)
            if stopped:
                with self._lock:
                    self._known.pop(binding_id, None)

    def ensure_binding(self, binding_id: str) -> None:
        with Session(self._engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.channel != "feishu" or binding.status != "active":
                return
            revision = binding.config_revision
        with self._lock:
            if binding_id in self._paused:
                return
        self._get_supervisor().start_binding(binding_id, revision)
        with self._lock:
            self._known[binding_id] = revision

    def pause_binding(self, binding_id: str) -> None:
        with self._lock:
            self._paused.add(binding_id)
            supervisor = self._supervisor
            existing_stop = self._stop_events.get(binding_id)
        if not supervisor:
            return
        if existing_stop and not existing_stop.is_set():
            return
        stop_done = threading.Event()
        with self._lock:
            self._stop_events[binding_id] = stop_done

        def stop_generation() -> None:
            try:
                if supervisor.stop_binding(binding_id, timeout=5.0):
                    with self._lock:
                        self._known.pop(binding_id, None)
            finally:
                stop_done.set()

        threading.Thread(
            target=stop_generation,
            name=f"staffdeck-feishu-stop-{binding_id}",
            daemon=True,
        ).start()

    def resume_binding(self, binding_id: str, *, start: bool = True) -> None:
        with self._lock:
            stop_done = self._stop_events.get(binding_id)
        if stop_done and not stop_done.wait(timeout=5.0):
            logger.warning("飞书旧代际尚未停止，拒绝恢复 binding=%s", binding_id)
            return
        with self._lock:
            self._paused.discard(binding_id)
            self._stop_events.pop(binding_id, None)
            supervisor = self._supervisor
        if supervisor:
            supervisor.reset_backoff(binding_id)
        if start:
            try:
                self.ensure_binding(binding_id)
            except Exception:
                logger.warning("飞书 binding 恢复启动失败 binding=%s", binding_id, exc_info=True)
        self._wake.set()

    def stop_binding(self, binding_id: str) -> None:
        with self._lock:
            supervisor = self._supervisor
        stopped = not supervisor or supervisor.stop_binding(binding_id, timeout=5.0)
        if stopped:
            with self._lock:
                self._known.pop(binding_id, None)

    def wait_binding_stopped(self, binding_id: str, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            stop_done = self._stop_events.get(binding_id)
        if stop_done and not stop_done.wait(timeout=max(0.0, deadline - time.monotonic())):
            return False
        while time.monotonic() < deadline:
            with self._lock:
                supervisor = self._supervisor
            if not supervisor or supervisor.state(binding_id) in {
                ConnectorState.ABSENT,
                ConnectorState.CRASH_BACKOFF,
            }:
                return True
            time.sleep(0.01)
        return False

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
            supervisor = self._supervisor
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        thread_stopped = not (thread and thread.is_alive())
        supervisor_stopped = not supervisor or supervisor.stop(
            timeout=max(0.0, deadline - time.monotonic())
        )
        if thread_stopped and supervisor_stopped:
            with self._lock:
                self._thread = None
                self._supervisor = None
                self._known.clear()
        return thread_stopped and supervisor_stopped
