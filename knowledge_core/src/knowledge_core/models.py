from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, MetaData, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class KnowledgeModel(SQLModel):
    metadata = MetaData()


class Space(KnowledgeModel, table=True):
    __tablename__ = "knowledge_spaces"
    id: str = Field(default_factory=lambda: new_id("space"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    owner_id: str = "local"
    owner_name: str = "Local"
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBinding(KnowledgeModel, table=True):
    __tablename__ = "knowledge_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "space_id", "knowledge_base_id"),)
    id: str = Field(default_factory=lambda: new_id("binding"), primary_key=True)
    tenant_id: str = Field(index=True)
    space_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    hidden: bool = False
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBranch(KnowledgeModel, table=True):
    __tablename__ = "knowledge_branches"
    __table_args__ = (UniqueConstraint("tenant_id", "space_id", "knowledge_base_id"),)
    id: str = Field(default_factory=lambda: new_id("branch"), primary_key=True)
    tenant_id: str = Field(index=True)
    space_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    base_version_id: str
    head_version_id: str
    status: str = "active"
    sync_state: str = "synced"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Publication(KnowledgeModel, table=True):
    __tablename__ = "knowledge_publications"
    id: str = Field(default_factory=lambda: new_id("publication"), primary_key=True)
    tenant_id: str = Field(index=True)
    suggestion_id: str = Field(index=True, unique=True)
    idempotency_key: str = Field(unique=True)
    status: str = "pending"
    target_reference: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class CitationSnapshot(KnowledgeModel, table=True):
    __tablename__ = "knowledge_citation_snapshots"
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str = Field(index=True)
    payload_json: dict[str, Any] = Field(sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class VersionAccess(KnowledgeModel, table=True):
    __tablename__ = "knowledge_version_access"
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    space_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    version_id: str = Field(index=True)


class KnowledgeBase(KnowledgeModel, table=True):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("kb"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: str | None = None
    status: str = Field(default="active", index=True)
    capability_scope: str = Field(default="general", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseVersion(KnowledgeModel, table=True):
    __tablename__ = "knowledge_base_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "knowledge_base_id", "version", name="uq_knowledge_base_version"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("kbver"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    version: str = Field(default="1.0.0", index=True)
    name: str
    description: str | None = None
    status: str = Field(default="active", index=True)
    capability_scope: str = Field(default="general", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDocument(KnowledgeModel, table=True):
    __tablename__ = "knowledge_documents"

    id: str = Field(default_factory=lambda: new_id("kdoc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    filename: str
    file_type: str = Field(index=True)
    title: str | None = None
    status: str = Field(default="processing", index=True)
    bucket_count: int = 0
    chunk_count: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBucket(KnowledgeModel, table=True):
    __tablename__ = "knowledge_buckets"

    id: str = Field(default_factory=lambda: new_id("kbucket"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_key: str = Field(index=True)
    title: str
    summary: str
    token_estimate: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(KnowledgeModel, table=True):
    __tablename__ = "knowledge_chunks"

    id: str = Field(default_factory=lambda: new_id("kchunk"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: str = Field(index=True)
    chunk_index: int = Field(index=True)
    content: str
    summary: str | None = None
    source_ref: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeConcept(KnowledgeModel, table=True):
    __tablename__ = "knowledge_concepts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_version_id",
            "concept_id",
            name="uq_knowledge_concept_version_path",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("kconcept"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    document_id: str | None = Field(default=None, index=True)
    concept_id: str = Field(index=True)
    concept_type: str = Field(index=True)
    title: str
    description: str | None = None
    content_md: str
    frontmatter_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    links_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDiscoverySuggestion(KnowledgeModel, table=True):
    __tablename__ = "knowledge_discovery_suggestions"

    id: str = Field(default_factory=lambda: new_id("kdisc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: str | None = Field(default=None, index=True)
    suggestion_type: str = Field(index=True)
    title: str
    status: str = Field(default="pending", index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeIngestJob(KnowledgeModel, table=True):
    __tablename__ = "knowledge_ingest_jobs"

    id: str = Field(default_factory=lambda: new_id("kjob"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: str | None = Field(default=None, index=True)
    document_id: str | None = Field(default=None, index=True)
    filename: str
    status: str = Field(default="queued", index=True)
    stage: str = "queued"
    progress: float = 0.0
    error: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
