"""openGauss(高斯)SQLAlchemy 方言兼容层回归测试.

覆盖:URL scheme 注册、版本串解析回退(openGauss 的 version() 不是 PG 格式)、
client_encoding=utf8 注入、应用方言层能力位,以及对上游 PG 版本串不产生回归。
"""

from __future__ import annotations

from sqlalchemy import make_url
from sqlalchemy.dialects import registry
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

from app.db import opengauss
from app.db.dialect import OpenGaussDialect, ensure_driver_available, get_dialect


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    """最小连接桩:只提供 _get_server_version_info 用到的 exec_driver_sql。"""

    def __init__(self, version_string):
        self._version_string = version_string

    def exec_driver_sql(self, statement):
        return _FakeResult(self._version_string)


def test_opengauss_sqlalchemy_dialect_registered() -> None:
    cls = registry.load("opengauss.psycopg")
    assert cls is opengauss.OpenGaussPGDialect
    assert issubclass(cls, PGDialect_psycopg)


def test_version_parse_falls_back_for_opengauss_string() -> None:
    dialect = opengauss.OpenGaussPGDialect()
    # openGauss 5.0 的真实返回格式(带 "(" 前缀,PG 方言正则无法解析)
    version = "(openGauss 5.0.0 build ) compiled at 2023-04-18 10:27:05 commit 0 last mr"
    assert dialect._get_server_version_info(_FakeConnection(version)) == (9, 2, 4)


def test_version_parse_keeps_postgresql_strings() -> None:
    dialect = opengauss.OpenGaussPGDialect()
    pg_version = "PostgreSQL 16.3 (Debian 16.3-1.pgdg120+1) on aarch64-unknown-linux-gnu"
    parsed = dialect._get_server_version_info(_FakeConnection(pg_version))
    assert parsed[0] == 16


def test_app_dialect_layer_entry() -> None:
    dialect = get_dialect("opengauss")
    assert isinstance(dialect, OpenGaussDialect)
    assert dialect.name == "opengauss"
    assert dialect.supports_partial_index is True
    assert dialect.session_scoped_advisory_lock is True
    kwargs = dialect.engine_kwargs("opengauss+psycopg://user:secret@db.internal/postgres")
    assert kwargs["connect_args"] == {"client_encoding": "utf8"}
    assert kwargs["pool_pre_ping"] is True


def test_ensure_driver_available_accepts_opengauss_url() -> None:
    # psycopg 为 [postgres] 可选依赖;开发/CI 装 dev extra 时可能缺失,缺失则跳过
    try:
        import psycopg  # noqa: F401
    except ImportError:  # pragma: no cover
        import pytest

        pytest.skip("psycopg not installed ([postgres] extra)")
    ensure_driver_available(make_url("opengauss+psycopg://user:secret@db.internal/postgres"))


def test_ensure_driver_available_message_for_missing_driver(monkeypatch) -> None:
    from app.db import dialect as dialect_module

    monkeypatch.setitem(
        dialect_module._OPTIONAL_DRIVERS,
        "opengauss",
        ("skilldeck_missing_driver_for_test", 'pip install "skill-agent-loop-backend[postgres]"'),
    )
    try:
        ensure_driver_available(make_url("opengauss+psycopg://user:secret@db.internal/postgres"))
    except RuntimeError as exc:
        assert "postgres" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing driver must raise RuntimeError")


def test_opengauss_dialect_declares_statement_cache() -> None:
    # SQLAlchemy 2.x 编译缓存需要方言显式声明,避免告警
    assert getattr(opengauss.OpenGaussPGDialect, "supports_statement_cache", False) is True
