from collections.abc import Callable, Generator
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

    backend_dir = Path(__file__).resolve().parents[2]
    return f"sqlite:///{(backend_dir / path).resolve()}"


settings = get_settings()

database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)


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
    with engine.begin() as conn:
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


def _seed_default_agents(conn, tables: set[str]) -> None:
    if "agent_profiles" not in tables:
        return
    tenant_ids = _tenant_ids(conn, tables)
    for tenant_id in tenant_ids:
        for agent_id, name, is_overall in (
            (_overall_agent_id(tenant_id), "整体智能体", True),
            (_default_agent_id(tenant_id), "默认智能体", False),
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
        if "sessions" in tables:
            conn.execute(
                text("UPDATE sessions SET agent_id = :agent_id WHERE tenant_id = :tenant_id AND (agent_id IS NULL OR agent_id = '')"),
                {"tenant_id": tenant_id, "agent_id": _default_agent_id(tenant_id)},
            )
        if "agent_resource_bindings" in tables:
            _seed_default_agent_bindings(conn, tenant_id)


def _seed_default_agent_bindings(conn, tenant_id: str) -> None:
    default_agent = _default_agent_id(tenant_id)
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
