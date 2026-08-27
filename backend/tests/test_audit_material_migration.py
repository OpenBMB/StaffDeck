from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

from app.db import (
    database,
    models,  # noqa: F401
)


def test_audit_material_schema_migration_rebuilds_legacy_unique_constraints(tmp_path) -> None:
    db_path = tmp_path / "legacy-materials.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE audit_case_materials (
                id VARCHAR PRIMARY KEY,
                tenant_id VARCHAR NOT NULL,
                audit_case_id VARCHAR NOT NULL,
                attachment_id VARCHAR NOT NULL,
                material_type VARCHAR NOT NULL,
                filename VARCHAR NOT NULL,
                content_type VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                size INTEGER NOT NULL,
                storage_key VARCHAR NOT NULL,
                extracted_text_storage_key VARCHAR,
                characters INTEGER NOT NULL DEFAULT 0,
                extraction_status VARCHAR NOT NULL DEFAULT 'pending',
                processing_status VARCHAR NOT NULL DEFAULT 'pending',
                version INTEGER NOT NULL DEFAULT 1,
                is_current BOOLEAN NOT NULL DEFAULT 1,
                supersedes_material_id VARCHAR,
                error_code VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_audit_case_material_sha
                    UNIQUE (tenant_id, audit_case_id, sha256),
                CONSTRAINT uq_audit_case_material_version
                    UNIQUE (tenant_id, audit_case_id, filename, version)
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO audit_case_materials "
            "(id, tenant_id, audit_case_id, attachment_id, material_type, filename, "
            "content_type, sha256, size, storage_key, created_at, updated_at) "
            "VALUES ('m1','t1','c1','a1','audit_record','same.txt','text/plain','h1',1,'k1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )

    with engine.begin() as conn:
        database._migrate_audit_case_material_schema(
            conn,
            inspect(conn),
            {"audit_case_materials"},
        )

    with engine.connect() as conn:
        constraints = [
            tuple(item["column_names"])
            for item in inspect(conn).get_unique_constraints("audit_case_materials")
        ]
        assert any(
            columns == ("tenant_id", "audit_case_id", "sha256", "material_type")
            for columns in constraints
        )
        assert any(
            columns == ("tenant_id", "audit_case_id", "material_type", "filename", "version")
            for columns in constraints
        )
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM audit_case_materials"
        ).scalar_one() == 1


def test_audit_material_schema_migration_is_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migrated-materials.db'}")
    SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:
        database._migrate_audit_case_material_schema(
            conn,
            inspect(conn),
            {"audit_case_materials"},
        )
        database._migrate_audit_case_material_schema(
            conn,
            inspect(conn),
            {"audit_case_materials"},
        )
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM audit_case_materials"
        ).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM app_data_migrations "
            "WHERE id = 'audit_case_material_unique_type_v1'"
        ).scalar_one() == 1
