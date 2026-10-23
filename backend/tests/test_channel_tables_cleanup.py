"""渠道功能移除后的表清理回归。

两个断言防两件事：
1. SQLModel 元数据里不应再注册任何 `channel_*` 表——否则新库会凭空建出 8 张空表；
2. 老库里残留的 8 张表必须被 `_drop_legacy_channel_tables` 清掉，且可以重复执行。
"""

from sqlalchemy import create_engine, text

import app.db.models  # noqa: F401  注册全部模型
from app.db.database import _LEGACY_CHANNEL_TABLES, _drop_legacy_channel_tables
from sqlmodel import SQLModel


def _table_names(conn) -> set[str]:
    return {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}


def test_no_channel_tables_registered_in_metadata() -> None:
    registered = sorted(name for name in SQLModel.metadata.tables if name.startswith("channel_"))
    assert registered == []


def test_legacy_channel_tables_are_dropped_idempotently(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (id VARCHAR PRIMARY KEY)"))
        for table in _LEGACY_CHANNEL_TABLES:
            conn.execute(text(f"CREATE TABLE {table} (id VARCHAR PRIMARY KEY)"))

    with engine.begin() as conn:
        _drop_legacy_channel_tables(conn, _table_names(conn))
        # 第二次执行时这些表已不存在，必须静默跳过而不是报错（启动会重复调用）
        _drop_legacy_channel_tables(conn, _table_names(conn))
        remaining = _table_names(conn)

    assert set(_LEGACY_CHANNEL_TABLES) & remaining == set()
    assert "sessions" in remaining
