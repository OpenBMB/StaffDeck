import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import database


def _legacy_sessions_ddl(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE sessions (
                id VARCHAR PRIMARY KEY,
                tenant_id VARCHAR,
                user_id VARCHAR,
                agent_id VARCHAR,
                title VARCHAR,
                status VARCHAR
            )
            """
        )
    )


def test_channel_columns_migration_is_idempotent(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _legacy_sessions_ddl(conn)

    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert {"channel", "external_conv_id", "channel_target_json"} <= columns

    # 重复执行不炸(列已存在、索引已存在)
    database._migrate_sqlite_skill_schema()

    index_names = {index["name"] for index in inspect(engine).get_indexes("sessions")}
    assert "uq_sessions_agent_channel_extconv" in index_names
    index_columns = next(
        index["column_names"]
        for index in inspect(engine).get_indexes("sessions")
        if index["name"] == "uq_sessions_agent_channel_extconv"
    )
    # 索引已重建为四列:agent/channel/channel_binding_id/external_conv_id(binding 隔离)
    assert index_columns == ["agent_id", "channel", "channel_binding_id", "external_conv_id"]

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sessions (id, tenant_id) VALUES ('s1', 't')"))
        conn.execute(text("INSERT INTO sessions (id, tenant_id) VALUES ('s2', 't')"))
    # NULL channel 的 web 会话不受唯一索引约束
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar_one()
        assert count == 2

        # 同 (agent, channel, binding, conv) 重复则违反唯一索引;binding 不同则放行
        conn.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, agent_id, channel, channel_binding_id, external_conv_id) "
                "VALUES ('c1', 't', 'a', 'wechat', 'chan_1', 'wechat_p2p_x')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, agent_id, channel, channel_binding_id, external_conv_id) "
                "VALUES ('c3', 't', 'a', 'wechat', 'chan_2', 'wechat_p2p_x')"
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO sessions (id, tenant_id, agent_id, channel, channel_binding_id, external_conv_id) "
                    "VALUES ('c2', 't', 'a', 'wechat', 'chan_1', 'wechat_p2p_x')"
                )
            )
