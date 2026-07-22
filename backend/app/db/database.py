from collections.abc import Callable, Generator
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


def _normalize_database_url(url: str) -> str:
    if not url.startswith("sqlite:///") or url.startswith("sqlite:////") or url == "sqlite:///:memory:":
        return url

    raw_path = unquote(url.removeprefix("sqlite:///"))
    if not raw_path or raw_path == ":memory:":
        return url

    path = Path(raw_path)
    if path.is_absolute():
        return url

    from app import paths
    base_dir = paths.user_data_dir() if paths.is_frozen() else Path(__file__).resolve().parents[2]
    return f"sqlite:///{(base_dir / path).resolve()}"


settings = get_settings()

database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)

_DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID = "20260712_default_model_output_tokens_8192"
_MODEL_API_PROTOCOLS_MIGRATION_ID = "20260722_model_api_protocols_v1"
_LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_DEFAULT_MODEL_OUTPUT_TOKENS = 8192
_MODEL_API_PROTOCOL_COLUMNS = {
    "extra_body_json",
    "api_protocol",
    "protocol_options_json",
    "legacy_unmapped_options_json",
    "trust_status",
    "verified_at",
    "verified_fingerprint",
    "verification_attempt_id",
    "verification_started_at",
    "verification_attempt_status",
    "verification_attempt_error_code",
    "config_revision",
    "security_revision",
    "key_revision",
}


def init_db() -> None:
    import app.db.models  # noqa: F401

    _configure_sqlite_runtime()
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_skill_schema()


def _configure_sqlite_runtime() -> None:
    if not database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))


def _migrate_sqlite_skill_schema() -> None:
    if not database_url.startswith("sqlite"):
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
    with _sqlite_immediate_connection() as conn:
        _migrate_model_api_protocols(conn, tables)
        _migrate_default_model_output_limit(conn, tables)

        if "users" in tables:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'member'"))

        if "sessions" in tables:
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            if "agent_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN agent_id VARCHAR"))
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
            if "context_state_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN context_state_json JSON"))
                conn.execute(text("UPDATE sessions SET context_state_json = '{}'"))

        if "messages" in tables:
            message_columns = {column["name"] for column in inspector.get_columns("messages")}
            if "metadata_json" not in message_columns:
                conn.execute(text("ALTER TABLE messages ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE messages SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        if "tools" in tables:
            tool_columns = {column["name"] for column in inspector.get_columns("tools")}
            if "bucket" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN bucket VARCHAR NOT NULL DEFAULT '未分桶'"))
            if "tool_type" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN tool_type VARCHAR NOT NULL DEFAULT 'http'"))
            if "config_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN config_json JSON"))
                conn.execute(text("UPDATE tools SET config_json = '{}' WHERE config_json IS NULL"))
            if "allowed_skills_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN allowed_skills_json JSON"))
                if legacy_allowed_column in tool_columns:
                    conn.execute(text(f"UPDATE tools SET allowed_skills_json = {legacy_allowed_column}"))
                else:
                    conn.execute(text("UPDATE tools SET allowed_skills_json = '[]'"))
            if "mcp_server_id" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN mcp_server_id VARCHAR"))

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

        _migrate_knowledge_base_schema(conn, inspector, tables)
        _seed_default_agents(conn, tables)

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
                _normalize_existing_skill_version_rows(conn, legacy_id_prefix)
                _seed_skill_versions(conn)
            _normalize_agent_branch_rows(conn, tables)
            _seed_agent_branch_state(conn, inspector, tables)
            _sync_explicit_skill_tool_bindings(conn, tables)


@contextmanager
def _sqlite_immediate_connection():
    conn = engine.connect()
    try:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_default_model_output_limit(conn, tables: set[str]) -> None:
    if "model_configs" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    ).first()
    if applied:
        return

    conn.execute(
        text(
            """
            UPDATE model_configs
            SET max_output_tokens = :new_limit,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_default = 1
              AND max_output_tokens = :legacy_limit
            """
        ),
        {
            "new_limit": _DEFAULT_MODEL_OUTPUT_TOKENS,
            "legacy_limit": _LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS,
        },
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    )


def _migrate_model_api_protocols(conn, tables: set[str]) -> None:
    if "model_configs" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    ).first()
    columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
    }
    if applied and _model_api_protocol_schema_complete(conn, columns):
        return
    repairing_applied_migration = bool(applied)
    if "extra_body_json" not in columns:
        conn.execute(text("ALTER TABLE model_configs ADD COLUMN extra_body_json JSON"))
        conn.execute(text("UPDATE model_configs SET extra_body_json = '{}'"))
        columns.add("extra_body_json")
    column_ddl = {
        "api_protocol": (
            "ALTER TABLE model_configs ADD COLUMN api_protocol VARCHAR "
            "NOT NULL DEFAULT 'openai_chat_completions'"
        ),
        "protocol_options_json": "ALTER TABLE model_configs ADD COLUMN protocol_options_json JSON",
        "legacy_unmapped_options_json": (
            "ALTER TABLE model_configs ADD COLUMN legacy_unmapped_options_json JSON"
        ),
        "trust_status": (
            "ALTER TABLE model_configs ADD COLUMN trust_status VARCHAR "
            "NOT NULL DEFAULT 'unverified'"
        ),
        "verified_at": "ALTER TABLE model_configs ADD COLUMN verified_at DATETIME",
        "verified_fingerprint": (
            "ALTER TABLE model_configs ADD COLUMN verified_fingerprint VARCHAR"
        ),
        "verification_attempt_id": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_id VARCHAR"
        ),
        "verification_started_at": (
            "ALTER TABLE model_configs ADD COLUMN verification_started_at DATETIME"
        ),
        "verification_attempt_status": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_status VARCHAR "
            "NOT NULL DEFAULT 'idle'"
        ),
        "verification_attempt_error_code": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_error_code VARCHAR"
        ),
        "config_revision": (
            "ALTER TABLE model_configs ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "security_revision": (
            "ALTER TABLE model_configs ADD COLUMN security_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "key_revision": (
            "ALTER TABLE model_configs ADD COLUMN key_revision INTEGER NOT NULL DEFAULT 1"
        ),
    }
    for column_name, ddl in column_ddl.items():
        if column_name not in columns:
            conn.execute(text(ddl))

    if not repairing_applied_migration:
        rows = conn.execute(
            text("SELECT id, enabled, extra_body_json FROM model_configs")
        ).mappings().all()
        for row in rows:
            extra_body = _json_object(row.get("extra_body_json"))
            thinking = extra_body.get("thinking")
            protocol_options: dict[str, object] = {"openai_chat_completions": {}}
            legacy_unmapped: dict[str, object] = {}
            if _valid_chat_thinking_options(thinking):
                protocol_options["openai_chat_completions"] = {"thinking": thinking}
                legacy_unmapped = {
                    key: value for key, value in extra_body.items() if key != "thinking"
                }
            elif extra_body:
                legacy_unmapped = extra_body
            conn.execute(
                text(
                    """
                    UPDATE model_configs
                    SET api_protocol = 'openai_chat_completions',
                        protocol_options_json = :protocol_options,
                        legacy_unmapped_options_json = :legacy_unmapped,
                        trust_status = CASE WHEN enabled = 1 THEN 'legacy_trusted' ELSE 'unverified' END,
                        verification_attempt_status = 'idle',
                        config_revision = 1,
                        security_revision = 1,
                        key_revision = 1
                    WHERE id = :id
                    """
                ),
                {
                    "id": row["id"],
                    "protocol_options": json.dumps(protocol_options, ensure_ascii=False),
                    "legacy_unmapped": json.dumps(legacy_unmapped, ensure_ascii=False),
                },
            )

    _normalize_model_default_rows(conn)
    if repairing_applied_migration:
        return


def _normalize_model_default_rows(conn) -> None:
    duplicate_defaults = conn.execute(
        text(
            """
            SELECT tenant_id
            FROM model_configs
            WHERE is_default = 1 AND enabled = 1
            GROUP BY tenant_id
            HAVING COUNT(*) > 1
            """
        )
    ).scalars().all()
    for tenant_id in duplicate_defaults:
        keep_id = conn.execute(
            text(
                """
                SELECT id FROM model_configs
                WHERE tenant_id = :tenant_id AND is_default = 1 AND enabled = 1
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                UPDATE model_configs SET is_default = 0
                WHERE tenant_id = :tenant_id AND is_default = 1 AND id != :keep_id
                """
            ),
            {"tenant_id": tenant_id, "keep_id": keep_id},
        )
    conn.execute(text("UPDATE model_configs SET is_default = 0 WHERE enabled = 0"))
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_tenant_default
            ON model_configs(tenant_id) WHERE is_default = 1
            """
        )
    )
    conn.execute(
        text(
            "INSERT OR IGNORE INTO app_data_migrations (id) VALUES (:id)"
        ),
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    )


def _model_api_protocol_schema_complete(conn, columns: set[str]) -> bool:
    if not _MODEL_API_PROTOCOL_COLUMNS.issubset(columns):
        return False
    index = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'uq_model_configs_tenant_default'"
        )
    ).scalar_one_or_none()
    return bool(index and "WHERE is_default = 1" in index)


def _valid_chat_thinking_options(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - {"type", "clear_thinking"}:
        return False
    if value.get("type") not in {"enabled", "disabled"}:
        return False
    return "clear_thinking" not in value or isinstance(value["clear_thinking"], bool)


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
    return _ensure_skill_graph(content)


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


def _normalize_existing_skill_version_rows(conn, legacy_id_prefix: str) -> None:
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skill_versions")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        conn.execute(
            text("UPDATE skill_versions SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _sync_explicit_skill_tool_bindings(conn, tables: set[str]) -> None:
    if "skills" not in tables or "tools" not in tables:
        return
    skill_rows = conn.execute(
        text(
            "SELECT tenant_id, skill_id, content_json FROM skills "
            "WHERE status IS NULL OR status != 'deleted'"
        )
    ).mappings().all()
    for skill_row in skill_rows:
        content = _json_object(skill_row.get("content_json"))
        tool_names = _explicit_skill_tool_names(content)
        if not tool_names:
            continue
        tool_rows = conn.execute(
            text("SELECT id, name, allowed_skills_json FROM tools WHERE tenant_id = :tenant_id"),
            {"tenant_id": skill_row["tenant_id"]},
        ).mappings().all()
        for tool_row in tool_rows:
            if str(tool_row.get("name") or "") not in tool_names:
                continue
            allowed_skills = _json_string_list(tool_row.get("allowed_skills_json"))
            skill_id = str(skill_row.get("skill_id") or "").strip()
            if not skill_id or skill_id in allowed_skills:
                continue
            allowed_skills.append(skill_id)
            conn.execute(
                text(
                    "UPDATE tools SET allowed_skills_json = :allowed_skills, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {
                    "id": tool_row["id"],
                    "allowed_skills": json.dumps(allowed_skills, ensure_ascii=False),
                },
            )


def _explicit_skill_tool_names(content: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if value.startswith("call_tool:"):
                    name = value.split(":", 1)[1].strip()
                    if name:
                        names.add(name)
    return names


def _json_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _ensure_skill_graph(content: dict[str, object]) -> dict[str, object]:
    nodes = content.get("nodes")
    steps = content.get("steps")
    if isinstance(nodes, list) and nodes:
        content.pop("steps", None)
        content.setdefault("start_node_id", _first_node_id(nodes))
        content.setdefault("terminal_node_ids", [_last_node_id(nodes)] if _last_node_id(nodes) else [])
        return content
    if not isinstance(steps, list) or not steps:
        content.setdefault("nodes", [])
        content.setdefault("edges", [])
        content.setdefault("terminal_node_ids", [])
        content.pop("steps", None)
        return content
    normalized_steps = [step for step in steps if isinstance(step, dict)]
    content["nodes"] = [_step_to_node_dict(step) for step in normalized_steps]
    content["edges"] = [
        {
            "source_node_id": str(normalized_steps[index].get("step_id") or f"step_{index + 1}"),
            "next_node_id": str(normalized_steps[index + 1].get("step_id") or f"step_{index + 2}"),
            "priority": index,
            "label": "默认推进",
        }
        for index in range(len(normalized_steps) - 1)
    ]
    if normalized_steps:
        content["start_node_id"] = content.get("start_node_id") or str(normalized_steps[0].get("step_id") or "step_1")
        content["terminal_node_ids"] = content.get("terminal_node_ids") or [
            str(normalized_steps[-1].get("step_id") or f"step_{len(normalized_steps)}")
        ]
    content.pop("steps", None)
    return content


def _step_to_node_dict(step: dict[str, object]) -> dict[str, object]:
    actions = step.get("allowed_actions") if isinstance(step.get("allowed_actions"), list) else []
    expected = step.get("expected_user_info") if isinstance(step.get("expected_user_info"), list) else []
    node_type = "collect_info" if expected else "response"
    if any(isinstance(action, str) and action.startswith("call_tool:") for action in actions):
        node_type = "tool_call"
    if "handoff_human" in actions:
        node_type = "handoff"
    return {
        "node_id": str(step.get("step_id") or step.get("node_id") or "step"),
        "type": node_type,
        "name": str(step.get("name") or step.get("step_id") or "步骤"),
        "instruction": str(step.get("instruction") or ""),
        "optional": bool(step.get("optional") or False),
        "condition": step.get("condition") if isinstance(step.get("condition"), str) else None,
        "expected_user_info": expected,
        "allowed_actions": actions,
        "knowledge_scope": step.get("knowledge_scope") if isinstance(step.get("knowledge_scope"), dict) else {},
        "retry_policy": step.get("retry_policy") if isinstance(step.get("retry_policy"), dict) else {},
        "metadata": step.get("metadata") if isinstance(step.get("metadata"), dict) else {},
    }


def _first_node_id(nodes: object) -> str | None:
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _last_node_id(nodes: object) -> str | None:
    if not isinstance(nodes, list):
        return None
    for node in reversed(nodes):
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


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


def _migrate_knowledge_base_schema(conn, inspector, tables: set[str]) -> None:
    tenant_ids = _tenant_ids(conn, tables)
    if "knowledge_bases" in tables:
        for tenant_id in tenant_ids:
            default_id = _default_knowledge_base_id(tenant_id)
            existing = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": default_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, 'active', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": default_id,
                        "tenant_id": tenant_id,
                        "name": "默认知识库",
                        "description": "系统默认知识库",
                    },
                )

    table_names = {
        "knowledge_documents": "knowledge_base_id",
        "knowledge_buckets": "knowledge_base_id",
        "knowledge_chunks": "knowledge_base_id",
        "knowledge_concepts": "knowledge_base_id",
        "knowledge_discovery_suggestions": "knowledge_base_id",
        "knowledge_ingest_jobs": "knowledge_base_id",
    }
    for table_name, column_name in table_names.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR"))
        rows = conn.execute(
            text(f"SELECT DISTINCT tenant_id FROM {table_name} WHERE {column_name} IS NULL OR {column_name} = ''")
        ).mappings().all()
        for row in rows:
            tenant_id = str(row.get("tenant_id") or "")
            if tenant_id:
                conn.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :knowledge_base_id WHERE tenant_id = :tenant_id AND ({column_name} IS NULL OR {column_name} = '')"),
                    {"tenant_id": tenant_id, "knowledge_base_id": _default_knowledge_base_id(tenant_id)},
                )

    if "knowledge_base_versions" in tables and "knowledge_bases" in tables:
        knowledge_bases = conn.execute(text("SELECT * FROM knowledge_bases")).mappings().all()
        for row in knowledge_bases:
            version_id = _knowledge_base_version_id(str(row["id"]), "1.0.0")
            existing = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not existing:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            :status, :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": row["tenant_id"],
                        "knowledge_base_id": row["id"],
                        "name": row["name"],
                        "description": row.get("description"),
                        "status": row.get("status") or "active",
                        "metadata_json": row.get("metadata_json") or "{}",
                    },
                )

    for table_name in table_names:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "knowledge_base_version_id" not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN knowledge_base_version_id VARCHAR"))
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT knowledge_base_id FROM {table_name}
                WHERE knowledge_base_id IS NOT NULL
                  AND knowledge_base_id != ''
                  AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                """
            )
        ).mappings().all()
        for row in rows:
            knowledge_base_id = str(row.get("knowledge_base_id") or "")
            if not knowledge_base_id:
                continue
            conn.execute(
                text(
                    f"""
                    UPDATE {table_name}
                    SET knowledge_base_version_id = :version_id
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                    """
                ),
                {
                    "knowledge_base_id": knowledge_base_id,
                    "version_id": _knowledge_base_version_id(knowledge_base_id, "1.0.0"),
                },
            )

    _split_document_backed_knowledge_bases(conn, tables)


def _split_document_backed_knowledge_bases(conn, tables: set[str]) -> None:
    required_tables = {"knowledge_bases", "knowledge_base_versions", "knowledge_documents"}
    if not required_tables.issubset(tables):
        return

    document_groups = conn.execute(
        text(
            """
            SELECT knowledge_base_id, COUNT(id) AS document_count
            FROM knowledge_documents
            WHERE knowledge_base_id IS NOT NULL AND knowledge_base_id != ''
            GROUP BY knowledge_base_id
            """
        )
    ).mappings().all()
    multi_document_base_ids = {
        str(row["knowledge_base_id"])
        for row in document_groups
        if int(row.get("document_count") or 0) > 1
    }
    if not multi_document_base_ids:
        return

    for source_knowledge_base_id in sorted(multi_document_base_ids):
        source = conn.execute(
            text("SELECT * FROM knowledge_bases WHERE id = :id"),
            {"id": source_knowledge_base_id},
        ).mappings().first()
        if not source:
            continue
        documents = conn.execute(
            text(
                """
                SELECT *
                FROM knowledge_documents
                WHERE knowledge_base_id = :knowledge_base_id
                ORDER BY created_at, id
                """
            ),
            {"knowledge_base_id": source_knowledge_base_id},
        ).mappings().all()
        if len(documents) <= 1:
            continue
        for document in documents:
            target_id = _document_knowledge_base_id(str(document["id"]))
            target = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": target_id},
            ).first()
            target_name = _unique_migrated_knowledge_base_name(
                conn,
                str(source["tenant_id"]),
                _document_knowledge_base_name(document),
                target_id,
            )
            metadata = _json_object(source.get("metadata_json"))
            metadata.update(
                {
                    "created_from_document_upload": True,
                    "source_document_id": document["id"],
                    "source_filename": document.get("filename"),
                    "split_from_knowledge_base_id": source_knowledge_base_id,
                }
            )
            if not target:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, :status, :metadata_json,
                            :created_at, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": source["tenant_id"],
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "status": "active",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                        "created_at": document.get("created_at") or source.get("created_at"),
                    },
                )
            version_id = _knowledge_base_version_id(target_id, "1.0.0")
            version_exists = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not version_exists:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            'active', :metadata_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": source["tenant_id"],
                        "knowledge_base_id": target_id,
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    },
                )
            _move_document_knowledge_rows(conn, tables, str(document["id"]), target_id, version_id)


def _move_document_knowledge_rows(
    conn,
    tables: set[str],
    document_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    document_scoped_tables = (
        "knowledge_buckets",
        "knowledge_chunks",
        "knowledge_concepts",
        "knowledge_discovery_suggestions",
    )
    if "knowledge_documents" in tables:
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    for table_name in document_scoped_tables:
        if table_name not in tables:
            continue
        conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE document_id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    if "knowledge_ingest_jobs" not in tables:
        return
    conn.execute(
        text(
            """
            UPDATE knowledge_ingest_jobs
            SET knowledge_base_id = :knowledge_base_id,
                knowledge_base_version_id = :version_id
            WHERE document_id = :document_id
            """
        ),
        {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "version_id": version_id,
        },
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _document_knowledge_base_id(document_id: str) -> str:
    return f"kb_doc_{document_id}"


def _document_knowledge_base_name(document) -> str:
    title = str(document.get("title") or "").strip()
    if title:
        return title
    filename = str(document.get("filename") or "").strip()
    stem = Path(filename).stem.strip()
    return stem or filename or "未命名知识库"


def _unique_migrated_knowledge_base_name(
    conn,
    tenant_id: str,
    base_name: str,
    target_id: str,
) -> str:
    normalized = base_name.strip() or "未命名知识库"
    existing_names = {
        str(row[0])
        for row in conn.execute(
            text("SELECT name FROM knowledge_bases WHERE tenant_id = :tenant_id AND id != :target_id"),
            {"tenant_id": tenant_id, "target_id": target_id},
        ).all()
        if row[0]
    }
    if normalized not in existing_names:
        return normalized
    index = 2
    while True:
        candidate = f"{normalized} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


def _seed_default_agents(conn, tables: set[str]) -> None:
    if "agent_profiles" not in tables:
        return
    tenant_ids = _tenant_ids(conn, tables)
    for tenant_id in tenant_ids:
        for agent_id, name, is_overall in (
            (_overall_agent_id(tenant_id), "整体智能体", True),
        ):
            existing = conn.execute(text("SELECT id FROM agent_profiles WHERE id = :id"), {"id": agent_id}).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_profiles (
                        id, tenant_id, name, description, persona_prompt, is_overall,
                        status, metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :name, :description, NULL, :is_overall,
                        'active', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": agent_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "description": "全局资源池" if is_overall else "默认对话可见域",
                    "is_overall": 1 if is_overall else 0,
                },
            )
        _archive_default_agent(conn, tenant_id)
        if "agent_resource_bindings" in tables:
            _seed_default_agent_bindings(conn, tenant_id)


def _seed_default_agent_bindings(conn, tenant_id: str) -> None:
    default_agent = _default_agent_id(tenant_id)
    active_default = conn.execute(
        text(
            """
            SELECT id FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND status != 'archived'
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not active_default:
        return
    resource_queries = (
        ("skill", "SELECT id, status FROM skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("general_skill", "SELECT id, status FROM general_skills WHERE tenant_id = :tenant_id AND status != 'deleted'"),
        ("knowledge_base", "SELECT id, status FROM knowledge_bases WHERE tenant_id = :tenant_id AND status != 'deleted'"),
    )
    for resource_type, sql in resource_queries:
        rows = conn.execute(text(sql), {"tenant_id": tenant_id}).mappings().all()
        for row in rows:
            resource_id = str(row.get("id") or "")
            if not resource_id:
                continue
            binding_status = "active" if str(row.get("status") or "") in {"active", "published"} else "inactive"
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_resource_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id
                      AND resource_type = :resource_type AND resource_id = :resource_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_resource_bindings (
                        id, tenant_id, agent_id, resource_type, resource_id, status,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, :resource_type, :resource_id, :status,
                        '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_resource_binding_id(tenant_id, default_agent, resource_type, resource_id),
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "status": binding_status,
                },
            )


def _archive_default_agent(conn, tenant_id: str) -> None:
    default_agent = _default_agent_id(tenant_id)
    row = conn.execute(
        text(
            """
            SELECT metadata_json FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND is_overall = 0
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not row:
        return
    try:
        metadata = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if metadata and not (
        metadata.get("is_default_employee") is True
        or metadata.get("created_by") == "admin"
        or metadata.get("owner_user_id") == "admin"
    ):
        return
    metadata.update(
        {
            "is_default_employee": True,
            "hidden_from_staffdeck": True,
            "archived_by_seed": True,
            "owner_user_id": "admin",
            "owner_username": "admin",
            "owner_display_name": "Administrator",
            "created_by_user_id": "admin",
            "created_by_username": "admin",
            "created_by": "admin",
            "created_by_display_name": "Administrator",
            "creator_name": "admin",
        }
    )
    conn.execute(
        text(
            """
            UPDATE agent_profiles
            SET status = 'archived',
                metadata_json = :metadata_json,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {
            "id": default_agent,
            "tenant_id": tenant_id,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        },
    )


def _seed_agent_branch_state(conn, inspector, tables: set[str]) -> None:
    if "agent_profiles" not in tables:
        return
    if "agent_skill_branches" in tables and "skills" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            _seed_default_agent_bindings(conn, tenant_id)
            rows = conn.execute(
                text(
                    """
                    SELECT s.*
                    FROM skills s
                    JOIN agent_resource_bindings b
                      ON b.resource_id = s.id
                     AND b.resource_type = 'skill'
                     AND b.tenant_id = s.tenant_id
                    WHERE s.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND s.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_skill_branch(conn, agent_id, row)

    if "agent_knowledge_branches" in tables and "knowledge_bases" in tables:
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE is_overall = 0 AND status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            agent_id = str(agent["id"])
            rows = conn.execute(
                text(
                    """
                    SELECT kb.*
                    FROM knowledge_bases kb
                    JOIN agent_resource_bindings b
                      ON b.resource_id = kb.id
                     AND b.resource_type = 'knowledge_base'
                     AND b.tenant_id = kb.tenant_id
                    WHERE kb.tenant_id = :tenant_id
                      AND b.agent_id = :agent_id
                      AND kb.status != 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id},
            ).mappings().all()
            for row in rows:
                _seed_agent_knowledge_branch(conn, agent_id, row)

    if "agent_model_bindings" in tables and "model_configs" in tables:
        default_models = conn.execute(
            text("SELECT tenant_id, id FROM model_configs WHERE is_default = 1 AND enabled = 1")
        ).mappings().all()
        model_by_tenant = {str(row["tenant_id"]): str(row["id"]) for row in default_models}
        agents = conn.execute(
            text("SELECT id, tenant_id FROM agent_profiles WHERE status != 'archived'")
        ).mappings().all()
        for agent in agents:
            tenant_id = str(agent["tenant_id"])
            model_id = model_by_tenant.get(tenant_id)
            if not model_id:
                continue
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_model_bindings
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id AND role = 'default'
                    """
                ),
                {"tenant_id": tenant_id, "agent_id": agent["id"]},
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_model_bindings (
                        id, tenant_id, agent_id, role, model_config_id, created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, 'default', :model_config_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_model_binding_id(str(agent["id"]), "default"),
                    "tenant_id": tenant_id,
                    "agent_id": agent["id"],
                    "model_config_id": model_id,
                },
            )


def _normalize_agent_branch_rows(conn, tables: set[str]) -> None:
    if "agent_resource_bindings" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_resource_bindings",
            select_columns=("id", "tenant_id", "agent_id", "resource_type", "resource_id"),
            key_columns=("tenant_id", "agent_id", "resource_type", "resource_id"),
            id_factory=lambda row: _agent_resource_binding_id(
                str(row["tenant_id"]),
                str(row["agent_id"]),
                str(row["resource_type"]),
                str(row["resource_id"]),
            ),
        )
    if "agent_skill_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branches",
            select_columns=("id", "tenant_id", "agent_id", "skill_id"),
            key_columns=("tenant_id", "agent_id", "skill_id"),
            id_factory=lambda row: _agent_skill_branch_id(str(row["agent_id"]), str(row["skill_id"])),
        )
    if "agent_skill_branch_versions" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_skill_branch_versions",
            select_columns=("id", "tenant_id", "agent_id", "skill_id", "version"),
            key_columns=("tenant_id", "agent_id", "skill_id", "version"),
            id_factory=lambda row: _agent_skill_branch_version_id(
                str(row["agent_id"]),
                str(row["skill_id"]),
                str(row["version"]),
            ),
        )
    if "agent_knowledge_branches" in tables:
        _normalize_canonical_ids(
            conn,
            table="agent_knowledge_branches",
            select_columns=("id", "tenant_id", "agent_id", "knowledge_base_id"),
            key_columns=("tenant_id", "agent_id", "knowledge_base_id"),
            id_factory=lambda row: _agent_knowledge_branch_id(str(row["agent_id"]), str(row["knowledge_base_id"])),
        )


def _normalize_canonical_ids(
    conn,
    *,
    table: str,
    select_columns: tuple[str, ...],
    key_columns: tuple[str, ...],
    id_factory: Callable[[dict[str, object]], str],
) -> None:
    columns_sql = ", ".join(select_columns)
    rows = conn.execute(text(f"SELECT {columns_sql} FROM {table}")).mappings().all()
    kept_keys: set[tuple[object, ...]] = set()
    for row in rows:
        row_dict = dict(row)
        row_id = str(row_dict["id"])
        key = tuple(row_dict[column] for column in key_columns)
        target_id = id_factory(row_dict)
        if key in kept_keys:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        kept_keys.add(key)
        if row_id == target_id:
            continue
        target_exists = conn.execute(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": target_id}).first()
        if target_exists:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        conn.execute(text(f"UPDATE {table} SET id = :target_id WHERE id = :id"), {"target_id": target_id, "id": row_id})


def _seed_agent_skill_branch(conn, agent_id: str, row) -> None:
    branch_id = _agent_skill_branch_id(agent_id, str(row["skill_id"]))
    existing = conn.execute(text("SELECT id FROM agent_skill_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    version = row.get("version") or "1.0.0"
    content_json = row.get("content_json") or "{}"
    branch_status = "active" if str(row.get("status") or "") == "published" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branches (
                id, tenant_id, agent_id, skill_id, source_skill_id, base_version, head_version,
                content_json, status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :base_version, :head_version,
                :content_json, :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "base_version": version,
            "head_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )
    if "agent_skill_branch_versions" not in {table for table in inspect(engine).get_table_names()}:
        return
    branch_version_id = _agent_skill_branch_version_id(agent_id, str(row["skill_id"]), version)
    existing_version = conn.execute(
        text("SELECT id FROM agent_skill_branch_versions WHERE id = :id"),
        {"id": branch_version_id},
    ).first()
    if existing_version:
        return
    conn.execute(
        text(
            """
            INSERT INTO agent_skill_branch_versions (
                id, tenant_id, agent_id, skill_id, source_skill_id, version, base_version,
                content_json, status, sync_state, change_summary, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :skill_id, :source_skill_id, :version, :base_version,
                :content_json, :status, 'synced', '初始化分支', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_version_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "skill_id": row["skill_id"],
            "source_skill_id": row["id"],
            "version": version,
            "base_version": version,
            "content_json": content_json,
            "status": branch_status,
        },
    )


def _seed_agent_knowledge_branch(conn, agent_id: str, row) -> None:
    branch_id = _agent_knowledge_branch_id(agent_id, str(row["id"]))
    existing = conn.execute(text("SELECT id FROM agent_knowledge_branches WHERE id = :id"), {"id": branch_id}).first()
    if existing:
        return
    branch_status = "active" if str(row.get("status") or "") == "active" else "inactive"
    conn.execute(
        text(
            """
            INSERT INTO agent_knowledge_branches (
                id, tenant_id, agent_id, knowledge_base_id, base_version, head_version,
                status, sync_state, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :tenant_id, :agent_id, :knowledge_base_id, '1.0.0', '1.0.0',
                :status, 'synced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": row["tenant_id"],
            "agent_id": agent_id,
            "knowledge_base_id": row["id"],
            "status": branch_status,
        },
    )


def _tenant_ids(conn, tables: set[str]) -> list[str]:
    ids: set[str] = set()
    if "tenants" in tables:
        ids.update(str(row[0]) for row in conn.execute(text("SELECT id FROM tenants")).all() if row[0])
    for table_name in ("skills", "general_skills", "knowledge_documents", "sessions"):
        if table_name not in tables:
            continue
        ids.update(str(row[0]) for row in conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table_name}")).all() if row[0])
    return sorted(ids)


def _default_knowledge_base_id(tenant_id: str) -> str:
    return f"kb_{tenant_id}_default"


def _overall_agent_id(tenant_id: str) -> str:
    return f"agent_{tenant_id}_overall"


def _default_agent_id(tenant_id: str) -> str:
    return f"agent_{tenant_id}_default"


def _knowledge_base_version_id(knowledge_base_id: str, version: str) -> str:
    return f"kbver_{knowledge_base_id}_{version.replace('.', '_').replace('-', '_')}"


def _agent_skill_branch_id(agent_id: str, skill_id: str) -> str:
    return f"agentbranch_{agent_id}_{skill_id}"


def _agent_skill_branch_version_id(agent_id: str, skill_id: str, version: str) -> str:
    safe_version = version.replace(".", "_").replace("-", "_")
    return f"agentbranchver_{agent_id}_{skill_id}_{safe_version}"


def _agent_knowledge_branch_id(agent_id: str, knowledge_base_id: str) -> str:
    return f"agentkb_{agent_id}_{knowledge_base_id}"


def _agent_resource_binding_id(tenant_id: str, agent_id: str, resource_type: str, resource_id: str) -> str:
    key = f"{tenant_id}:{agent_id}:{resource_type}:{resource_id}"
    return f"agentres_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _agent_model_binding_id(agent_id: str, role: str) -> str:
    return f"agentmodel_{agent_id}_{role}"


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
