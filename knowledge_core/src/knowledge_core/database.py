"""Versioned upgrades for the KM-owned SQLite schema."""

from sqlalchemy import inspect, text

from .models import KnowledgeModel

SCHEMA_VERSION = 2


def initialize_schema(engine):
    if engine.url.get_backend_name() != "sqlite":
        raise ValueError("Knowledge Core currently supports SQLite only")
    with engine.begin() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version > SCHEMA_VERSION:
            raise ValueError("KM database was created by a newer schema version")
        KnowledgeModel.metadata.create_all(connection)
        if version < 2:
            inspector = inspect(connection)
            for name in ("knowledge_spaces", "knowledge_bindings"):
                columns = {column["name"] for column in inspector.get_columns(name)}
                if "updated_at" not in columns:
                    connection.execute(
                        text(f'ALTER TABLE "{name}" ADD COLUMN updated_at DATETIME')
                    )
                    connection.execute(
                        text(f'UPDATE "{name}" SET updated_at = created_at')
                    )
                if name == "knowledge_bindings" and "metadata_json" not in columns:
                    connection.execute(
                        text(
                            "ALTER TABLE knowledge_bindings ADD COLUMN metadata_json JSON NOT NULL DEFAULT '{}'"
                        )
                    )
        connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
