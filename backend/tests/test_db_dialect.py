"""方言提供者单测:注册表、engine_kwargs、日分桶表达式形态、JSON 读改写、锁。"""

import os
from types import SimpleNamespace

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy import text as sa_text

from app.db.dialect import (
    GenericDialect,
    PostgresDialect,
    SQLiteDialect,
    get_dialect,
    register_dialect,
)
import app.db.dialect as dialect_module

import pytest

_COLUMN = Column("created_at", DateTime)


@pytest.fixture(autouse=True)
def _restore_dialect_registry():
    """用例可能注册新方言或缓存 Generic 实例:每个用例后还原注册表,防跨测试污染。"""
    snapshot = dict(dialect_module._DIALECTS)
    yield
    dialect_module._DIALECTS.clear()
    dialect_module._DIALECTS.update(snapshot)


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
    """最小 PG 会话桩:记录 SQL/事务调用并按预设返回 scalar/first。"""

    def __init__(self, scalar, *, first_row=None) -> None:
        self._scalar = scalar
        self._first_row = first_row
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return SimpleNamespace(
            scalar=lambda: self._scalar,
            first=lambda: self._first_row,
        )

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_postgres_advisory_lock_roundtrip() -> None:
    dialect = PostgresDialect()
    session = _StubSession(1)
    assert dialect.acquire_advisory_lock(session, "connector") is True
    assert "pg_try_advisory_lock(hashtext(:key), 0)" in session.calls[0][0]
    assert session.calls[0][1] == {"key": "connector"}
    # acquire 后立即提交:advisory lock 随会话存活,不能留着 idle-in-transaction
    assert session.commits == 1
    dialect.release_advisory_lock(session, "connector")
    assert "pg_advisory_unlock(hashtext(:key), 0)" in session.calls[1][0]
    assert session.commits == 2
    # 锁被占用时返回 False
    assert dialect.acquire_advisory_lock(_StubSession(0), "connector") is False


def test_postgres_check_advisory_lock_queries_pg_locks() -> None:
    dialect = PostgresDialect()
    session = _StubSession(None, first_row=(1,))
    assert dialect.check_advisory_lock(session, "connector") is True
    sql = session.calls[0][0]
    assert "pg_locks" in sql and "locktype = 'advisory'" in sql
    assert "pg_backend_pid()" in sql
    # 校验查询开启的事务必须回滚,避免持锁连接 idle-in-transaction
    assert session.rollbacks == 1

    missing = _StubSession(None, first_row=None)
    assert dialect.check_advisory_lock(missing, "connector") is False


def test_generic_file_lock_refuses_non_sqlite_backend() -> None:
    """MySQL/达梦等:url.database 是库名而非文件路径,文件锁必须响亮拒绝。"""
    from sqlalchemy.engine import make_url

    dialect = GenericDialect("mysql")
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(url=make_url("mysql+pymysql://u:p@db.internal/staffdeck"))
    )
    assert dialect.acquire_advisory_lock(session, "staffdeck-connector") is False


def test_base_check_advisory_lock_tracks_file_handle(tmp_path) -> None:
    """文件锁无静默失效:check 只核实本进程仍持有打开的句柄。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'check.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dialect = SQLiteDialect()
    with Session(engine) as session:
        assert dialect.check_advisory_lock(session, "k") is False
        assert dialect.acquire_advisory_lock(session, "k") is True
        assert dialect.check_advisory_lock(session, "k") is True
        dialect.release_advisory_lock(session, "k")
        assert dialect.check_advisory_lock(session, "k") is False


def test_default_model_partial_index_not_in_table_metadata() -> None:
    """部分唯一索引不进 metadata:其它方言 create_all 不会静默退化为全量唯一索引。"""
    import app.db.models as models  # noqa: F401

    index_names = {index.name for index in models.ModelConfig.__table__.indexes}
    assert "uq_model_configs_tenant_default" not in index_names


def test_for_update_compile_dialect_behavior() -> None:
    """FOR UPDATE:PG 真实加锁,SQLite 省略(编译期即验证,不依赖真实连接)。"""
    from sqlalchemy.dialects import postgresql as pg_dialect
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    from app.db.models import ChannelBinding

    stmt = select(ChannelBinding).where(ChannelBinding.id == "b1").with_for_update()
    assert "FOR UPDATE" in str(stmt.compile(dialect=pg_dialect.dialect()))
    assert "FOR UPDATE" not in str(stmt.compile(dialect=sqlite_dialect.dialect()))


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


def test_sqlite_file_lock_fork_child_does_not_inherit(tmp_path, monkeypatch) -> None:
    """fork 防护:继承句柄不作数(重抢并登记新进程号);父进程仍持锁时真实抢锁失败。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fork.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dialect = SQLiteDialect()
    sibling = SQLiteDialect()  # 模拟父进程仍存活持有的同一把锁
    real_pid = os.getpid()
    with Session(engine) as session:
        assert dialect.acquire_advisory_lock(session, "k") is True
        # 进入"子进程"视角(getpid 被 mock 成另一个进程号):继承句柄被关闭并按
        # 本进程身份重抢(同进程内等价:旧锁已随句柄关闭释放),锁登记到新进程号
        monkeypatch.setattr(os, "getpid", lambda: real_pid + 100000)
        assert dialect.acquire_advisory_lock(session, "k") is True
        assert dialect._lock_handles["k"][0] == real_pid + 100000
        dialect.release_advisory_lock(session, "k")
        # 父进程仍持锁(sibling 句柄打开):子进程身份真实抢锁失败
        assert sibling.acquire_advisory_lock(session, "k") is True
        assert dialect.acquire_advisory_lock(session, "k") is False
        sibling.release_advisory_lock(session, "k")
        # 父释放后可得
        assert dialect.acquire_advisory_lock(session, "k") is True
        dialect.release_advisory_lock(session, "k")


def _seed_duplicate_default_models(engine) -> None:
    from app.db.models import ModelConfig

    # sqlite 上索引已由按方言 DDL 创建,先删掉再插入(模拟无部分索引后端的数据形态)
    with engine.begin() as conn:
        conn.execute(sa_text("DROP INDEX uq_model_configs_tenant_default"))
    with Session(engine) as db:
        for name in ("m1", "m2"):
            db.add(
                ModelConfig(
                    tenant_id="t1",
                    name=name,
                    model="gpt-x",
                    api_key_encrypted="enc",
                    is_default=True,
                )
            )
        db.commit()


def test_validate_default_model_invariant_rejects_duplicates(monkeypatch, tmp_path) -> None:
    """无部分索引后端:启动校验发现同租户多条默认模型时响亮拒绝,清理后放行。"""
    import app.db.database as database
    import app.db.models  # noqa: F401 - 注册全部表模型
    from app.db.models import ModelConfig

    engine = create_engine(
        f"sqlite:///{tmp_path / 'dup.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    _seed_duplicate_default_models(engine)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "_dialect", SimpleNamespace(supports_partial_index=False))
    with pytest.raises(RuntimeError, match="多条默认模型"):
        database._validate_default_model_invariant()

    # 清理到每租户一条后校验通过
    with Session(engine) as db:
        row = db.exec(select(ModelConfig).where(ModelConfig.name == "m2")).one()
        row.is_default = False
        db.add(row)
        db.commit()
    database._validate_default_model_invariant()


def test_validate_default_model_invariant_skipped_when_partial_index_supported(
    monkeypatch, tmp_path
) -> None:
    """支持部分索引的后端:约束由 DB 唯一索引保证,启动校验直接跳过。"""
    import app.db.database as database
    import app.db.models  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'skip.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    _seed_duplicate_default_models(engine)  # 即使存在脏数据形态也不查库

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "_dialect", SimpleNamespace(supports_partial_index=True))
    database._validate_default_model_invariant()
