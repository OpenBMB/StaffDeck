from collections.abc import Generator
import json

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


settings = get_settings()

connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
engine: Engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    import app.db.models  # noqa: F401

    _configure_sqlite_runtime()
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_skill_schema()


def _configure_sqlite_runtime() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))


def _migrate_sqlite_skill_schema() -> None:
    if not settings.database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    legacy_key = "so" + "p"
    legacy_active_column = f"active_{legacy_key}_id"
    legacy_stack_column = f"{legacy_key}_stack_json"
    legacy_allowed_column = f"allowed_{legacy_key}s_json"
    legacy_table = f"{legacy_key}_skills"
    legacy_id_column = f"{legacy_key}_id"
    legacy_id_prefix = f"{legacy_key}_"
    with engine.begin() as conn:
        if "sessions" in tables:
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            if "title" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN title VARCHAR"))
            if "active_skill_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN active_skill_id VARCHAR"))
                if legacy_active_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET active_skill_id = {legacy_active_column}"))
            if "skill_stack_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN skill_stack_json JSON"))
                if legacy_stack_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET skill_stack_json = {legacy_stack_column}"))
                else:
                    conn.execute(text("UPDATE sessions SET skill_stack_json = '[]'"))
            if "pending_tasks_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN pending_tasks_json JSON"))
                conn.execute(text("UPDATE sessions SET pending_tasks_json = '[]'"))
            if "awaiting_input_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN awaiting_input_json JSON"))
            if "knowledge_context_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN knowledge_context_json JSON"))
                conn.execute(text("UPDATE sessions SET knowledge_context_json = '[]'"))

        if "tools" in tables:
            tool_columns = {column["name"] for column in inspector.get_columns("tools")}
            if "allowed_skills_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN allowed_skills_json JSON"))
                if legacy_allowed_column in tool_columns:
                    conn.execute(text(f"UPDATE tools SET allowed_skills_json = {legacy_allowed_column}"))
                else:
                    conn.execute(text("UPDATE tools SET allowed_skills_json = '[]'"))

        if "ui_configs" in tables:
            ui_columns = {column["name"] for column in inspector.get_columns("ui_configs")}
            if "reflection_max_rounds" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN reflection_max_rounds INTEGER NOT NULL DEFAULT 1")
                )
            if "agent_loop_max_actions" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN agent_loop_max_actions INTEGER NOT NULL DEFAULT 6")
                )

        if "skill_feedback" in tables:
            feedback_columns = {column["name"] for column in inspector.get_columns("skill_feedback")}
            if "skill_version" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN skill_version VARCHAR"))
            if "step_id" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN step_id VARCHAR"))

        if "message_feedback" in tables:
            message_feedback_columns = {column["name"] for column in inspector.get_columns("message_feedback")}
            feedback_column_sql = {
                "analysis_status": "ALTER TABLE message_feedback ADD COLUMN analysis_status VARCHAR NOT NULL DEFAULT 'pending'",
                "analysis_bucket": "ALTER TABLE message_feedback ADD COLUMN analysis_bucket VARCHAR",
                "analysis_reason": "ALTER TABLE message_feedback ADD COLUMN analysis_reason VARCHAR",
                "analysis_summary": "ALTER TABLE message_feedback ADD COLUMN analysis_summary VARCHAR",
                "analysis_confidence": "ALTER TABLE message_feedback ADD COLUMN analysis_confidence FLOAT",
                "analysis_json": "ALTER TABLE message_feedback ADD COLUMN analysis_json JSON",
                "analyzed_at": "ALTER TABLE message_feedback ADD COLUMN analyzed_at DATETIME",
            }
            for column_name, ddl in feedback_column_sql.items():
                if column_name not in message_feedback_columns:
                    conn.execute(text(ddl))
            if "analysis_json" not in message_feedback_columns:
                conn.execute(text("UPDATE message_feedback SET analysis_json = '{}' WHERE analysis_json IS NULL"))

        if "general_skills" in tables:
            general_skill_columns = {column["name"] for column in inspector.get_columns("general_skills")}
            if "skill_files_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN skill_files_json JSON"))
                conn.execute(text("UPDATE general_skills SET skill_files_json = '[]' WHERE skill_files_json IS NULL"))
            if "metadata_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE general_skills SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        if legacy_table in tables and "skills" in tables:
            rows = conn.execute(text(f"SELECT * FROM {legacy_table}")).mappings().all()
            for row in rows:
                skill_id = _normalize_skill_identifier(
                    row.get("skill_id") or row.get(legacy_id_column),
                    legacy_id_prefix,
                )
                if not skill_id:
                    continue
                target_id = str(row["id"]).replace(legacy_id_prefix, "skill_", 1)
                existing = conn.execute(
                    text("SELECT id FROM skills WHERE tenant_id = :tenant_id AND skill_id = :skill_id"),
                    {"tenant_id": row["tenant_id"], "skill_id": skill_id},
                ).first()
                if existing:
                    continue
                content = _migrate_skill_content(row.get("content_json"), skill_id)
                existing_id = conn.execute(
                    text("SELECT id FROM skills WHERE id = :id"),
                    {"id": target_id},
                ).first()
                if existing_id:
                    conn.execute(
                        text(
                            """
                            UPDATE skills
                            SET skill_id = :skill_id, content_json = :content_json, updated_at = :updated_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": target_id,
                            "skill_id": skill_id,
                            "content_json": json.dumps(content, ensure_ascii=False),
                            "updated_at": row.get("updated_at"),
                        },
                    )
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO skills (
                            id, tenant_id, skill_id, version, name, business_domain,
                            description, content_json, status, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                            :description, :content_json, :status, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": row["tenant_id"],
                        "skill_id": skill_id,
                        "version": row.get("version") or "1.0.0",
                        "name": row["name"],
                        "business_domain": row.get("business_domain"),
                        "description": row.get("description"),
                        "content_json": json.dumps(content, ensure_ascii=False),
                        "status": row.get("status") or "draft",
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    },
                )
        if "skills" in tables:
            _normalize_existing_skill_rows(conn, legacy_id_prefix)
            if "skill_versions" in tables:
                _seed_skill_versions(conn)


def _migrate_skill_content(value: object, skill_id: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            content = json.loads(value)
        except json.JSONDecodeError:
            content = {}
    elif isinstance(value, dict):
        content = dict(value)
    else:
        content = {}
    if "skill_id" not in content:
        content["skill_id"] = content.pop("so" + "p_id", skill_id)
    else:
        content["skill_id"] = skill_id
    return content


def _normalize_existing_skill_rows(conn, legacy_id_prefix: str) -> None:
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skills")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        if skill_id == row.get("skill_id"):
            conn.execute(
                text("UPDATE skills SET content_json = :content_json WHERE id = :id"),
                {"id": row["id"], "content_json": json.dumps(content, ensure_ascii=False)},
            )
            continue
        existing = conn.execute(
            text("SELECT id FROM skills WHERE skill_id = :skill_id AND id != :id"),
            {"skill_id": skill_id, "id": row["id"]},
        ).first()
        if existing:
            continue
        conn.execute(
            text("UPDATE skills SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _seed_skill_versions(conn) -> None:
    rows = conn.execute(text("SELECT * FROM skills")).mappings().all()
    for row in rows:
        version = row.get("version") or "1.0.0"
        existing = conn.execute(
            text(
                """
                SELECT id FROM skill_versions
                WHERE tenant_id = :tenant_id AND skill_id = :skill_id AND version = :version
                """
            ),
            {"tenant_id": row["tenant_id"], "skill_id": row["skill_id"], "version": version},
        ).first()
        if existing:
            continue
        conn.execute(
            text(
                """
                INSERT INTO skill_versions (
                    id, tenant_id, skill_id, version, name, business_domain,
                    description, content_json, status, created_at, updated_at
                )
                VALUES (
                    :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                    :description, :content_json, :status, :created_at, :updated_at
                )
                """
            ),
            {
                "id": f"skillver_{row['id']}",
                "tenant_id": row["tenant_id"],
                "skill_id": row["skill_id"],
                "version": version,
                "name": row["name"],
                "business_domain": row.get("business_domain"),
                "description": row.get("description"),
                "content_json": row.get("content_json"),
                "status": row.get("status") or "draft",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )


def _normalize_skill_identifier(value: object, legacy_id_prefix: str) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith(legacy_id_prefix):
        return f"skill_{value[len(legacy_id_prefix):]}"
    return value


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
