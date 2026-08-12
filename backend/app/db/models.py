from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import JSON, Column, Index, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    id: str = Field(primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)

    id: str = Field(default_factory=lambda: new_id("user"), primary_key=True)
    tenant_id: str = Field(index=True)
    username: str = Field(index=True)
    display_name: Optional[str] = None
    role: str = Field(default="member", index=True)
    # 账号来源:web=网页端创建;wechat 等=渠道懒建(用户管理列表默认隐藏)
    source: str = Field(default="web", index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserAvatar(SQLModel, table=True):
    """用户头像:小图以 data_url 直接存库(与聊天附件内联方式一致),

    独立小表避免 users 热表膨胀;create_all 建表,无需 ALTER。
    """

    __tablename__ = "user_avatars"

    user_id: str = Field(primary_key=True)
    data_url: str
    updated_at: datetime = Field(default_factory=utc_now)


class APIClient(SQLModel, table=True):
    """Server-to-server identity for the versioned public API."""

    __tablename__ = "api_clients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_api_client_tenant_name"),
    )

    id: str = Field(default_factory=lambda: new_id("apiclient"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_by_user_id: Optional[str] = Field(default=None, index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class APICredential(SQLModel, table=True):
    """Hashed tenant or agent credential. Plaintext is returned exactly once."""

    __tablename__ = "api_credentials"
    __table_args__ = (
        UniqueConstraint("key_prefix", name="uq_api_credential_prefix"),
    )

    id: str = Field(default_factory=lambda: new_id("apicred"), primary_key=True)
    tenant_id: str = Field(index=True)
    client_id: str = Field(index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    name: str
    key_prefix: str = Field(index=True)
    key_digest: str
    scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    last_used_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revoked_at: Optional[datetime] = None


class APIIdempotencyRecord(SQLModel, table=True):
    __tablename__ = "api_idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "credential_id", "method", "path", "idempotency_key",
            name="uq_api_idempotency_request",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("apiidem"), primary_key=True)
    tenant_id: str = Field(index=True)
    credential_id: str = Field(index=True)
    method: str
    path: str
    idempotency_key: str = Field(index=True)
    request_hash: str
    status_code: int = 200
    response_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    resource_id: Optional[str] = Field(default=None, index=True)
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class APIJob(SQLModel, table=True):
    __tablename__ = "api_jobs"

    id: str = Field(default_factory=lambda: new_id("apijob"), primary_key=True)
    tenant_id: str = Field(index=True)
    credential_id: str = Field(index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(index=True)
    status: str = Field(default="queued", index=True)
    stage: str = Field(default="queued", index=True)
    progress: float = 0.0
    request_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    cancel_requested: bool = False
    retryable: bool = False
    session_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class APIJobEvent(SQLModel, table=True):
    __tablename__ = "api_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_api_job_event_sequence"),
    )

    id: str = Field(default_factory=lambda: new_id("apievent"), primary_key=True)
    tenant_id: str = Field(index=True)
    job_id: str = Field(index=True)
    sequence: int = Field(index=True)
    event_type: str = Field(index=True)
    data_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    public: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class WebhookEndpoint(SQLModel, table=True):
    __tablename__ = "webhook_endpoints"

    id: str = Field(default_factory=lambda: new_id("webhook"), primary_key=True)
    tenant_id: str = Field(index=True)
    client_id: str = Field(index=True)
    name: str
    url: str
    secret_encrypted: str
    events_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WebhookDelivery(SQLModel, table=True):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_delivery_event"),
    )

    id: str = Field(default_factory=lambda: new_id("whdelivery"), primary_key=True)
    tenant_id: str = Field(index=True)
    endpoint_id: str = Field(index=True)
    event_id: str = Field(index=True)
    event_type: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="queued", index=True)
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = Field(default=None, index=True)
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    delivered_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class ExternalSessionBinding(SQLModel, table=True):
    __tablename__ = "external_session_bindings"
    __table_args__ = (
        UniqueConstraint(
            "credential_id", "external_session_id",
            name="uq_external_session_credential_id",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("extsession"), primary_key=True)
    tenant_id: str = Field(index=True)
    credential_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    external_session_id: str = Field(index=True)
    external_user_id: Optional[str] = Field(default=None, index=True)
    session_id: str = Field(index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class APISOPDraft(SQLModel, table=True):
    __tablename__ = "api_sop_drafts"

    id: str = Field(default_factory=lambda: new_id("sopdraft"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    base_version: Optional[str] = Field(default=None, index=True)
    draft_version: str = Field(index=True)
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="draft", index=True)
    source: str = Field(default="structured", index=True)
    warnings_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    validation_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    etag: str = Field(index=True)
    created_by_credential_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    published_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class APIAuditLog(SQLModel, table=True):
    __tablename__ = "api_audit_logs"

    id: str = Field(default_factory=lambda: new_id("apiaudit"), primary_key=True)
    tenant_id: Optional[str] = Field(default=None, index=True)
    credential_id: Optional[str] = Field(default=None, index=True)
    request_id: str = Field(index=True)
    method: str
    path: str
    resource_type: Optional[str] = Field(default=None, index=True)
    resource_id: Optional[str] = Field(default=None, index=True)
    status_code: int = Field(index=True)
    duration_ms: float
    created_at: datetime = Field(default_factory=utc_now)


class Skill(SQLModel, table=True):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", name="uq_skill_tenant_skill_id"),)

    id: str = Field(default_factory=lambda: new_id("skill"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    version: str = "1.0.0"
    name: str
    business_domain: Optional[str] = None
    description: Optional[str] = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="draft", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillVersion(SQLModel, table=True):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", "version", name="uq_skill_version"),)

    id: str = Field(default_factory=lambda: new_id("skillver"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    version: str = Field(index=True)
    name: str
    business_domain: Optional[str] = None
    description: Optional[str] = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="draft", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranch(SQLModel, table=True):
    __tablename__ = "agent_skill_branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "skill_id", name="uq_agent_skill_branch"),
    )

    id: str = Field(default_factory=lambda: new_id("agentbranch"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    source_skill_id: str = Field(index=True)
    base_version: str = "1.0.0"
    head_version: str = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranchVersion(SQLModel, table=True):
    __tablename__ = "agent_skill_branch_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "skill_id", "version", name="uq_agent_skill_branch_version"),
    )

    id: str = Field(default_factory=lambda: new_id("agentbranchver"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    source_skill_id: str = Field(index=True)
    version: str = Field(index=True)
    base_version: str = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="diverged", index=True)
    change_summary: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralSkill(SQLModel, table=True):
    __tablename__ = "general_skills"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_general_skill_tenant_slug"),)

    id: str = Field(default_factory=lambda: new_id("genskill"), primary_key=True)
    tenant_id: str = Field(index=True)
    slug: str = Field(index=True)
    name: str
    description: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="draft", index=True)
    capability_scope: str = Field(default="general", index=True)
    permissions_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    runtime_config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBase(SQLModel, table=True):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("kb"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    status: str = Field(default="active", index=True)
    capability_scope: str = Field(default="general", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseVersion(SQLModel, table=True):
    __tablename__ = "knowledge_base_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "knowledge_base_id", "version", name="uq_knowledge_base_version"),
    )

    id: str = Field(default_factory=lambda: new_id("kbver"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    version: str = Field(default="1.0.0", index=True)
    name: str
    description: Optional[str] = None
    status: str = Field(default="active", index=True)
    capability_scope: str = Field(default="general", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentKnowledgeBranch(SQLModel, table=True):
    __tablename__ = "agent_knowledge_branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "knowledge_base_id", name="uq_agent_knowledge_branch"),
    )

    id: str = Field(default_factory=lambda: new_id("agentkb"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    base_version: str = "1.0.0"
    head_version: str = "1.0.0"
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDocument(SQLModel, table=True):
    __tablename__ = "knowledge_documents"

    id: str = Field(default_factory=lambda: new_id("kdoc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    filename: str
    file_type: str = Field(index=True)
    title: Optional[str] = None
    status: str = Field(default="processing", index=True)
    bucket_count: int = 0
    chunk_count: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBucket(SQLModel, table=True):
    __tablename__ = "knowledge_buckets"

    id: str = Field(default_factory=lambda: new_id("kbucket"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_key: str = Field(index=True)
    title: str
    summary: str
    token_estimate: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(SQLModel, table=True):
    __tablename__ = "knowledge_chunks"

    id: str = Field(default_factory=lambda: new_id("kchunk"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: str = Field(index=True)
    chunk_index: int = Field(index=True)
    content: str
    summary: Optional[str] = None
    source_ref: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeConcept(SQLModel, table=True):
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
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: Optional[str] = Field(default=None, index=True)
    concept_id: str = Field(index=True)
    concept_type: str = Field(index=True)
    title: str
    description: Optional[str] = None
    content_md: str
    frontmatter_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    links_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDiscoverySuggestion(SQLModel, table=True):
    __tablename__ = "knowledge_discovery_suggestions"

    id: str = Field(default_factory=lambda: new_id("kdisc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: Optional[str] = Field(default=None, index=True)
    suggestion_type: str = Field(index=True)
    title: str
    status: str = Field(default="pending", index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeIngestJob(SQLModel, table=True):
    __tablename__ = "knowledge_ingest_jobs"

    id: str = Field(default_factory=lambda: new_id("kjob"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: Optional[str] = Field(default=None, index=True)
    filename: str
    status: str = Field(default="queued", index=True)
    stage: str = "queued"
    progress: float = 0.0
    error: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class ModelConfig(SQLModel, table=True):
    __tablename__ = "model_configs"

    id: str = Field(default_factory=lambda: new_id("model"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    provider: str = "openai_compatible"
    api_protocol: str = Field(default="openai_chat_completions", index=True)
    base_url: Optional[str] = None
    api_key_encrypted: str
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    protocol_options_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    legacy_unmapped_options_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    trust_status: str = Field(default="unverified", index=True)
    verified_at: Optional[datetime] = None
    verified_fingerprint: Optional[str] = None
    verification_attempt_id: Optional[str] = None
    verification_started_at: Optional[datetime] = None
    verification_attempt_status: str = Field(default="idle", index=True)
    verification_attempt_error_code: Optional[str] = None
    config_revision: int = 1
    security_revision: int = 1
    key_revision: int = 1
    is_default: bool = False
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PersonaConfig(SQLModel, table=True):
    __tablename__ = "persona_configs"

    tenant_id: str = Field(primary_key=True)
    system_prompt: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UIConfig(SQLModel, table=True):
    __tablename__ = "ui_configs"

    tenant_id: str = Field(primary_key=True)
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = 1
    agent_loop_max_actions: int = 32
    sandbox_enabled: bool = False
    sandbox_network_mode: str = Field(default="all")
    sandbox_allowed_domains: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    harness_storage_path: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentProfile(SQLModel, table=True):
    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_agent_profile_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("agent"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = Field(default=False, index=True)
    status: str = Field(default="active", index=True)
    harness_max_actions: int = Field(default=32)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentUsage(SQLModel, table=True):
    __tablename__ = "agent_usages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "agent_id", name="uq_agent_usage_user_agent"),
    )

    id: str = Field(default_factory=lambda: new_id("agentuse"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentModelBinding(SQLModel, table=True):
    __tablename__ = "agent_model_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "role", name="uq_agent_model_binding"),
    )

    id: str = Field(default_factory=lambda: new_id("agentmodel"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    role: str = Field(default="default", index=True)
    model_config_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentResourceBinding(SQLModel, table=True):
    __tablename__ = "agent_resource_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "resource_type", "resource_id", name="uq_agent_resource"),
    )

    id: str = Field(default_factory=lambda: new_id("agentres"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    resource_type: str = Field(index=True)
    resource_id: str = Field(index=True)
    status: str = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Tool(SQLModel, table=True):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tool_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str = Field(index=True)
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = Field(default="未分桶", index=True)
    tool_type: str = Field(default="http", index=True)
    method: str
    url: str
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    auth_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    input_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    allowed_skills_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    mcp_server_id: Optional[str] = Field(default=None, index=True)
    capability_scope: str = Field(default="general", index=True)
    capability_scope_inherited: bool = True
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MCPServer(SQLModel, table=True):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_server_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("mcpsrv"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str = Field(index=True)
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = Field(default="MCP 工具", index=True)
    # 连接方式：stdio / streamable_http / sse / builtin
    transport: str = Field(default="streamable_http", index=True)
    # streamable_http / sse 使用
    url: Optional[str] = None
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # stdio 使用
    command: Optional[str] = None
    args_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    env_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    cwd: Optional[str] = None
    # MCP Apps is opt-in so legacy standard MCP servers retain identical behavior.
    apps_mode: str = Field(default="disabled", index=True)
    negotiated_capabilities_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    # 最近一次发现的原始工具定义（预览/审计用）
    discovered_tools_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    last_synced_at: Optional[datetime] = None
    capability_scope: str = Field(default="general", index=True)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MockOrder(SQLModel, table=True):
    __tablename__ = "mock_orders"

    order_id: str = Field(primary_key=True)
    user_id: Optional[str] = Field(default=None, index=True)
    product_id: Optional[str] = Field(default=None, index=True)
    sku_id: Optional[str] = None
    quantity: int = 1
    status: str = Field(default="created", index=True)
    payment_status: Optional[str] = None
    order_status: Optional[str] = None
    signed_days: int = 0
    refundable: bool = True
    total_amount: float = 0.0
    currency: str = "CNY"
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChatSession(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: Optional[str] = Field(default=None, index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill_stack_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    pending_tasks_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    resume_after_answer_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    awaiting_input_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    knowledge_context_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    context_state_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"
    channel: Optional[str] = None
    external_conv_id: Optional[str] = None
    channel_target_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    # 渠道会话直挂绑定:出站 staging 优先按它直查,不再靠 (agent_id, channel) 反查
    channel_binding_id: Optional[str] = None
    # 渠道外部账号稳定键:绑定删除后仍保留,仅允许同一外部 Bot 精确认领历史会话
    channel_account_key: Optional[str] = Field(default=None, index=True)
    # 团队会话挂接:非空表示该会话属于某团队(TL 对话/任务执行等)
    team_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelBinding(SQLModel, table=True):
    __tablename__ = "channel_bindings"

    id: str = Field(default_factory=lambda: new_id("chan"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    channel: str = Field(default="wechat", index=True)
    # 团队绑定:非空表示该渠道接入某团队(与员工挂载互斥),入站消息直路由团队 TL;
    # 存 team_id 不存 leader,换帅自动跟随
    team_id: Optional[str] = Field(default=None, index=True)
    # pending/active/expired/disabled
    status: str = Field(default="pending", index=True)
    # Fernet 加密后的渠道凭证（如微信 bot_token），绝不回传明文
    credentials_enc: Optional[str] = None
    # ilink_bot_id、baseurl、get_updates_buf 游标、session_expired、bound_at 等
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # provider 侧 Bot 的稳定连接键,全部署唯一;pending 绑定激活前允许为空
    external_account_key: Optional[str] = Field(default=None, unique=True, index=True)
    # 身份作用域稳定键:企微为 corp_id,微信为空字符串
    identity_scope_key: Optional[str] = Field(default=None, index=True)
    # provider 回调声明的租户边界；飞书首次可信事件中 CAS 固定 tenant_key
    provider_tenant_key: Optional[str] = Field(default=None, index=True)
    # 每次凭证/账号配置成功提交后递增,用于 ingress 代际隔离
    config_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    connected: bool = False
    # 最近一次成功连上渠道的时间(企微断开超时告警的时间基准)
    last_connected_at: Optional[datetime] = None
    created_by_user_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelBindingAgent(SQLModel, table=True):
    """渠道账号可调度的员工集合（一个微信号挂载多个数字员工，恰好一个默认）。"""

    __tablename__ = "channel_binding_agents"
    __table_args__ = (UniqueConstraint("binding_id", "agent_id", name="uq_channel_binding_agent"),)

    id: str = Field(default_factory=lambda: new_id("chba"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    is_default: bool = False
    sort_order: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class ChannelConvState(SQLModel, table=True):
    """路由指针：每个 (binding, external_conv_id) 会话的当前员工。"""

    __tablename__ = "channel_conv_states"
    __table_args__ = (
        UniqueConstraint("binding_id", "external_conv_id", name="uq_channel_conv_state"),
    )

    id: str = Field(default_factory=lambda: new_id("chconv"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    external_conv_id: str
    current_agent_id: str
    # 手动 /切换 后的保护窗:此时间之前跳过智能自动分发
    manual_pin_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelBindCode(SQLModel, table=True):
    """渠道身份自助绑定码:网页端生成,渠道侧 /绑定 <码> 核销。"""

    __tablename__ = "channel_bind_codes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_channel_bind_code_tenant_code"),
        UniqueConstraint("tenant_id", "user_id", name="uq_channel_bind_code_tenant_user"),
    )

    id: str = Field(default_factory=lambda: new_id("chbc"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    code: str = Field(index=True)
    expires_at: datetime
    used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class ChannelIdentity(SQLModel, table=True):
    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_account_scope",
            "external_user_id",
            name="uq_channel_identity_scope_external",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("chident"), primary_key=True)
    tenant_id: str = Field(index=True)
    channel: str = Field(index=True)
    # 渠道账号作用域:wechat 置空(全局 wxid);wecom 取 corp_id/bot_id/binding.id,隔离跨企业身份
    external_account_scope: str = Field(default="", index=True)
    external_user_id: str
    staffdeck_user_id: str = Field(index=True)
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelInboundEvent(SQLModel, table=True):
    __tablename__ = "channel_inbound_events"
    __table_args__ = (
        UniqueConstraint("binding_id", "event_id", name="uq_channel_inbound_event_binding"),
        Index(
            "ix_channel_inbound_events_binding_status_created",
            "binding_id",
            "status",
            "created_at",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("chevt"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    channel: str = Field(index=True)
    event_id: str
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # 入站时的绑定配置代次，仅用于 ingress 代际审计；已落库事件不因后续轮换失效
    config_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    # 每条事件不可变的回复目标；异步处理不得读取会话上的可变 target
    target_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    # 收到确认标记的句柄；最终回复送达后据此异步撤回。飞书存远端 reaction_id；
    # 钉钉 emotion 接口不返回 ID，存本地哨兵值表示"已挂上待撤回"。
    reaction_id: Optional[str] = Field(default=None, index=True)
    # received/processing/done/failed
    status: str = Field(default="received", index=True)
    # 创建/接管该事件的进程启动代次；当前代次仍在运行时禁止按墙钟误接管。
    processor_run_id: Optional[str] = Field(default=None, index=True)
    error: Optional[str] = None
    processed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelDelivery(SQLModel, table=True):
    __tablename__ = "channel_deliveries"

    id: str = Field(default_factory=lambda: new_id("chdlv"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    session_id: str = Field(index=True)
    message_id: Optional[str] = Field(default=None, index=True)
    # 投递目标：to_user_id + context_token
    target_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # reply/error_notice
    kind: str = Field(default="reply", index=True)
    text: str
    # pending/sending/delivered/failed
    status: str = Field(default="pending", index=True)
    attempts: int = 0
    next_attempt_at: Optional[datetime] = Field(default=None, index=True)
    # 原子 claim 的抢占时间(守护据此重置卡死投递)
    sending_since: Optional[datetime] = None
    last_error: Optional[str] = None
    # 回复类投递 = message_id，天然幂等
    idempotency_key: str = Field(unique=True, index=True)
    # 第一次真正尝试远端发送的时间，用于飞书 UUID 一小时去重窗口
    first_attempt_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HumanHandoffRequest(SQLModel, table=True):
    __tablename__ = "human_handoff_requests"

    id: str = Field(default_factory=lambda: new_id("handoff"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    requester_user_id: Optional[str] = Field(default=None, index=True)
    assignee_user_id: Optional[str] = Field(default=None, index=True)
    trigger_skill_id: Optional[str] = Field(default=None, index=True)
    trigger_step_id: Optional[str] = Field(default=None, index=True)
    context_summary: Optional[str] = None
    pending_question: Optional[str] = None
    status: str = Field(default="pending", index=True)
    human_reply: Optional[str] = None
    resume_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    answered_at: Optional[datetime] = None


class ScheduledTask(SQLModel, table=True):
    __tablename__ = "scheduled_tasks"

    id: str = Field(default_factory=lambda: new_id("sched"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    created_by_user_id: str = Field(index=True)
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: str = Field(default="daily", index=True)
    schedule_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    timezone: str = Field(default="Asia/Shanghai", index=True)
    rrule: Optional[str] = None
    status: str = Field(default="active", index=True)
    concurrency_policy: str = Field(default="forbid", index=True)
    misfire_policy: str = Field(default="coalesce", index=True)
    max_runs: Optional[int] = None
    end_at: Optional[datetime] = Field(default=None, index=True)
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    last_run_at: Optional[datetime] = Field(default=None, index=True)
    last_status: Optional[str] = Field(default=None, index=True)
    run_count: int = 0
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_until: Optional[datetime] = Field(default=None, index=True)
    source_session_id: Optional[str] = Field(default=None, index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduledTaskRun(SQLModel, table=True):
    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        UniqueConstraint("scheduled_task_id", "scheduled_for", name="uq_scheduled_task_run_due_time"),
    )

    id: str = Field(default_factory=lambda: new_id("schedrun"), primary_key=True)
    tenant_id: str = Field(index=True)
    scheduled_task_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    user_id: str = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    scheduled_for: datetime = Field(index=True)
    status: str = Field(default="queued", index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    result_summary: Optional[str] = None
    error: Optional[str] = None
    trace_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessTaskFrameRecord(SQLModel, table=True):
    """Durable TaskFrame state for the isolated Harness v2 execution path."""

    __tablename__ = "harness_task_frames"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "task_id", name="uq_harness_task_frame_session_task"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("htask"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    source_turn_id: str = Field(index=True)
    task_id: str = Field(index=True)
    kind: str = Field(default="conversation", index=True)
    decision: str = Field(default="answer_only", index=True)
    status: str = Field(default="queued", index=True)
    sequence: int = 0
    skill_id: Optional[str] = Field(default=None, index=True)
    step_id: Optional[str] = Field(default=None, index=True)
    user_intent: Optional[str] = None
    requirements_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    depends_on_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    task_requirement_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    state_version: int = 1
    attempt_no: int = 0
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessRunRecord(SQLModel, table=True):
    __tablename__ = "harness_runs"

    id: str = Field(default_factory=lambda: new_id("hrun"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    task_frame_record_id: str = Field(index=True)
    task_id: str = Field(index=True)
    source_turn_id: str = Field(index=True)
    status: str = Field(default="running", index=True)
    attempt_no: int = 1
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    action_count: int = 0
    task_requirement_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    capability_snapshot_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessTurnRecord(SQLModel, table=True):
    """Exactly-once receipt for one client-addressable Harness turn."""

    __tablename__ = "harness_turns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "client_turn_id",
            name="uq_harness_turn_client_receipt",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("hturn"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    client_turn_id: str = Field(index=True)
    request_digest: str = Field(index=True)
    status: str = Field(default="started", index=True)
    lease_owner: str = Field(index=True)
    lease_expires_at: datetime = Field(index=True)
    user_message_id: Optional[str] = Field(default=None, index=True)
    response_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    error_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessSessionLeaseRecord(SQLModel, table=True):
    """Cross-process execution fence for one Harness chat session."""

    __tablename__ = "harness_session_leases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            name="uq_harness_session_execution_lease",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("hslease"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    lease_owner: str = Field(index=True)
    lease_expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HarnessInvocationRecord(SQLModel, table=True):
    __tablename__ = "harness_invocations"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "call_id", name="uq_harness_invocation_run_call"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("hinvoke"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    task_id: str = Field(index=True)
    run_id: str = Field(index=True)
    call_id: str = Field(index=True)
    tool_name: str = Field(index=True)
    request_digest: str = Field(index=True)
    logical_action_key: Optional[str] = Field(
        default=None,
        unique=True,
        index=True,
    )
    replayed_from_invocation_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="started", index=True)
    arguments_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    response_cache_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    approval_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    role: str
    content: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MessageFeedback(SQLModel, table=True):
    __tablename__ = "message_feedback"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_feedback_message_user"),)

    id: str = Field(default_factory=lambda: new_id("fb"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    message_id: str = Field(index=True)
    user_id: str = Field(index=True)
    rating: str = Field(index=True)
    analysis_status: str = Field(default="pending", index=True)
    analysis_bucket: Optional[str] = Field(default=None, index=True)
    analysis_reason: Optional[str] = None
    analysis_summary: Optional[str] = None
    analysis_confidence: Optional[float] = None
    analysis_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    analyzed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillFeedback(SQLModel, table=True):
    __tablename__ = "skill_feedback"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_skill_feedback_message_user"),)

    id: str = Field(default_factory=lambda: new_id("skillfb"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    skill_version: Optional[str] = Field(default=None, index=True)
    step_id: Optional[str] = Field(default=None, index=True)
    session_id: str = Field(index=True)
    message_id: str = Field(index=True)
    user_id: str = Field(index=True)
    rating: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentEvent(SQLModel, table=True):
    __tablename__ = "agent_events"

    id: str = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    event_type: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memories"

    id: str = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    username: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(default="conversation", index=True)
    content: str
    importance: float = 0.5
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Team(SQLModel, table=True):
    """多 Agent 团队:一名 TL(leader 角色成员)+ 若干成员(数字员工)。"""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_team_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("team"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    owner_user_id: str = Field(index=True)
    # 预留:并发策略/竞标等团队级配置
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TeamMember(SQLModel, table=True):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "agent_id", name="uq_team_member_agent"),)

    id: str = Field(default_factory=lambda: new_id("team_member"), primary_key=True)
    team_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    # role: leader(TL,每团队至多一名)/ member
    role: str = Field(default="member", index=True)
    created_at: datetime = Field(default_factory=utc_now)


class TeamTask(SQLModel, table=True):
    """团队任务:pending -> in_progress -> review -> done/rework/escalated;

    rework -> in_progress 重入;pending -> bidding -> pending 为任务池竞标链路。
    """

    __tablename__ = "team_tasks"

    id: str = Field(default_factory=lambda: new_id("team_task"), primary_key=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    parent_task_id: Optional[str] = Field(default=None, index=True)
    title: str
    description: Optional[str] = None
    priority: str = Field(default="normal", index=True)
    status: str = Field(default="pending", index=True)
    created_by_user_id: Optional[str] = Field(default=None, index=True)
    created_by_tl: bool = False
    assignee_agent_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    report_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    review_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # 乐观锁版本号,人改判/验收并发时防覆盖
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TeamTaskEvent(SQLModel, table=True):
    """团队任务审计流水。"""

    __tablename__ = "team_task_events"

    id: str = Field(default_factory=lambda: new_id("team_task_event"), primary_key=True)
    task_id: str = Field(index=True)
    team_id: str = Field(index=True)
    # actor_type: user / agent / system
    actor_type: str = Field(index=True)
    actor_id: Optional[str] = Field(default=None, index=True)
    event_type: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class TeamWakeEvent(SQLModel, table=True):
    """团队唤醒事件:任务派发/报告/退回时唤醒对应成员或 TL 的后台会话。"""

    __tablename__ = "team_wake_events"

    id: str = Field(default_factory=lambda: new_id("team_wake"), primary_key=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    target_agent_id: str = Field(index=True)
    # trigger_type: task_assigned / task_report / task_rework / tl_message 等
    trigger_type: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # status: pending -> claimed -> done / failed
    status: str = Field(default="pending", index=True)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TeamBlackboardEntry(SQLModel, table=True):
    """团队黑板:团队工作记忆,TL 裁决写入/人直写,按团队隔离。"""

    __tablename__ = "team_blackboard_entries"

    id: str = Field(default_factory=lambda: new_id("bbentry"), primary_key=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    content: str
    tags_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # source_type: member(TL 裁决的成员建议) / leader / human(人直写)
    source_type: str = Field(default="human", index=True)
    source_agent_id: Optional[str] = Field(default=None, index=True)
    source_task_id: Optional[str] = Field(default=None, index=True)
    # 引用回链:如 {"task_id": ..., "task_title": ...}
    citation_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # status: active / archived
    status: str = Field(default="active", index=True)
    pinned: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TeamTaskBid(SQLModel, table=True):
    """团队任务竞标:候选成员的方案陈述(round=1)/反驳(round=2)及 TL 打分。"""

    __tablename__ = "team_task_bids"
    __table_args__ = (
        UniqueConstraint("task_id", "agent_id", "round", "kind", name="uq_team_task_bid_round"),
    )

    id: str = Field(default_factory=lambda: new_id("bid"), primary_key=True)
    task_id: str = Field(index=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    round: int = 1
    # kind: statement(方案陈述) / rebuttal(反驳)
    kind: str = Field(default="statement", index=True)
    content: str
    # TL 裁决后回写的分数与理由,未裁决前为 None
    score: Optional[float] = None
    score_rationale: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
