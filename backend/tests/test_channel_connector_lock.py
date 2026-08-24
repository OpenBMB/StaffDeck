"""connector 单实例锁:方言分发、SQLite 文件锁行为、PG 持锁会话常驻、占用冲突。"""

import os
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.channels as channels
from app.db.dialect import SQLiteDialect


def _sqlite_engine(tmp_path, name: str = "connector.db"):
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _reset_connector_lock_state() -> None:
    channels._connector_lock_pid = None
    channels._connector_lock_session = None


class _FakeSessionScopedDialect:
    """PG 形态的假方言:记录 acquire/release,会话级锁标志位为真。"""

    session_scoped_advisory_lock = True

    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.lock_held = acquired
        self.calls: list[tuple[str, str]] = []

    def acquire_advisory_lock(self, session, key: str) -> bool:
        self.calls.append(("acquire", key))
        return self.acquired

    def release_advisory_lock(self, session, key: str) -> None:
        self.calls.append(("release", key))

    def check_advisory_lock(self, session, key: str) -> bool:
        self.calls.append(("check", key))
        return self.lock_held


def test_connector_lock_sqlite_file_roundtrip(tmp_path, monkeypatch) -> None:
    engine = _sqlite_engine(tmp_path)
    monkeypatch.setattr("app.db.engine", engine)
    _reset_connector_lock_state()

    assert channels._acquire_connector_process_lock() is True
    # 重入成功
    assert channels._acquire_connector_process_lock() is True
    # 数据目录文件锁已落盘(行为不变,锁文件名带统一 key)
    assert (tmp_path / "connector.db.staffdeck-connector.lock").exists()
    channels._release_connector_process_lock()
    assert channels._connector_lock_pid is None
    # 释放后可再获取
    assert channels._acquire_connector_process_lock() is True
    channels._release_connector_process_lock()
    _reset_connector_lock_state()


def test_connector_lock_sqlite_contention_returns_false(tmp_path, monkeypatch) -> None:
    engine = _sqlite_engine(tmp_path)
    monkeypatch.setattr("app.db.engine", engine)
    _reset_connector_lock_state()

    holder = SQLiteDialect()
    with Session(engine) as session:
        # 另一"进程"(独立方言实例)已持有同一文件锁
        assert holder.acquire_advisory_lock(session, channels._CONNECTOR_LOCK_KEY) is True
        assert channels._acquire_connector_process_lock() is False
        holder.release_advisory_lock(session, channels._CONNECTOR_LOCK_KEY)
    # 释放后可获取
    assert channels._acquire_connector_process_lock() is True
    channels._release_connector_process_lock()
    _reset_connector_lock_state()


def test_connector_lock_pg_resident_session_roundtrip(monkeypatch) -> None:
    engine = create_engine("postgresql+psycopg://user:secret@db.internal/staffdeck")
    monkeypatch.setattr("app.db.engine", engine)
    dialect = _FakeSessionScopedDialect()
    monkeypatch.setattr("app.db.dialect.get_dialect", lambda _name: dialect)
    _reset_connector_lock_state()

    assert channels._acquire_connector_process_lock() is True
    assert dialect.calls == [("acquire", channels._CONNECTOR_LOCK_KEY)]
    # PG advisory lock 随连接存活:持锁会话常驻模块级
    assert channels._connector_lock_session is not None
    channels._release_connector_process_lock()
    assert ("release", channels._CONNECTOR_LOCK_KEY) in dialect.calls
    assert channels._connector_lock_session is None
    _reset_connector_lock_state()


def test_connector_lock_pg_contention_closes_session(monkeypatch) -> None:
    engine = create_engine("postgresql+psycopg://user:secret@db.internal/staffdeck")
    monkeypatch.setattr("app.db.engine", engine)
    dialect = _FakeSessionScopedDialect(acquired=False)
    monkeypatch.setattr("app.db.dialect.get_dialect", lambda _name: dialect)
    _reset_connector_lock_state()

    assert channels._acquire_connector_process_lock() is False
    assert channels._connector_lock_session is None
    assert channels._connector_lock_pid is None
    _reset_connector_lock_state()


# ---------- 会话级锁存活校验与断连降级 ----------


def test_connector_lock_healthy_without_session_is_true() -> None:
    """文件锁路径无常驻会话:无静默失效模式,健康检查恒 True。"""
    _reset_connector_lock_state()
    assert channels._connector_lock_healthy() is True


def test_connector_lock_healthy_delegates_to_dialect(monkeypatch) -> None:
    engine = create_engine("postgresql+psycopg://user:secret@db.internal/staffdeck")
    monkeypatch.setattr("app.db.engine", engine)
    dialect = _FakeSessionScopedDialect()
    monkeypatch.setattr("app.db.dialect.get_dialect", lambda _name: dialect)
    _reset_connector_lock_state()

    channels._connector_lock_session = object()
    dialect.lock_held = True
    assert channels._connector_lock_healthy() is True
    dialect.lock_held = False
    assert channels._connector_lock_healthy() is False
    _reset_connector_lock_state()


def test_connector_lock_watchdog_degrades_on_lock_loss(monkeypatch) -> None:
    """锁静默失效:看门狗调用 stop_channel_services 主动降级后退出。"""
    _reset_connector_lock_state()
    monkeypatch.setattr(channels, "_CONNECTOR_LOCK_CHECK_SECONDS", 0)
    channels._connector_lock_pid = os.getpid()
    channels._connector_lock_session = object()
    monkeypatch.setattr(channels, "_connector_lock_healthy", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(
        channels, "stop_channel_services", lambda: calls.append("stop") or True
    )

    channels._connector_lock_watchdog()
    assert calls == ["stop"]
    _reset_connector_lock_state()


def test_connector_lock_watchdog_exits_after_normal_release(monkeypatch) -> None:
    """正常释放锁后看门狗自行退出,不触发降级。"""
    _reset_connector_lock_state()
    monkeypatch.setattr(channels, "_CONNECTOR_LOCK_CHECK_SECONDS", 0)
    # pid=None:锁未持有,看门狗第一轮即返回
    calls: list[str] = []
    monkeypatch.setattr(
        channels, "stop_channel_services", lambda: calls.append("stop") or True
    )
    channels._connector_lock_watchdog()
    assert calls == []


def test_release_tolerates_dead_lock_session(monkeypatch) -> None:
    """释放路径容错:会话已随断连死亡(release 抛错)时状态仍被清理,不再抛出。"""
    engine = create_engine("postgresql+psycopg://user:secret@db.internal/staffdeck")
    monkeypatch.setattr("app.db.engine", engine)

    class _DeadReleaseDialect(_FakeSessionScopedDialect):
        def release_advisory_lock(self, session, key: str) -> None:
            raise RuntimeError("connection is closed")

    dialect = _DeadReleaseDialect()
    monkeypatch.setattr("app.db.dialect.get_dialect", lambda _name: dialect)
    _reset_connector_lock_state()
    channels._connector_lock_pid = os.getpid()
    channels._connector_lock_session = SimpleNamespace(close=lambda: None)

    channels._release_connector_process_lock()  # 不应抛出
    assert channels._connector_lock_pid is None
    assert channels._connector_lock_session is None
