from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import database


def _create_legacy_harness_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE harness_task_frames (
                    id VARCHAR PRIMARY KEY
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE harness_runs (
                    id VARCHAR PRIMARY KEY
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE harness_invocations (
                    id VARCHAR PRIMARY KEY
                )
                """
            )
        )
        conn.execute(text("INSERT INTO harness_task_frames (id) VALUES ('task_1')"))
        conn.execute(text("INSERT INTO harness_runs (id) VALUES ('run_1')"))
        conn.execute(
            text(
                "INSERT INTO harness_invocations (id) "
                "VALUES ('invocation_1'), ('invocation_2')"
            )
        )


def test_harness_v2_schema_migration_is_complete_and_idempotent(
    monkeypatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "harness-v2-migration.db"
    engine = create_engine(f"sqlite:///{db_path}")
    _create_legacy_harness_schema(engine)
    monkeypatch.setattr(database, "database_url", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    database._migrate_sqlite_skill_schema()

    inspector = inspect(engine)
    task_frame_columns = {
        column["name"] for column in inspector.get_columns("harness_task_frames")
    }
    assert {
        "decision",
        "agent_loop_id",
        "attempt_no",
        "lease_owner",
        "lease_expires_at",
    } <= task_frame_columns
    run_columns = {
        column["name"] for column in inspector.get_columns("harness_runs")
    }
    assert {
        "agent_loop_id",
        "attempt_no",
        "lease_owner",
        "lease_expires_at",
    } <= run_columns
    invocation_columns = {
        column["name"] for column in inspector.get_columns("harness_invocations")
    }
    assert {
        "logical_action_key",
        "replayed_from_invocation_id",
        "response_cache_json",
    } <= invocation_columns

    with engine.begin() as conn:
        task_defaults = conn.execute(
            text(
                "SELECT decision, attempt_no FROM harness_task_frames "
                "WHERE id = 'task_1'"
            )
        ).one()
        assert task_defaults == ("answer_only", 0)
        run_attempt = conn.execute(
            text("SELECT attempt_no FROM harness_runs WHERE id = 'run_1'")
        ).scalar_one()
        assert run_attempt == 1
        response_caches = conn.execute(
            text(
                "SELECT response_cache_json FROM harness_invocations "
                "ORDER BY id"
            )
        ).scalars()
        assert list(response_caches) == ["{}", "{}"]

    logical_action_index = next(
        index
        for index in inspector.get_indexes("harness_invocations")
        if index["name"] == "ix_harness_invocations_logical_action_key"
    )
    assert logical_action_index["unique"] == 1
    assert logical_action_index["column_names"] == ["logical_action_key"]

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO harness_invocations (id) "
                "VALUES ('invocation_3')"
            )
        )
        conn.execute(
            text(
                "UPDATE harness_invocations SET logical_action_key = 'action_1' "
                "WHERE id = 'invocation_1'"
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE harness_invocations "
                    "SET logical_action_key = 'action_1' "
                    "WHERE id = 'invocation_2'"
                )
            )
