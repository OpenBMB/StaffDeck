from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.capability_scope import CapabilityScope

KnowledgeBaseMode = Literal["dedicated", "shared"]
KnowledgePublicationState = Literal["draft", "released", "rejected"]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KnowledgeBaseCreateRequest(BaseModel):
    tenant_id: str
    name: str
    description: Optional[str] = None
    mode: KnowledgeBaseMode = "dedicated"
    agent_id: str | None = None
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shared_has_no_employee_owner(self) -> KnowledgeBaseCreateRequest:
        """共享知识库属于团队绑定关系，不能同时声明员工私有所有者。"""
        if self.mode == "shared" and self.agent_id:
            raise ValueError("shared knowledge base cannot declare agent_id")
        return self


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
    mode: KnowledgeBaseMode = "dedicated"
    published_version_id: str | None = None
    published_version: str | None = None
    bound_team_count: int = 0
    management_context: dict[str, Any] = Field(default_factory=dict)
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


class KnowledgeBaseVersionRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    version: str
    name: str
    description: str | None = None
    status: str
    publication_state: KnowledgePublicationState
    parent_version_id: str | None = None
    source_team_id: str | None = None
    created_by_agent_id: str | None = None
    created_by_user_id: str | None = None
    change_reason: str | None = None
    published_at: datetime | None = None
    is_published_head: bool = False
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBaseAuditEventRead(BaseModel):
    id: str
    knowledge_base_id: str
    team_id: str | None = None
    team_name: str | None = None
    knowledge_base_version_id: str | None = None
    knowledge_base_version: str | None = None
    actor_type: str
    actor_id: str
    actor_name: str
    action: str
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class KnowledgeBaseAuditPageRead(BaseModel):
    items: list[KnowledgeBaseAuditEventRead] = Field(default_factory=list)
    total: int
    offset: int
    limit: int
    has_more: bool


class SharedKnowledgeTeamRead(BaseModel):
    """一个当前用户可管理且已绑定共享知识库的活动团队。"""

    id: str
    name: str


class SharedKnowledgeDraftCreateRequest(BaseModel):
    tenant_id: str
    team_id: str
    change_reason: NonEmptyText
    expected_published_version_id: str | None = None


class SharedKnowledgePublishRequest(BaseModel):
    tenant_id: str
    team_id: str
    expected_published_version_id: NonEmptyText
    change_reason: NonEmptyText
    idempotency_key: str | None = None


class SharedKnowledgeRejectRequest(BaseModel):
    tenant_id: str
    team_id: str
    change_reason: NonEmptyText
    idempotency_key: str | None = None


class SharedKnowledgeRollbackRequest(BaseModel):
    tenant_id: str
    team_id: str
    target_version_id: NonEmptyText
    expected_published_version_id: NonEmptyText
    change_reason: NonEmptyText
    idempotency_key: str | None = None


class KnowledgeBaseConvertToSharedRequest(BaseModel):
    tenant_id: str
    agent_id: NonEmptyText
    source_version_id: str | None = None
    name: NonEmptyText
    description: str | None = None
    change_reason: NonEmptyText
    team_bindings: list[str] = Field(default_factory=list)
    default_for_team_id: str | None = None

    @model_validator(mode="after")
    def validate_initial_team_bindings(self) -> KnowledgeBaseConvertToSharedRequest:
        """初始绑定不得重复，且默认团队必须属于同一批绑定。"""
        if len(set(self.team_bindings)) != len(self.team_bindings):
            raise ValueError("team_bindings must be unique")
        if self.default_for_team_id and self.default_for_team_id not in self.team_bindings:
            raise ValueError("default_for_team_id must be included in team_bindings")
        return self


class KnowledgeBaseConversionRead(BaseModel):
    source_knowledge_base_id: str
    source_version_id: str
    new_knowledge_base: KnowledgeBaseRead
    released_version: KnowledgeBaseVersionRead
    binding_ids: list[str] = Field(default_factory=list)
    default_for_team_id: str | None = None
    source_archived: bool
    audit_event_id: str


class KnowledgeDocumentUploadRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: Optional[str] = None
    knowledge_base_version_id: str | None = None
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
    knowledge_base_version_id: str | None = None
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
    content_md: Optional[str] = Field(default=None, max_length=2_000_000)
    expected_updated_at: Optional[str] = None


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
    knowledge_base_version_id: str | None = None
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
