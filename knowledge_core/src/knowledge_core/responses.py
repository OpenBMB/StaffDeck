"""Wire response types have no persistence metadata or optional module imports."""
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Entity(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime


class Space(Entity):
    name: str
    owner_id: str = 'local'
    owner_name: str = 'Local'
    status: str = 'active'


class KnowledgeBase(Entity):
    name: str
    description: str | None = None
    status: str = 'active'
    capability_scope: str = 'general'
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseVersion(KnowledgeBase):
    knowledge_base_id: str
    version: str


class KnowledgeBinding(Entity):
    space_id: str
    knowledge_base_id: str
    hidden: bool = False
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBranch(Entity):
    space_id: str
    knowledge_base_id: str
    base_version_id: str
    head_version_id: str
    status: str = 'active'
    sync_state: str = 'synced'


class KnowledgeDocument(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    filename: str
    file_type: str
    title: str | None = None
    status: str
    bucket_count: int
    chunk_count: int
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class KnowledgeBucket(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    document_id: str
    bucket_key: str
    title: str
    summary: str
    token_estimate: int
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunk(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    document_id: str
    bucket_id: str
    chunk_index: int
    content: str
    summary: str | None = None
    source_ref: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeConcept(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    document_id: str | None = None
    concept_id: str
    concept_type: str
    title: str
    description: str | None = None
    content_md: str
    frontmatter_json: dict[str, Any] = Field(default_factory=dict)
    links_json: list[dict[str, Any]] = Field(default_factory=list)
    citations_json: list[dict[str, Any]] = Field(default_factory=list)
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list)
    status: str = 'active'


class KnowledgeDiscoverySuggestion(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    document_id: str
    suggestion_type: str
    title: str
    status: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list)
    reason: str | None = None


class KnowledgeIngestJob(Entity):
    knowledge_base_id: str
    knowledge_base_version_id: str | None = None
    document_id: str | None = None
    filename: str
    status: str
    stage: str
    progress: float
    error: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
