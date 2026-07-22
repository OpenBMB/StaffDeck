from pathlib import Path

from sqlalchemy import create_engine, text

from app import paths
from app.db.database import (
    _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID,
    _MODEL_API_PROTOCOLS_MIGRATION_ID,
    _migrate_default_model_output_limit,
    _migrate_model_api_protocols,
    _normalize_database_url,
)


def test_relative_sqlite_url_resolves_under_backend_dir() -> None:
    backend_dir = Path(__file__).resolve().parents[1]

    assert _normalize_database_url("sqlite:///./skill_agent_loop.db") == (
        f"sqlite:///{backend_dir / 'skill_agent_loop.db'}"
    )


def test_absolute_and_memory_sqlite_urls_are_preserved() -> None:
    assert _normalize_database_url("sqlite:////tmp/example.db") == "sqlite:////tmp/example.db"
    assert _normalize_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_frozen_relative_sqlite_resolves_under_user_data_dir(monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    # 与实现一致：_normalize_database_url 返回 .resolve() 后的路径，期望值同样 resolve
    expected = (paths.user_data_dir() / "skill_agent_loop.db").resolve()
    assert _normalize_database_url("sqlite:///./skill_agent_loop.db") == f"sqlite:///{expected}"


def test_frozen_sqlite_honors_data_dir_override(monkeypatch, tmp_path) -> None:
    # 直接断言 _normalize_database_url 返回值（不 importlib.reload 全局 engine）。
    # 期望值加 .resolve()：实现里有 .resolve()，Mac 上 /var→/private/var，
    # 且不依赖 pytest 版本对 tmp_path 是否预 resolve。
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    result = _normalize_database_url("sqlite:///./skill_agent_loop.db")
    expected = (tmp_path / "skill_agent_loop.db").resolve()
    assert result == f"sqlite:///{expected}"


def test_default_model_output_limit_migration_is_scoped_and_runs_once(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    is_default INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO model_configs (id, is_default, max_output_tokens)
                VALUES
                    ('default_legacy', 1, 2048),
                    ('default_custom', 1, 4096),
                    ('secondary_legacy', 0, 2048)
                """
            )
        )

        _migrate_default_model_output_limit(conn, {"model_configs"})

        rows = dict(
            conn.execute(
                text("SELECT id, max_output_tokens FROM model_configs ORDER BY id")
            ).all()
        )
        assert rows == {
            "default_custom": 4096,
            "default_legacy": 8192,
            "secondary_legacy": 2048,
        }
        assert conn.execute(
            text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
        ).scalar_one() == _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID

        conn.execute(
            text(
                "UPDATE model_configs SET max_output_tokens = 2048 "
                "WHERE id = 'default_legacy'"
            )
        )
        _migrate_default_model_output_limit(conn, {"model_configs"})

        assert conn.execute(
            text(
                "SELECT max_output_tokens FROM model_configs "
                "WHERE id = 'default_legacy'"
            )
        ).scalar_one() == 2048


def test_model_protocol_migration_preserves_legacy_chat_and_normalizes_defaults(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL,
                    extra_body_json JSON,
                    updated_at DATETIME
                )
                """
            )
        )
        insert = text(
            """
            INSERT INTO model_configs (
                id, tenant_id, enabled, is_default, extra_body_json, updated_at
            ) VALUES (:id, :tenant_id, :enabled, :is_default, :extra_body, :updated_at)
            """
        )
        conn.execute(
            insert,
            [
                {
                    "id": "older",
                    "tenant_id": "tenant_a",
                    "enabled": 1,
                    "is_default": 1,
                    "extra_body": '{"thinking":{"type":"disabled","clear_thinking":true}}',
                    "updated_at": "2026-01-01 00:00:00",
                },
                {
                    "id": "newer",
                    "tenant_id": "tenant_a",
                    "enabled": 1,
                    "is_default": 1,
                    "extra_body": '{"vendor_flag":true}',
                    "updated_at": "2026-02-01 00:00:00",
                },
                {
                    "id": "disabled",
                    "tenant_id": "tenant_b",
                    "enabled": 0,
                    "is_default": 1,
                    "extra_body": "{}",
                    "updated_at": "2026-03-01 00:00:00",
                },
            ],
        )

        _migrate_model_api_protocols(conn, {"model_configs"})

        rows = {
            row["id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT id, api_protocol, trust_status, is_default,
                           protocol_options_json, legacy_unmapped_options_json
                    FROM model_configs ORDER BY id
                    """
                )
            ).mappings()
        }
        assert rows["older"]["api_protocol"] == "openai_chat_completions"
        assert rows["older"]["trust_status"] == "legacy_trusted"
        assert rows["older"]["is_default"] == 0
        assert rows["newer"]["is_default"] == 1
        assert rows["disabled"]["trust_status"] == "unverified"
        assert rows["disabled"]["is_default"] == 0
        assert '"clear_thinking": true' in rows["older"]["protocol_options_json"]
        assert '"vendor_flag": true' in rows["newer"]["legacy_unmapped_options_json"]
        assert conn.execute(
            text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
        ).scalar_one() == _MODEL_API_PROTOCOLS_MIGRATION_ID

        conn.execute(text("UPDATE model_configs SET trust_status = 'verified' WHERE id = 'newer'"))
        _migrate_model_api_protocols(conn, {"model_configs"})
        assert conn.execute(
            text("SELECT trust_status FROM model_configs WHERE id = 'newer'")
        ).scalar_one() == "verified"


def test_model_protocol_migration_handles_table_without_extra_body(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oldest-models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO model_configs VALUES "
                "('legacy', 'tenant_a', 1, 1, '2026-01-01 00:00:00')"
            )
        )

        _migrate_model_api_protocols(conn, {"model_configs"})

        row = conn.execute(
            text(
                "SELECT extra_body_json, api_protocol, trust_status, is_default "
                "FROM model_configs WHERE id = 'legacy'"
            )
        ).one()
        assert row == ("{}", "openai_chat_completions", "legacy_trusted", 1)


def test_model_protocol_migration_rolls_back_all_changes_on_failure(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback-models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL,
                    extra_body_json JSON,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO model_configs VALUES "
                "('legacy', 'tenant_a', 1, 1, '{}', '2026-01-01 00:00:00')"
            )
        )

    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            _migrate_model_api_protocols(conn, {"model_configs"})
            raise RuntimeError("simulate startup failure")
    except RuntimeError:
        conn.rollback()

    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
        }
        assert "api_protocol" not in columns
        migration_table = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'app_data_migrations'"
            )
        ).first()
        assert migration_table is None


def test_model_protocol_migration_repairs_marker_with_incomplete_schema(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL,
                    extra_body_json JSON,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO model_configs
                    (id, tenant_id, enabled, is_default, extra_body_json, updated_at)
                VALUES ('legacy', 'tenant_a', 1, 1, '{}', '2026-01-01 00:00:00')
                """
            )
        )
        _migrate_model_api_protocols(conn, {"model_configs"})
        conn.execute(
            text(
                "UPDATE model_configs SET trust_status = 'verified', "
                "verified_fingerprint = 'keep-me' WHERE id = 'legacy'"
            )
        )
        conn.execute(text("DROP INDEX uq_model_configs_tenant_default"))
        conn.execute(text("ALTER TABLE model_configs DROP COLUMN protocol_options_json"))

    with engine.begin() as conn:
        _migrate_model_api_protocols(conn, {"model_configs"})
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
        }
        assert "protocol_options_json" in columns
        assert conn.execute(
            text(
                "SELECT trust_status, verified_fingerprint FROM model_configs "
                "WHERE id = 'legacy'"
            )
        ).one() == ("verified", "keep-me")
        assert conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_model_configs_tenant_default'"
            )
        ).scalar_one() == "uq_model_configs_tenant_default"
