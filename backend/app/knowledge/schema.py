from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.capability_scope import CapabilityScope


class KnowledgeBaseCreateRequest(BaseModel):
    tenant_id: str
    name: str
    description: Optional[str] = None
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    capability_scope: Optional[CapabilityScope] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBaseRollbackRequest(BaseModel):
    tenant_id: str
    agent_id: str
    version: str


class KnowledgeBaseRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    status: str
    capability_scope: CapabilityScope
    version: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    branch_head_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0
    bucket_count: int = 0
    chunk_count: int = 0
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentUploadRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    title: Optional[str] = None
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestJobRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: Optional[str] = None
    filename: str
    status: str
    stage: str
    progress: float
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    filename: str
    file_type: str
    title: Optional[str] = None
    status: str
    bucket_count: int
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentUpdateRequest(BaseModel):
    tenant_id: str
    title: Optional[str] = None
    status: Optional[Literal["ready", "processing", "failed", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBucketRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_key: str
    title: str
    summary: str
    token_estimate: int
    chunk_count: int = 0
    status: str = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBucketUpdateRequest(BaseModel):
    tenant_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeChunkRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: str
    chunk_index: int
    content: str
    summary: Optional[str] = None
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeChunkUpdateRequest(BaseModel):
    tenant_id: str
    content: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeConceptRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    document_id: Optional[str] = None
    concept_id: str
    concept_type: str
    title: str
    description: Optional[str] = None
    content_md: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    links: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeConceptUpdateRequest(BaseModel):
    tenant_id: str
    content_md: str
    document_id: Optional[str] = None
    status: Literal["active", "archived"] = "active"


class KnowledgeOkfImportRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    agent_id: Optional[str] = None


class KnowledgeSearchRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    query: str
    query_type: Literal["answer", "policy_check", "tool_discovery", "skill_discovery"] = "answer"
    desired_evidence: Optional[str] = None
    scope: dict[str, Any] = Field(default_factory=dict)
    model_config_id: Optional[str] = None
    mode: Literal["chat", "skill_discovery", "debug"] = "chat"
    knowledge_base_ids: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    max_bucket_rounds: int = 2
    max_buckets: int = 4
    max_chunks: int = 8
    budget_tokens: int = 4000
    max_depth: int = 2
    need_evidence_pack: bool = True


class KnowledgeSearchResponse(BaseModel):
    selected_buckets: list[KnowledgeBucketRead] = Field(default_factory=list)
    chunks: list[KnowledgeChunkRead] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    route_trace: list[dict[str, Any]] = Field(default_factory=list)
    selected_documents: list[dict[str, Any]] = Field(default_factory=list)
    selected_concepts: list[dict[str, Any]] = Field(default_factory=list)
    expanded_sections: list[dict[str, Any]] = Field(default_factory=list)
    okf_citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_pack: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeDiscoveryRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: Optional[str] = None
    suggestion_type: Literal["skill", "tool", "warning"]
    title: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    reason: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
