"""Core metadata has no space, catalog, version or business-resource columns."""
from datetime import UTC, datetime
from uuid import uuid4
from sqlalchemy import Column, DateTime, Float, Integer, JSON, MetaData, String, Table, Text


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def identifier(prefix):
    return prefix + "_" + uuid4().hex


def entity(name, *columns):
    return Table(name, metadata, Column("id", String, primary_key=True),
                 *columns, Column("created_at", DateTime, nullable=False),
                 Column("updated_at", DateTime, nullable=False))


metadata = MetaData()
documents = entity("km_documents", Column("filename", String, nullable=False), Column("file_type", String),
    Column("title", String), Column("status", String, nullable=False), Column("content", Text),
    Column("metadata", JSON, nullable=False), Column("bucket_count", Integer), Column("chunk_count", Integer))
buckets = entity("km_buckets", Column("document_id", String, index=True, nullable=False),
    Column("title", String), Column("summary", Text), Column("content", Text), Column("metadata", JSON, nullable=False))
chunks = entity("km_chunks", Column("document_id", String, index=True, nullable=False),
    Column("bucket_id", String, index=True, nullable=False), Column("chunk_index", Integer),
    Column("content", Text), Column("summary", Text), Column("source_ref", String), Column("metadata", JSON, nullable=False))
jobs = entity("km_ingest_jobs", Column("filename", String), Column("status", String, index=True),
    Column("stage", String), Column("progress", Float), Column("error", Text), Column("document_id", String, index=True),
    Column("payload", JSON), Column("context", JSON), Column("principal", JSON), Column("finished_at", DateTime))
citations = Table("km_citation_snapshots", metadata, Column("id", String, primary_key=True),
    Column("document_id", String, index=True), Column("payload", JSON, nullable=False))
schema = Table("km_schema_versions", metadata, Column("module", String, primary_key=True),
    Column("version", Integer, nullable=False))
CORE_TABLES = tuple(metadata.tables)
