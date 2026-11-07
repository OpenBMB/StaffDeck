"""MySQL/达梦适配器单测:注册表、能力位、引擎参数、日分桶、命名锁、驱动提示。

全部用例不依赖真实驱动/数据库:命名锁用桩 Engine/Connection/Session(与
test_db_dialect.py 的 PG 用例同构),驱动缺失用 sys.modules 注入 None 模拟
ImportError,日分桶用 SQLAlchemy 方言离线编译。
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import make_url

import app.db.dialect as dialect_module
from app.db.dialect import (
    DatabaseDialect,
    DMDialect,
    MySQLDialect,
    ensure_driver_available,
    get_dialect,
)

_COLUMN = Column("created_at", DateTime)


# ---------- 注册表与能力位 ----------


def test_mysql_dialect_registered_with_expected_capabilities() -> None:
    dialect = get_dialect("mysql")
    assert isinstance(dialect, MySQLDialect)
    assert isinstance(dialect, DatabaseDialect)
    assert dialect.name == "mysql"
    # 无部分唯一索引:默认模型索引不创建,由 init_db 启动校验兜底
    assert dialect.supports_partial_index is False
    # 命名锁属于连接:业务层须常驻专属连接(见 PostgresDialect 同语义)
    assert dialect.session_scoped_advisory_lock is True


def test_dm_dialect_registered_with_expected_capabilities() -> None:
    dialect = get_dialect("dm")
    assert isinstance(dialect, DMDialect)
    assert isinstance(dialect, DatabaseDialect)
    assert dialect.name == "dm"
    assert dialect.supports_partial_index is False
    # 无已验证原生锁:走 BaseDialect 文件锁(非 SQLite 文件库响亮拒绝)
    assert dialect.session_scoped_advisory_lock is False


def test_mysql_engine_kwargs_pool_hardening_and_utf8mb4() -> None:
    kwargs = MySQLDialect().engine_kwargs("mysql+pymysql://u:p@h/db")
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 20
    assert kwargs["connect_args"] == {"charset": "utf8mb4"}


def test_dm_engine_kwargs_defer_to_deployment() -> None:
    """达梦方言插件/驱动为部署方安装物:不内置未经验证的项目参数。"""
    assert DMDialect().engine_kwargs("dm+dmPython://u:p@h:5236/STAFFDECK") == {}


# ---------- 日分桶(离线编译) ----------


def test_mysql_day_bucket_compiles_to_date_cast() -> None:
    compiled = str(
        MySQLDialect()
        .day_bucket(_COLUMN)
        .compile(dialect=mysql_dialect.dialect(), compile_kwargs={"literal_binds": True})
    )
    # 服务器时区自然日:标准 SQL CAST(... AS DATE),无 sqlite 的 localtime 修饰
    assert compiled == "CAST(created_at AS DATE)"


def test_dm_day_bucket_compiles_to_date_cast() -> None:
    compiled = str(
        DMDialect()
        .day_bucket(_COLUMN)
        .compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert compiled == "CAST(created_at AS DATE)"


# ---------- MySQL 命名锁(桩连接,不依赖真实 MySQL) ----------


class _StubConn:
    """专属连接桩:记录 SQL/参数、AUTOCOMMIT 设置与关闭状态。"""

    def __init__(self, scalar=1) -> None:
        self._scalar = scalar
        self.calls: list[tuple[str, dict | None]] = []
        self.isolation: str | None = None
        self.closed = False

    def execution_options(self, **kwargs):
        self.isolation = kwargs.get("isolation_level")
        return self

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return SimpleNamespace(scalar=lambda: self._scalar)

    def close(self) -> None:
        self.closed = True


class _StubEngine:
    """connect() 按需产出新 _StubConn 的引擎桩;scalar 可调模拟 GET_LOCK 返回。"""

    def __init__(self) -> None:
        self.conns: list[_StubConn] = []
        self.scalar = 1

    def connect(self):
        conn = _StubConn(self.scalar)
        self.conns.append(conn)
        return conn


class _StubSession:
    """最小会话桩:仅作 bind 来源(锁实现不得 commit/rollback 它)。"""

    def __init__(self, bind) -> None:
        self._bind = bind

    def get_bind(self):
        return self._bind


def test_mysql_advisory_lock_uses_named_lock_on_dedicated_connection() -> None:
    """acquire/check/release 全程使用同一条专属 Connection,不归池。"""
    dialect = MySQLDialect()
    engine = _StubEngine()
    session = _StubSession(engine)

    assert dialect.acquire_advisory_lock(session, "staffdeck-connector") is True
    assert len(engine.conns) == 1
    conn = engine.conns[0]
    assert conn.isolation == "AUTOCOMMIT"
    assert conn.calls[0] == ("SELECT GET_LOCK(:key, 0)", {"key": "staffdeck-connector"})
    assert conn.closed is False

    # check 在同一连接上比对 IS_USED_LOCK 与 CONNECTION_ID(),不再从池里取新连接
    assert dialect.check_advisory_lock(session, "staffdeck-connector") is True
    assert len(engine.conns) == 1
    check_sql, check_params = conn.calls[1]
    assert "IS_USED_LOCK(:key) = CONNECTION_ID()" in check_sql
    assert check_params == {"key": "staffdeck-connector"}

    dialect.release_advisory_lock(session, "staffdeck-connector")
    assert conn.calls[2][0] == "SELECT RELEASE_LOCK(:key)"
    assert conn.closed is True

    # 释放后重新取锁:新连接
    assert dialect.acquire_advisory_lock(session, "staffdeck-connector") is True
    assert len(engine.conns) == 2
    dialect.release_advisory_lock(session, "staffdeck-connector")


def test_mysql_advisory_lock_resolves_engine_from_session_bind() -> None:
    """session.get_bind() 返回 Connection 形态时取其 .engine(与 PG 同 seam)。"""
    engine = _StubEngine()
    session = _StubSession(SimpleNamespace(engine=engine))
    assert MySQLDialect().acquire_advisory_lock(session, "k") is True
    assert len(engine.conns) == 1


@pytest.mark.parametrize("locked_value", [0, None])
def test_mysql_advisory_lock_contention_or_error_closes_connection(locked_value) -> None:
    """GET_LOCK 返回 0=被占用 / NULL=错误:都不算持锁,专属连接立即关闭。"""
    dialect = MySQLDialect()
    engine = _StubEngine()
    engine.scalar = locked_value
    session = _StubSession(engine)

    assert dialect.acquire_advisory_lock(session, "connector") is False
    assert engine.conns[0].closed is True
    assert dialect.check_advisory_lock(session, "connector") is False


def test_mysql_advisory_lock_check_failure_treated_as_lost(monkeypatch) -> None:
    """连接被服务端掐断后 check 查询抛错:必须判失效(不能静默当健康)。"""
    dialect = MySQLDialect()
    engine = _StubEngine()
    session = _StubSession(engine)
    assert dialect.acquire_advisory_lock(session, "connector") is True
    conn = engine.conns[0]

    def _broken_execute(statement, params=None):
        raise RuntimeError("MySQL server has gone away")

    monkeypatch.setattr(conn, "execute", _broken_execute)
    assert dialect.check_advisory_lock(session, "connector") is False


def test_mysql_advisory_lock_reentrant_and_fork_guard(monkeypatch) -> None:
    """同进程重入不新建连接;fork 子进程继承连接不作数且不被操作/关闭。"""
    parent = MySQLDialect()
    engine = _StubEngine()
    session = _StubSession(engine)
    assert parent.acquire_advisory_lock(session, "k") is True
    assert parent.acquire_advisory_lock(session, "k") is True
    assert len(engine.conns) == 1
    parent_conn = engine.conns[0]
    parent_calls = len(parent_conn.calls)

    # 模拟另一"进程"(独立方言实例 + 另一进程号):继承状态不作数
    child = MySQLDialect()
    child._mysql_lock_conns = dict(parent._mysql_lock_conns)  # fork 继承的是内存副本
    real_pid = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: real_pid + 100000)
    assert child.check_advisory_lock(session, "k") is False
    # 子进程不得在父进程的共享连接上执行任何语句(INVALID:关闭会连带释放父锁)
    assert len(parent_conn.calls) == parent_calls
    assert parent_conn.closed is False
    assert child.acquire_advisory_lock(session, "k") is True
    assert len(engine.conns) == 2
    child.release_advisory_lock(session, "k")
    # 子进程只释放自己的新连接,继承的父连接保持原状
    assert engine.conns[1].closed is True
    assert parent_conn.closed is False
    monkeypatch.undo()
    # 父进程锁不受子进程动作影响
    assert parent.check_advisory_lock(session, "k") is True
    parent.release_advisory_lock(session, "k")
    assert parent_conn.closed is True


def test_mysql_advisory_lock_rejects_overlong_lock_name() -> None:
    """MySQL 命名锁名上限 64 字符:边界内可用,超长响亮拒绝且不碰数据库。"""
    assert MySQLDialect.MAX_LOCK_NAME_LENGTH == 64
    engine = _StubEngine()
    session = _StubSession(engine)
    dialect = MySQLDialect()
    assert dialect.acquire_advisory_lock(session, "k" * 64) is True
    dialect.release_advisory_lock(session, "k" * 64)

    engine.conns.clear()
    with pytest.raises(ValueError, match="64 个字符"):
        dialect.acquire_advisory_lock(session, "k" * 65)
    assert engine.conns == []


# ---------- 达梦:无原生锁 → 文件锁响亮拒绝 ----------


def test_dm_file_lock_refuses_non_sqlite_database() -> None:
    """达梦 url.database 是库名而非文件路径:不得当路径在 CWD 生成静默错锁。"""
    dialect = get_dialect("dm")
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            url=make_url("dm+dmPython://u:p@dm.internal:5236/STAFFDECK")
        )
    )
    assert dialect.acquire_advisory_lock(session, "staffdeck-connector") is False
    assert dialect._lock_handles == {}
    assert dialect.check_advisory_lock(session, "staffdeck-connector") is False


# ---------- 可选驱动提示 ----------


def test_optional_driver_specs_cover_mysql_and_dm() -> None:
    assert dialect_module._OPTIONAL_DRIVERS["mysql"] == (
        "pymysql",
        'pip install "skill-agent-loop-backend[mysql]"',
    )
    assert dialect_module._OPTIONAL_DRIVERS["dm"] == (
        "dmPython",
        'pip install "skill-agent-loop-backend[dm]"',
    )


@pytest.mark.parametrize(
    ("backend", "module_name", "url"),
    [
        ("mysql", "pymysql", "mysql+pymysql://u:p@db.internal/staffdeck"),
        ("dm", "dmPython", "dm+dmPython://u:p@dm.internal:5236/STAFFDECK"),
    ],
)
def test_ensure_driver_available_missing_driver_has_install_hint(
    monkeypatch, backend: str, module_name: str, url: str
) -> None:
    # sys.modules 中置 None 使 __import__ 抛 ImportError(与真实缺驱动同路径)
    monkeypatch.setitem(sys.modules, module_name, None)
    with pytest.raises(RuntimeError) as excinfo:
        ensure_driver_available(make_url(url))
    message = str(excinfo.value)
    assert backend in message
    assert "需要可选驱动" in message
    assert f"skill-agent-loop-backend[{backend}]" in message
    # 保留原始 ImportError 作为 cause,便于排障
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_ensure_driver_available_passes_when_driver_present(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pymysql", types.ModuleType("pymysql"))
    assert ensure_driver_available(make_url("mysql+pymysql://u:p@h/db")) is None
    # 未登记可选驱动的后端(sqlite/未注册)直接放行
    assert ensure_driver_available(make_url("sqlite:///x.db")) is None
    assert ensure_driver_available(make_url("mssql+pyodbc://u:p@h/db")) is None
