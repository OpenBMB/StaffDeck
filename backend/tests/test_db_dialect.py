"""方言提供者单测:注册表、engine_kwargs、日分桶表达式形态、JSON 读改写、锁。"""

from types import SimpleNamespace

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import text as sa_text

from app.db.dialect import (
    GenericDialect,
    PostgresDialect,
    SQLiteDialect,
    get_dialect,
    register_dialect,
)

_COLUMN = Column("created_at", DateTime)


def test_registry_returns_expected_dialects() -> None:
    assert get_dialect("sqlite").name == "sqlite"
    assert get_dialect("sqlite").supports_partial_index is True
    assert get_dialect("postgresql").name == "postgresql"
    assert get_dialect("postgresql").supports_partial_index is True
    # 未注册后端回退 GenericDialect(名按 backend 名,能力保守)
    fallback = get_dialect("mysql")
    assert isinstance(fallback, GenericDialect)
    assert fallback.name == "mysql"
    assert fallback.supports_partial_index is False


def test_register_dialect_override() -> None:
    custom = GenericDialect("dm")
    register_dialect("dm", custom)
    assert get_dialect("dm") is custom


def test_engine_kwargs_sqlite_matches_legacy_behavior() -> None:
    kwargs = get_dialect("sqlite").engine_kwargs("sqlite:///x.db")
    assert kwargs == {"connect_args": {"check_same_thread": False, "timeout": 30}}
    assert get_dialect("postgresql").engine_kwargs("postgresql+psycopg://h/db") == {}


def test_day_bucket_sqlite_keeps_localtime_date() -> None:
    compiled = str(
        SQLiteDialect()
        .day_bucket(_COLUMN)
        .compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "date(" in compiled and "localtime" in compiled


def test_day_bucket_postgres_casts_with_configured_timezone(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.db.dialect.get_settings", lambda: SimpleNamespace(app_timezone="Asia/Shanghai")
    )
    compiled = str(
        PostgresDialect()
        .day_bucket(_COLUMN)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "timezone('Asia/Shanghai'" in compiled
    assert compiled.startswith("CAST(") and compiled.endswith("AS DATE)")


def test_day_bucket_postgres_falls_back_to_local_offset(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.db.dialect.get_settings", lambda: SimpleNamespace(app_timezone="")
    )
    compiled = str(
        PostgresDialect()
        .day_bucket(_COLUMN)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "make_interval" in compiled
    assert compiled.startswith("CAST(") and compiled.endswith("AS DATE)")


def test_day_bucket_generic_is_standard_cast() -> None:
    compiled = str(
        GenericDialect("dm")
        .day_bucket(_COLUMN)
        .compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert compiled.startswith("CAST(") and " AS DATE)" in compiled


def test_json_config_helpers_read_modify_write() -> None:
    dialect = SQLiteDialect()
    assert dialect.json_config_get(None, "k") is None
    config = dialect.json_config_set({"a": 1}, "b", {"x": True})
    assert config == {"a": 1, "b": {"x": True}}
    # 不改动入参原 dict
    assert dialect.json_config_get(config, "a") == 1
    removed = dialect.json_config_remove(config, "a")
    assert removed == {"b": {"x": True}}
    assert dialect.json_config_remove(config, "missing") == config


def test_sqlite_file_advisory_lock_excludes_other_holder(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'lock.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    first, second = SQLiteDialect(), SQLiteDialect()
    with Session(engine) as session:
        assert first.acquire_advisory_lock(session, "connector") is True
        # 同一持有者重入成功
        assert first.acquire_advisory_lock(session, "connector") is True
        # 另一持有者抢同一把文件锁失败
        assert second.acquire_advisory_lock(session, "connector") is False
        first.release_advisory_lock(session, "connector")
        # 释放后可被他人获取
        assert second.acquire_advisory_lock(session, "connector") is True
        second.release_advisory_lock(session, "connector")


class _StubSession:
    """最小 PG 会话桩:记录 SQL 并按预设返回 scalar。"""

    def __init__(self, scalar) -> None:
        self._scalar = scalar
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return SimpleNamespace(scalar=lambda: self._scalar)


def test_postgres_advisory_lock_roundtrip() -> None:
    dialect = PostgresDialect()
    session = _StubSession(1)
    assert dialect.acquire_advisory_lock(session, "connector") is True
    assert "pg_try_advisory_lock(hashtext" in session.calls[0][0]
    assert session.calls[0][1] == {"key": "connector"}
    dialect.release_advisory_lock(session, "connector")
    assert "pg_advisory_unlock(hashtext" in session.calls[1][0]
    # 锁被占用时返回 False
    assert dialect.acquire_advisory_lock(_StubSession(0), "connector") is False


def test_create_all_contains_channel_session_unique_index() -> None:
    import app.db.models  # noqa: F401 - 注册全部表模型

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        session_indexes = {
            row[1] for row in conn.execute(sa_text("PRAGMA index_list('sessions')"))
        }
        assert "uq_sessions_agent_channel_extconv" in session_indexes
        index_sql = conn.execute(
            sa_text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='uq_model_configs_tenant_default'"
            )
        ).scalar_one()
        # 部分唯一索引:仅默认模型一行受约束
        assert "WHERE is_default = 1" in index_sql
