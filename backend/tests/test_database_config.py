from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel

from app import paths
from app.agents.branching import visible_knowledge_base_versions
from app.db import database
from app.db.database import (
    _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID,
    _MODEL_API_PROTOCOLS_MIGRATION_ID,
    _migrate_default_model_output_limit,
    _migrate_knowledge_base_schema,
    _migrate_model_api_protocols,
    _normalize_database_url,
)
from app.db.models import KnowledgeBase, KnowledgeBaseVersion, KnowledgeDocument, Tenant


def test_sqlite_startup_migration_adds_display_name_login_index(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-users.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    username VARCHAR NOT NULL,
                    display_name VARCHAR,
                    role VARCHAR NOT NULL DEFAULT 'member',
                    source VARCHAR NOT NULL DEFAULT 'web',
                    password_hash VARCHAR NOT NULL
                )
                """
            )
        )

    monkeypatch.setattr(database, "engine", engine)

    database._migrate_sqlite_skill_schema()
    database._migrate_sqlite_skill_schema()

    display_name_indexes = [
        index
        for index in inspect(engine).get_indexes("users")
        if index["name"] == "ix_users_tenant_id_display_name"
    ]
    assert display_name_indexes == [
        {
            "name": "ix_users_tenant_id_display_name",
            "column_names": ["tenant_id", "display_name"],
            "unique": 0,
            "dialect_options": {},
        }
    ]


def test_knowledge_base_migration_accepts_existing_noncanonical_version_id(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge-version.db'}")
    child_tables = (
        "knowledge_documents",
        "knowledge_buckets",
        "knowledge_chunks",
        "knowledge_concepts",
        "knowledge_discovery_suggestions",
        "knowledge_ingest_jobs",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_bases (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_base_versions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR NOT NULL,
                    version VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, knowledge_base_id, version)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_bases
                    (id, tenant_id, name, status, capability_scope, metadata_json)
                VALUES ('kb_preset_sales_001', 'tenant_demo', 'Sales', 'active', 'general', '{}')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_base_versions
                    (id, tenant_id, knowledge_base_id, version, name, status,
                     capability_scope, metadata_json)
                VALUES (
                    'legacy-version-id', 'tenant_demo', 'kb_preset_sales_001',
                    '1.0.0', 'Sales', 'active', 'general', '{}'
                )
                """
            )
        )
        for table_name in child_tables:
            conn.execute(
                text(
                    f"""
                    CREATE TABLE {table_name} (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR NOT NULL,
                        knowledge_base_id VARCHAR,
                        knowledge_base_version_id VARCHAR
                    )
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO {table_name}
                        (id, tenant_id, knowledge_base_id, knowledge_base_version_id)
                    VALUES (:id, 'tenant_demo', 'kb_preset_sales_001', NULL)
                    """
                ),
                {"id": f"{table_name}-row"},
            )

        tables = {"knowledge_bases", "knowledge_base_versions", *child_tables}
        _migrate_knowledge_base_schema(conn, inspect(conn), tables)
        _migrate_knowledge_base_schema(conn, inspect(conn), tables)

        versions = conn.execute(
            text(
                "SELECT id FROM knowledge_base_versions "
                "WHERE knowledge_base_id = 'kb_preset_sales_001' AND version = '1.0.0'"
            )
        ).scalars().all()
        assert versions == ["legacy-version-id"]
        for table_name in child_tables:
            assert conn.execute(
                text(f"SELECT knowledge_base_version_id FROM {table_name}")
            ).scalar_one() == "legacy-version-id"


def test_knowledge_base_migration_creates_dedicated_default_in_current_schema(
    tmp_path,
) -> None:
    """Fresh current schemas must receive a default KB with every required column."""
    engine = create_engine(f"sqlite:///{tmp_path / 'current-schema.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_current", name="Current schema"))
        db.commit()

    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        _migrate_knowledge_base_schema(conn, inspect(conn), tables)
        row = conn.execute(
            text(
                "SELECT mode, published_version_id FROM knowledge_bases "
                "WHERE id = 'kb_tenant_current_default'"
            )
        ).one()

    assert row.mode == "dedicated"
    assert row.published_version_id is None


def test_document_split_migration_sets_current_required_knowledge_fields(
    tmp_path,
) -> None:
    """当前结构拆分旧多文档知识库时，新根记录与版本必须保持专用和已发布语义。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'document-split-current.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_split", name="Document split"))
        db.add(
            KnowledgeBase(
                id="kb_split_source",
                tenant_id="tenant_split",
                name="旧多文档知识库",
                mode="dedicated",
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_split_source",
                tenant_id="tenant_split",
                knowledge_base_id="kb_split_source",
                version="1.0.0",
                name="旧多文档知识库",
                publication_state="released",
            )
        )
        db.add_all(
            [
                KnowledgeDocument(
                    id="kdoc_split_a",
                    tenant_id="tenant_split",
                    knowledge_base_id="kb_split_source",
                    knowledge_base_version_id="kbver_split_source",
                    filename="a.md",
                    file_type="md",
                    status="ready",
                ),
                KnowledgeDocument(
                    id="kdoc_split_b",
                    tenant_id="tenant_split",
                    knowledge_base_id="kb_split_source",
                    knowledge_base_version_id="kbver_split_source",
                    filename="b.md",
                    file_type="md",
                    status="ready",
                ),
            ]
        )
        db.commit()

    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        _migrate_knowledge_base_schema(conn, inspect(conn), tables)
        migrated = conn.execute(
            text(
                """
                SELECT kb.mode, kb.published_version_id, kbv.publication_state
                FROM knowledge_bases AS kb
                JOIN knowledge_base_versions AS kbv ON kbv.knowledge_base_id = kb.id
                WHERE kb.id IN ('kb_doc_kdoc_split_a', 'kb_doc_kdoc_split_b')
                ORDER BY kb.id
                """
            )
        ).all()

    assert migrated == [
        ("dedicated", None, "released"),
        ("dedicated", None, "released"),
    ]


def test_document_split_migration_leaves_shared_multi_document_base_intact(
    tmp_path,
) -> None:
    """共享正式版本允许多个文档，旧数据拆分迁移不得改变其根记录或文档归属。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'document-split-shared.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_shared_split", name="Shared document split"))
        db.add(
            KnowledgeBase(
                id="kb_shared_multi_document",
                tenant_id="tenant_shared_split",
                name="共享多文档知识库",
                mode="shared",
                published_version_id="kbver_shared_multi_document",
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_shared_multi_document",
                tenant_id="tenant_shared_split",
                knowledge_base_id="kb_shared_multi_document",
                version="1.0.0",
                name="共享多文档知识库",
                publication_state="released",
            )
        )
        db.add_all(
            [
                KnowledgeDocument(
                    id="kdoc_shared_a",
                    tenant_id="tenant_shared_split",
                    knowledge_base_id="kb_shared_multi_document",
                    knowledge_base_version_id="kbver_shared_multi_document",
                    filename="shared-a.md",
                    file_type="md",
                    status="ready",
                ),
                KnowledgeDocument(
                    id="kdoc_shared_b",
                    tenant_id="tenant_shared_split",
                    knowledge_base_id="kb_shared_multi_document",
                    knowledge_base_version_id="kbver_shared_multi_document",
                    filename="shared-b.md",
                    file_type="md",
                    status="ready",
                ),
            ]
        )
        db.commit()

    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        _migrate_knowledge_base_schema(conn, inspect(conn), tables)
        document_base_ids = conn.execute(
            text(
                """
                SELECT knowledge_base_id
                FROM knowledge_documents
                WHERE id IN ('kdoc_shared_a', 'kdoc_shared_b')
                ORDER BY id
                """
            )
        ).scalars().all()
        split_target_count = conn.execute(
            text("SELECT COUNT(*) FROM knowledge_bases WHERE id LIKE 'kb_doc_kdoc_shared_%'")
        ).scalar_one()

    assert document_base_ids == [
        "kb_shared_multi_document",
        "kb_shared_multi_document",
    ]
    assert split_target_count == 0


def test_shared_knowledge_migration_preserves_private_data_and_is_idempotent(
    tmp_path,
) -> None:
    """旧库迁移两次后仍保持专用语义，并原样保留员工分支与资源绑定。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'shared-knowledge.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_bases (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_base_versions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR NOT NULL,
                    version VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, knowledge_base_id, version)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE teams (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    owner_user_id VARCHAR NOT NULL,
                    config_json JSON,
                    status VARCHAR NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_knowledge_branches (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR NOT NULL,
                    base_version VARCHAR NOT NULL,
                    head_version VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    sync_state VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_resource_bindings (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_bases
                    (id, tenant_id, name, status, capability_scope, metadata_json)
                VALUES ('kb_private', 'tenant_demo', '私有资料', 'active', 'general', '{}')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_base_versions
                    (id, tenant_id, knowledge_base_id, version, name, status,
                     capability_scope, metadata_json)
                VALUES (
                    'kbver_private', 'tenant_demo', 'kb_private', '1.0.0',
                    '私有资料', 'active', 'general', '{}'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO teams
                    (id, tenant_id, name, owner_user_id, config_json, status)
                VALUES ('team_a', 'tenant_demo', '项目 A', 'user_admin', '{}', 'active')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_knowledge_branches
                    (id, tenant_id, agent_id, knowledge_base_id, base_version,
                     head_version, status, sync_state, metadata_json)
                VALUES (
                    'branch_private', 'tenant_demo', 'agent_writer', 'kb_private',
                    '1.0.0', '1.0.0', 'active', 'synced', '{"owner": "agent_writer"}'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_resource_bindings
                    (id, tenant_id, agent_id, resource_type, resource_id, status, metadata_json)
                VALUES (
                    'binding_private', 'tenant_demo', 'agent_writer', 'knowledge',
                    'kb_private', 'active', '{"scope": "private"}'
                )
                """
            )
        )

    for _ in range(2):
        with engine.begin() as conn:
            current_inspector = inspect(conn)
            _migrate_knowledge_base_schema(
                conn,
                current_inspector,
                set(current_inspector.get_table_names()),
            )

    migrated_inspector = inspect(engine)
    assert {
        "mode",
        "published_version_id",
    }.issubset({column["name"] for column in migrated_inspector.get_columns("knowledge_bases")})
    assert {
        "parent_version_id",
        "publication_state",
        "source_team_id",
        "created_by_agent_id",
        "created_by_user_id",
        "change_reason",
        "published_at",
    }.issubset(
        {
            column["name"]
            for column in migrated_inspector.get_columns("knowledge_base_versions")
        }
    )
    assert "default_knowledge_base_id" in {
        column["name"] for column in migrated_inspector.get_columns("teams")
    }
    assert {
        "team_knowledge_base_bindings",
        "team_knowledge_base_grants",
        "knowledge_base_audit_events",
    }.issubset(set(migrated_inspector.get_table_names()))

    with engine.connect() as conn:
        private_base = conn.execute(
            text(
                "SELECT mode, published_version_id FROM knowledge_bases "
                "WHERE id = 'kb_private'"
            )
        ).mappings().one()
        private_version = conn.execute(
            text(
                "SELECT publication_state, parent_version_id FROM knowledge_base_versions "
                "WHERE id = 'kbver_private'"
            )
        ).mappings().one()
        assert dict(private_base) == {
            "mode": "dedicated",
            "published_version_id": None,
        }
        assert dict(private_version) == {
            "publication_state": "released",
            "parent_version_id": None,
        }
        assert conn.execute(
            text("SELECT default_knowledge_base_id FROM teams WHERE id = 'team_a'")
        ).scalar_one() is None
        assert conn.execute(
            text("SELECT COUNT(*) FROM team_knowledge_base_bindings")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM team_knowledge_base_grants")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM knowledge_base_audit_events")
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT tenant_id, agent_id, knowledge_base_id, base_version, "
                "head_version, status, sync_state, metadata_json "
                "FROM agent_knowledge_branches WHERE id = 'branch_private'"
            )
        ).one() == (
            "tenant_demo",
            "agent_writer",
            "kb_private",
            "1.0.0",
            "1.0.0",
            "active",
            "synced",
            '{"owner": "agent_writer"}',
        )
        assert conn.execute(
            text(
                "SELECT tenant_id, agent_id, resource_type, resource_id, status, metadata_json "
                "FROM agent_resource_bindings WHERE id = 'binding_private'"
            )
        ).one() == (
            "tenant_demo",
            "agent_writer",
            "knowledge",
            "kb_private",
            "active",
            '{"scope": "private"}',
        )

    knowledge_base_indexes = {
        index["name"] for index in migrated_inspector.get_indexes("knowledge_bases")
    }
    version_indexes = {
        index["name"]
        for index in migrated_inspector.get_indexes("knowledge_base_versions")
    }
    team_indexes = {index["name"] for index in migrated_inspector.get_indexes("teams")}
    assert {
        "ix_knowledge_bases_mode",
        "ix_knowledge_bases_published_version_id",
    }.issubset(knowledge_base_indexes)
    assert "ix_knowledge_base_versions_publication_state" in version_indexes
    assert "ix_teams_default_knowledge_base_id" in team_indexes


def test_shared_knowledge_startup_migration_preserves_legacy_visibility_twice(
    monkeypatch, tmp_path
) -> None:
    """旧版数据库连续启动两次后，开放广场、私有分支、文档与团队边界保持不变。"""
    database_path = tmp_path / "legacy-shared-knowledge-startup.db"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)

    # 先构造共享知识库功能上线前的代表性表结构与可见关系。
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_bases (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_base_versions (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR NOT NULL,
                    version VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR NOT NULL,
                    capability_scope VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, knowledge_base_id, version)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_profiles (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    persona_prompt VARCHAR,
                    is_overall BOOLEAN NOT NULL,
                    status VARCHAR NOT NULL,
                    harness_max_actions INTEGER NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, name)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_resource_bindings (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    resource_type VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, agent_id, resource_type, resource_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agent_knowledge_branches (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR NOT NULL,
                    base_version VARCHAR NOT NULL,
                    head_version VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    sync_state VARCHAR NOT NULL,
                    metadata_json JSON,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, agent_id, knowledge_base_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_documents (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    knowledge_base_id VARCHAR,
                    knowledge_base_version_id VARCHAR,
                    filename VARCHAR NOT NULL,
                    file_type VARCHAR NOT NULL,
                    title VARCHAR,
                    status VARCHAR NOT NULL,
                    bucket_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    metadata_json JSON,
                    error VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE teams (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    description VARCHAR,
                    owner_user_id VARCHAR NOT NULL,
                    config_json JSON,
                    status VARCHAR NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (tenant_id, name)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_profiles (
                    id, tenant_id, name, is_overall, status, harness_max_actions,
                    metadata_json, created_at, updated_at
                ) VALUES
                    ('agent_tenant_demo_overall', 'tenant_demo', '整体智能体', 1,
                     'active', 32, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('agent_writer', 'tenant_demo', '内容员工', 0,
                     'active', 32, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_bases (
                    id, tenant_id, name, status, capability_scope, metadata_json,
                    created_at, updated_at
                ) VALUES
                    ('kb_gallery', 'tenant_demo', '开放广场模板', 'active', 'general', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('kb_private', 'tenant_demo', '员工私有资料', 'active', 'general', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_base_versions (
                    id, tenant_id, knowledge_base_id, version, name, status,
                    capability_scope, metadata_json, created_at, updated_at
                ) VALUES
                    ('kbver_gallery', 'tenant_demo', 'kb_gallery', '1.0.0',
                     '开放广场模板', 'active', 'general', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('kbver_private', 'tenant_demo', 'kb_private', '1.0.0',
                     '员工私有资料', 'active', 'general', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_resource_bindings (
                    id, tenant_id, agent_id, resource_type, resource_id, status,
                    metadata_json, created_at, updated_at
                ) VALUES
                    ('binding_gallery', 'tenant_demo', 'agent_tenant_demo_overall',
                     'knowledge_base', 'kb_gallery', 'active', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('binding_private', 'tenant_demo', 'agent_writer',
                     'knowledge_base', 'kb_private', 'active', '{}',
                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_knowledge_branches (
                    id, tenant_id, agent_id, knowledge_base_id, base_version,
                    head_version, status, sync_state, metadata_json, created_at, updated_at
                ) VALUES (
                    'branch_private', 'tenant_demo', 'agent_writer', 'kb_private',
                    '1.0.0', '1.0.0', 'active', 'synced', '{}',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_documents (
                    id, tenant_id, knowledge_base_id, knowledge_base_version_id,
                    filename, file_type, title, status, bucket_count, chunk_count,
                    metadata_json, created_at, updated_at
                ) VALUES (
                    'doc_private', 'tenant_demo', 'kb_private', NULL,
                    'private.md', 'md', '私有文档', 'ready', 1, 1, '{}',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO teams (
                    id, tenant_id, name, owner_user_id, config_json, status,
                    created_at, updated_at
                ) VALUES (
                    'team_legacy', 'tenant_demo', '旧项目组', 'user_admin', '{}', 'active',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "database_url", database_url)

    # 真实走两次应用启动入口，验证迁移重放不会扩大或缩小既有可见范围。
    database.init_db()
    database.init_db()

    with Session(engine) as db:
        assert set(
            visible_knowledge_base_versions(
                db, "tenant_demo", "agent_tenant_demo_overall"
            )
        ) == {"kb_gallery"}
        assert set(
            visible_knowledge_base_versions(db, "tenant_demo", "agent_writer")
        ) == {"kb_private"}

    # 迁移只补 dedicated/released 兼容字段，不创建共享指针或团队关系。
    with engine.connect() as conn:
        bases = conn.execute(
            text(
                """
                SELECT id, mode, published_version_id
                FROM knowledge_bases
                WHERE id IN ('kb_gallery', 'kb_private')
                ORDER BY id
                """
            )
        ).all()
        assert bases == [
            ("kb_gallery", "dedicated", None),
            ("kb_private", "dedicated", None),
        ]
        assert conn.execute(
            text(
                """
                SELECT knowledge_base_id, knowledge_base_version_id
                FROM knowledge_documents
                WHERE id = 'doc_private'
                """
            )
        ).one() == ("kb_private", "kbver_private")
        assert conn.execute(
            text(
                "SELECT default_knowledge_base_id FROM teams WHERE id = 'team_legacy'"
            )
        ).scalar_one() is None
        assert conn.execute(
            text("SELECT COUNT(*) FROM team_knowledge_base_bindings")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM team_knowledge_base_grants")
        ).scalar_one() == 0


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
