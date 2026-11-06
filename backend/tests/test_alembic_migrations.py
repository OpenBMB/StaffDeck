"""Alembic 迁移通路回归测试。

覆盖:基线 migration 建库(表集合/两个唯一索引/样例数据/幂等/downgrade/离线 SQL),
以及非 SQLite 启动集成 ``_prepare_non_sqlite_schema`` 的三类存量情形
(全新空库 / 已有表但无版本表 / 已有 alembic_version)。

真实 PostgreSQL 验证不进入 CI,只用 tmp SQLite 文件库;PG 手工验证结论见 PR。
"""

from __future__ import annotations

import io
import logging

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.db import database
from app.db.database import _prepare_non_sqlite_schema
from app.db.models import ModelConfig, Tenant


def _alembic_config(database_url: str):
    return database._alembic_config(database_url)


def _head_revision() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()
    assert head is not None
    return head


def _upgrade_to_head(database_url: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(database_url), "head")


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _indexes_by_name(engine, table_name: str) -> dict[str, dict]:
    return {index["name"]: index for index in inspect(engine).get_indexes(table_name)}


def _assert_schema_matches_metadata(engine) -> None:
    """基线 upgrade 后的表集合必须与当前 SQLModel.metadata 完全一致。"""
    expected = set(SQLModel.metadata.tables) | {"alembic_version"}
    assert _table_names(engine) == expected


def _assert_partial_default_model_index(engine) -> None:
    index = _indexes_by_name(engine, "model_configs")["uq_model_configs_tenant_default"]
    assert index["column_names"] == ["tenant_id"]
    assert index["unique"] == 1
    # SQLite 部分索引带 sqlite_where;PG 为 postgresql_where(见基线 migration)
    assert index["dialect_options"]


def test_baseline_upgrade_builds_schema_and_indexes(tmp_path) -> None:
    db_path = tmp_path / "baseline.db"
    database_url = f"sqlite:///{db_path}"
    _upgrade_to_head(database_url)

    engine = create_engine(database_url)
    try:
        _assert_schema_matches_metadata(engine)
        _assert_partial_default_model_index(engine)

        session_index = _indexes_by_name(engine, "sessions")[
            "uq_sessions_agent_channel_extconv"
        ]
        assert session_index["column_names"] == [
            "agent_id",
            "channel",
            "channel_binding_id",
            "external_conv_id",
        ]
        assert session_index["unique"] == 1

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == _head_revision()

        # 样例数据可插入/查询(ORM 映射与 migration 建出的 schema 一致)
        with Session(engine) as session:
            session.add(Tenant(id="tenant_a", name="租户 A"))
            session.add(
                ModelConfig(
                    tenant_id="tenant_a",
                    name="默认模型",
                    api_key_encrypted="secret",
                    model="demo-model",
                    is_default=True,
                )
            )
            session.commit()
            stored = session.exec(select(ModelConfig).where(ModelConfig.tenant_id == "tenant_a"))
            assert [row.model for row in stored] == ["demo-model"]

        # 部分唯一索引生效:同租户第二条默认模型被拒绝
        with Session(engine) as session:
            session.add(
                ModelConfig(
                    tenant_id="tenant_a",
                    name="第二个默认",
                    api_key_encrypted="secret",
                    model="other-model",
                    is_default=True,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()

        # 非默认模型不受限制
        with Session(engine) as session:
            session.add(
                ModelConfig(
                    tenant_id="tenant_a",
                    name="备用模型",
                    api_key_encrypted="secret",
                    model="other-model",
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_baseline_upgrade_is_idempotent_and_downgradable(tmp_path) -> None:
    from alembic import command

    database_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    _upgrade_to_head(database_url)
    _upgrade_to_head(database_url)  # 重复 upgrade 幂等

    engine = create_engine(database_url)
    try:
        assert _table_names(engine) == set(SQLModel.metadata.tables) | {"alembic_version"}

        command.downgrade(_alembic_config(database_url), "base")
        assert _table_names(engine) == {"alembic_version"}

        command.upgrade(_alembic_config(database_url), "head")
        _assert_schema_matches_metadata(engine)
        _assert_partial_default_model_index(engine)
    finally:
        engine.dispose()


def test_env_offline_mode_renders_sql(tmp_path) -> None:
    """env.py 的 offline(--sql)通路可用,且基线含方言部分唯一索引。"""
    from alembic import command

    config = _alembic_config(f"sqlite:///{tmp_path / 'offline.db'}")
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE tenants" in sql
    assert "CREATE UNIQUE INDEX uq_model_configs_tenant_default" in sql
    assert "alembic_version" in sql


def test_prepare_non_sqlite_schema_creates_and_stamps_fresh_database(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        _prepare_non_sqlite_schema(engine)

        _assert_schema_matches_metadata(engine)
        # 全新库走 create_all,models.py 的按方言 after_create DDL 同样建出部分索引
        _assert_partial_default_model_index(engine)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == _head_revision()

        # 再次调用时已有 alembic_version,走 upgrade head,幂等
        _prepare_non_sqlite_schema(engine)
        _assert_schema_matches_metadata(engine)
    finally:
        engine.dispose()


def test_prepare_non_sqlite_schema_stamps_existing_tables_with_warning(
    tmp_path, caplog
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE tenants ("
                    "id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, "
                    "created_at DATETIME, updated_at DATETIME)"
                )
            )

        with caplog.at_level(logging.WARNING, logger="app.db.database"):
            _prepare_non_sqlite_schema(engine)

        tables = _table_names(engine)
        # 只按基线标记,不 create_all 补建缺失表
        assert "alembic_version" in tables
        assert "users" not in tables
        assert "tenants" in tables
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == _head_revision()
        assert any(
            "缺少 alembic_version" in record.getMessage() for record in caplog.records
        )
    finally:
        engine.dispose()


def test_prepare_non_sqlite_schema_upgrades_versioned_database(tmp_path) -> None:
    from alembic import command

    database_url = f"sqlite:///{tmp_path / 'versioned.db'}"
    _upgrade_to_head(database_url)
    command.downgrade(_alembic_config(database_url), "base")

    engine = create_engine(database_url)
    try:
        # 重放到 base 后只剩空的 alembic_version;helper 走 upgrade head 重建 schema
        assert _table_names(engine) == {"alembic_version"}
        _prepare_non_sqlite_schema(engine)
        _assert_schema_matches_metadata(engine)
        _assert_partial_default_model_index(engine)
    finally:
        engine.dispose()
