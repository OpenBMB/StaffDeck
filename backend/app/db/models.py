from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Column, Index, Integer, String, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    id: str = Field(primary_key=True)
    name: str
    # 种子数据指纹:画廊 fixture 内容未变化时跳过启动时的全量重播(启动提速)
    seed_fingerprint: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),
        Index("ix_users_tenant_id_display_name", "tenant_id", "display_name"),
    )

    id: str = Field(default_factory=lambda: new_id("user"), primary_key=True)
    tenant_id: str = Field(index=True)
    username: str = Field(index=True)
    display_name: Optional[str] = None
    # 所属部门:注册/账号管理维护,用于组织维度的用户识别
    department: Optional[str] = None
    # 部门保护标记:在产品里被手动改过(本人或管理员)后,域登录不再覆盖部门
    department_manual: bool = Field(default=False)
    role: str = Field(default="member", index=True)
    # 账号来源:web=网页端创建;wechat 等=渠道懒建(用户管理列表默认隐藏)
    source: str = Field(default="web", index=True)
    # 邮箱:本地注册可为空;域账号登录时由 AD 的 mail 属性同步
    email: Optional[str] = Field(default=None, index=True)
    # 认证方式:local=本地口令;ldap=域账号(由 AD 同步落库)
    auth_source: Optional[str] = None
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
    execution_owner: Optional[str] = Field(default=None, index=True)
    execution_generation: int = 0
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
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


class A2ATaskRun(SQLModel, table=True):
    """Durable state for outbound A2A calls and locally served A2A tasks."""

    __tablename__ = "a2a_task_runs"

    id: str = Field(default_factory=lambda: new_id("a2arun"), primary_key=True)
    direction: str = Field(default="client", index=True)
    tenant_id: str = Field(index=True)
    tool_id: Optional[str] = Field(default=None, index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    invocation_id: Optional[str] = Field(default=None, index=True)
    endpoint_url: str
    agent_card_url: Optional[str] = None
    protocol_binding: str = "JSONRPC"
    protocol_version: str = "1.0"
    remote_task_id: Optional[str] = Field(default=None, index=True)
    context_id: Optional[str] = Field(default=None, index=True)
    codex_session_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="submitted", index=True)
    request_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    artifacts_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    agent_card_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_event_id: Optional[str] = Field(default=None, index=True)
    cancel_requested: bool = False
    recovery_attempts: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class A2ATaskEvent(SQLModel, table=True):
    __tablename__ = "a2a_task_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_a2a_task_event_sequence"),
    )

    id: str = Field(default_factory=lambda: new_id("a2aevt"), primary_key=True)
    tenant_id: str = Field(index=True)
    run_id: str = Field(index=True)
    sequence: int = Field(index=True)
    external_event_id: Optional[str] = Field(default=None, index=True)
    event_type: str = Field(index=True)
    data_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
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
    delivery_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
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


class SkillEdgeCondition(SQLModel, table=True):
    """SOP 边条件的**结构化编译结果**（离线编译产物，见 app/skills/edge_condition_spec.py）。

    为什么不写回 ``skills.content_json``：SkillCard 的 JSON 会被前端 SkillCard
    类型、SkillEditor 的局部改写来回搬运，边缘字段没有对应的 TS 类型就会被
    静默丢掉。改成按**条件指纹**独立存表：

    - 唯一键 ``(tenant_id, skill_id, condition_fingerprint)``——同一条边条件
      （端点 + 条件原文 + 源节点必填字段）在任何版本下复用同一份编译结果；
    - 条件原文一变指纹就变 → 旧行自然失效，不需要显式清理；
    - ``status`` 三态：``compiled`` 已结构化 / ``llm_judge`` 仍需模型语义判断
      / ``failed`` 编译失败（保留 ``condition`` 自由文本，运行时照旧回落 LLM）。
    """

    __tablename__ = "skill_edge_conditions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "skill_id",
            "condition_fingerprint",
            name="uq_skill_edge_condition_fingerprint",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("edgecond"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    # 最近一次编译时技能所处的版本，仅作参考（复用不受版本约束）
    version: str = "1.0.0"
    source_node_id: str = Field(index=True)
    next_node_id: str
    condition_text: str = ""
    condition_fingerprint: str = Field(index=True)
    # compiled / llm_judge / failed
    status: str = Field(default="compiled", index=True)
    kind: str = ""
    spec_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # preset / llm / manual
    source: str = "llm"
    confidence: float = 1.0
    error: Optional[str] = None
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
    #: 标签分类（办公类 / 财务类 / 人力资源类 / 编程类），技能广场按此下拉过滤。
    category: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="draft", index=True)
    capability_scope: str = Field(default="general", index=True)
    #: 技能包累计下载次数（广场下载 + 免登录分享下载都 +1）。
    download_count: int = Field(default=0)
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


class AttachmentParseJob(SQLModel, table=True):
    """聊天附件云端解析任务（MinerU）。

    PDF 扫描件无文字层，pypdf/extract_document_text 抽不出文本；上传接口对
    PDF 附件自动创建本任务并异步调度（rq 优先、进程内兜底），前端轮询进度。
    解析出的 markdown 写入附件暂存目录（``parsed.md``），turn 物化时直接
    作为 extracted_text_path 提供给模型，避免模型在沙箱里再做一次必然失败的
    文字层抽取。``metadata_json`` 保存重建暂存读取所需的附件指纹
    （filename/content_type/size/sha256）。
    """

    __tablename__ = "attachment_parse_jobs"

    id: str = Field(default_factory=lambda: new_id("pjob"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    attachment_id: str = Field(index=True)
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
    user_id: Optional[str] = Field(default=None, index=True)
    name: str
    provider: str = "openai_compatible"
    api_protocol: str = Field(default="openai_chat_completions", index=True)
    base_url: Optional[str] = None
    api_key_encrypted: str
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # 自定义请求头：{"Header-Name": "value"}，逐请求 merge 进最终出站请求头
    custom_headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
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
    is_intent_recognition: bool = Field(default=False)
    # 全局免费模型:租户内单选(管理员把自己的一条模型标记为全局,成员在免费员工上无自己模型时自动使用,
    # 按「用户×员工×天」计 10 问配额)。置位/清理由 service 层保证租户内唯一。
    is_global_free: bool = False
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GlobalFreeModelUsage(SQLModel, table=True):
    """免费模型每日配额:每用户对每个免费员工每天 FREE_MODEL_DAILY_LIMIT 问。

    count 走 SQL 侧原子自增(对话链路并发安全);(tenant, user, agent, date) 唯一。
    usage_date 用 UTC 日期字符串('YYYY-MM-DD'),避免时区列类型在 PG/SQLite 间的差异。
    """

    __tablename__ = "global_free_model_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "agent_id", "usage_date", name="uq_free_model_usage_day"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("freemodel"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    usage_date: str = Field(index=True)
    count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    updated_at: datetime = Field(default_factory=utc_now)


class ApiKeyApplication(SQLModel, table=True):
    """用户申请 API Key 的审批单:pending -> approved / rejected (管理员操作)。

    api_key 以加密形式存储(api_key_encrypted),仅在申请人自己的视图中明文返回一次;
    api_url 为网关地址(当前为本地 mock,后续对接阿里云 DashScope 时由云侧下发)。
    """

    __tablename__ = "api_key_applications"

    id: str = Field(default_factory=lambda: new_id("apk"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    username: Optional[str] = None
    purpose: Optional[str] = None
    status: str = Field(default="pending", index=True)
    api_key_encrypted: Optional[str] = None
    api_url: Optional[str] = None
    reviewer_user_id: Optional[str] = Field(default=None, index=True)
    reviewer_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # 对接阿里云 AI Gateway 后回填的字段
    gateway_name: Optional[str] = None
    gateway_id: Optional[str] = None
    gateway_url: Optional[str] = None
    quota_limit: Optional[int] = None
    quota_period: Optional[str] = None  # day / week / month
    quota_rule_id: Optional[str] = None
    consumer_id: Optional[str] = None
    consumer_name: Optional[str] = None
    # 审批分配时选定的消费组与配额规则（逻辑关联，便于列表展示与批量改配额）
    consumer_group_id: Optional[str] = None
    consumer_group_name: Optional[str] = None
    quota_rule_name: Optional[str] = None

    # 用量分析缓存（按自然月汇总，降低重复查询阿里云频次）
    used_amount: Optional[int] = None
    usage_month: Optional[str] = None  # YYYY-MM


class ApiKeyConsumerGroup(SQLModel, table=True):
    """消费者组(阿里云 AI Gateway ConsumerGroup)管理。

    对应阿里云 ListConsumerGroups 返回的 ConsumerGroup（csg- 前缀），
    消费者组不可增删改，只能看成员列表。
    """

    __tablename__ = "api_key_consumer_groups"

    id: str = Field(default_factory=lambda: new_id("cg"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    gateway_id: str
    gateway_name: str
    external_consumer_group_id: Optional[str] = None  # 阿里云消费组 ID,如 csg-dab9hgem1hknuvjcvc0g
    external_consumer_id: Optional[str] = None  # [废弃] 旧字段,保留兼容
    consumer_type: str = "AI"
    consumer_count: Optional[int] = None  # 组内消费者数量（云端字段）
    status: str = Field(default="enabled", index=True)
    created_by_user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ApiKeyConsumer(SQLModel, table=True):
    """消费者(阿里云 AI Gateway Consumer)管理。

    对应阿里云消费者（cs- 前缀），可启用/停用、修改配额规则。
    创建消费者的流程：CreateConsumer → 分配 API Key → 加入消费组 → 绑定配额规则。
    """

    __tablename__ = "api_key_consumers"

    id: str = Field(default_factory=lambda: new_id("co"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    gateway_id: str
    gateway_name: str
    external_consumer_id: Optional[str] = None  # 阿里云消费者 ID,如 cs-dabnih6m1hkqvi3fvg70
    external_consumer_group_id: Optional[str] = None  # 所属消费组 ID,如 csg-dab9hgem1hknuvjcvc0g
    consumer_group_name: Optional[str] = None  # 所属消费组名称
    consumer_type: str = "AI"
    deploy_status: Optional[str] = None  # 部署状态
    enable: bool = True  # 是否启用
    status: str = Field(default="enabled", index=True)
    created_by_user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ApiKeyQuotaRule(SQLModel, table=True):
    """配额规则(阿里云 AI Gateway QuotaRule)管理。

    创建配额规则即在阿里云对应网关下创建一个 QuotaRule(external_rule_id);
    可绑定到一个或多个消费者(消费组),审批时选定规则即把该消费组纳入规则限流。
    """

    __tablename__ = "api_key_quota_rules"

    id: str = Field(default_factory=lambda: new_id("qr"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    gateway_id: str
    gateway_name: str
    quota_dimension: str = "token"  # token / credit
    quota_limit: int
    period_type: str = "day"  # day / week / month
    external_rule_id: Optional[str] = None  # 阿里云配额规则 ID,如 qr-mock-001
    subject_type: Optional[str] = "consumer"  # consumer / consumer_group（组粒度 2.1.21+）
    status: str = Field(default="enabled", index=True)
    created_by_user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ApiKeyUsageSnapshot(SQLModel, table=True):
    """配额用量快照（按 消费者 × 配额规则 × 自然月 落库）。

    当月每次查询用量时把实时值 upsert 进快照；历史月份直接读快照，
    不再回源阿里云。中途更换配额规则时按规则维度叠加，换规则不丢当月已用量。

    月内累计语义（2026-09-10 定案）：配额窗口可能在自然月内被重置（云端手动
    重置/窗口对齐变更）。检测到云端值 < 本地当前窗口值时，旧窗口累计归档进
    archived_used_amount（冻结不再变动，=「快照1」），云端新值作为当前窗口
    used_amount（=「快照2」，之后持续更新）。展示值 = 两者之和 = 本自然月
    累计使用量；未发生过重置时 archived 恒为 0，行为与单快照一致。
    """

    __tablename__ = "api_key_usage_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "consumer_id",
            "month",
            "quota_rule_id",
            name="uq_api_key_usage_snapshot_consumer_rule_month",
        ),
        Index("ix_api_key_usage_snapshot_tenant_month", "tenant_id", "month"),
    )

    id: str = Field(default_factory=lambda: new_id("usnap"), primary_key=True)
    tenant_id: str = Field(index=True)
    month: str = Field(index=True)  # YYYY-MM
    consumer_id: str = Field(index=True)  # 阿里云消费者 ID（cs-）
    consumer_name: Optional[str] = None
    gateway_id: Optional[str] = None
    gateway_name: Optional[str] = None
    quota_rule_id: str = Field(index=True)  # 阿里云配额规则 ID（qr-）
    quota_rule_name: Optional[str] = None
    quota_limit: int = 0
    quota_period: Optional[str] = None  # day / week / month
    used_amount: int = 0  # 当前配额窗口用量（「快照2」，重置后从云端新值起算）
    archived_used_amount: int = 0  # 月内窗口重置前的累计用量（「快照1」，冻结）
    reset_count: int = 0  # 月内窗口重置次数；本行有效总配额 = quota_limit × (1 + reset_count)
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
    context_token_budget: int = 32_000
    context_compaction_trigger_ratio: float = 0.70
    context_recent_round_limit: int = 6
    context_long_summary_token_budget: int = 4_000
    context_medium_summary_token_budget: int = 4_000
    context_allowed_roles: list[str] = Field(
        default_factory=lambda: ["user", "assistant"],
        sa_column=Column(JSON),
    )
    context_long_summary_prefix: str = "历史的信息可以被总结为："
    context_medium_summary_prefix: str = "近期的历史信息总结为："
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
    # 被使用次数:每新建一个会话 +1(站内与分享链接访客都计入),员工广场卡片展示。
    # 冗余计数列而非实时 count(sessions) —— 广场列表逐行 count 在远程 PG 上会退化成 N+1。
    usage_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    # 免费模型:管理员在员工广场标记后,成员无自己模型时自动使用租户的全局免费模型
    # (每用户每员工每天 FREE_MODEL_DAILY_LIMIT 问);广场卡片显示「免费」徽标。
    free_model_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
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


class AgentShareLink(SQLModel, table=True):
    """数字员工分享链接:站内生成,访客凭 token 免登录打开对话窗。

    `model_config_id` 是分享时锁定的模型,访客端不提供模型选择,
    对话链路会强制覆盖请求体里的 model_config_id。
    """

    __tablename__ = "agent_share_links"
    __table_args__ = (UniqueConstraint("token", name="uq_agent_share_link_token"),)

    id: str = Field(default_factory=lambda: new_id("agentshare"), primary_key=True)
    token: str = Field(index=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    model_config_id: str = Field(index=True)
    created_by: str = Field(index=True)
    # 空 = 永久有效
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    access_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    last_access_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class GeneralSkillShareLink(SQLModel, table=True):
    """通用技能分享链接:站内生成,访客凭 token 免登录查看描述并下载技能包。

    复用数字员工分享链接的口径：token 全局唯一、可撤销、可过期、有访问计数。
    指向的技能必须仍是 published 状态，否则公开端点一律 404。
    """

    __tablename__ = "general_skill_share_links"
    __table_args__ = (UniqueConstraint("token", name="uq_general_skill_share_token"),)

    id: str = Field(default_factory=lambda: new_id("gsshare"), primary_key=True)
    token: str = Field(index=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    skill_slug: str = Field(index=True)
    created_by: str = Field(index=True)
    # 空 = 永久有效
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    # 创建时缓存的文件清单（path/size）与总大小：公开信息页只读本行，
    # 不再拉 MB 级的 general_skills.skill_files_json 大字段
    files_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    total_bytes: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    access_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    last_access_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class AgentShareVisitor(SQLModel, table=True):
    """分享链接访客:一个浏览器 = 一个虚拟用户,承载独立的多轮上下文。

    `visitor_key` 是服务端签发的不可猜测随机串,前端存 localStorage 后回传,
    据此复用同一条会话线;换浏览器/清存储即得到新上下文。
    """

    __tablename__ = "agent_share_visitors"
    __table_args__ = (UniqueConstraint("visitor_key", name="uq_agent_share_visitor_key"),)

    id: str = Field(default_factory=lambda: new_id("sharevisitor"), primary_key=True)
    share_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    # users 表里的虚拟访客账号 id:作为 sessions.user_id,保证访客之间上下文互不可见
    user_id: str = Field(index=True)
    visitor_key: str = Field(index=True)
    message_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    created_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)


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
    # 该 server 派生工具（Tool.tool_type == "mcp"）的默认调用超时（秒）。
    # None = 沿用全局 settings.tool_timeout_seconds。MCP 子工具在界面上不可单独编辑，
    # 所以这是唯一的入口：远程 MCP 里的慢操作（渲染、转 PDF、图片生成、联网搜索）
    # 远超全局默认的 8 秒，必须能在这里按 server 调大。
    timeout_seconds: Optional[float] = None
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
    # 用户可编辑的接入显示名;为空时前端回退展示渠道类型名
    name: Optional[str] = Field(default=None)
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


class ChannelBindingManager(SQLModel, table=True):
    """渠道绑定协作者:创建者/admin 显式授权的非创建者,可凭证/挂载/启停但不能删除。

    同一 (binding, user) 仅一行;移除即软撤销(revoked_at),重新添加复活该行,
    保留最近一次授权/撤销记录用于审计。删除渠道绑定级联清空协作者行。
    """

    __tablename__ = "channel_binding_managers"
    __table_args__ = (
        UniqueConstraint("binding_id", "user_id", name="uq_channel_binding_manager"),
        Index("ix_channel_binding_managers_tenant_user", "tenant_id", "user_id"),
    )

    id: str = Field(default_factory=lambda: new_id("chbm"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    user_id: str = Field(index=True)
    granted_by_user_id: str
    granted_at: datetime = Field(default_factory=utc_now)
    revoked_at: Optional[datetime] = None


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
    # 路由指针版本；自动路由分类完成后必须以此做 CAS，避免覆盖期间的手动切换。
    routing_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
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
    processor_lease_expires_at: Optional[datetime] = Field(default=None, index=True)
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
    # 每次领取投递都会生成新的 owner 并递增 generation；旧 worker 的迟到结果不得落库。
    delivery_owner: Optional[str] = Field(default=None, index=True)
    delivery_generation: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
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
    # 飞书 handoff_notice 投递成功后回写的飞书 message_id;阶段 4 据此关联处理人回复。
    # 网页触发的 handoff 无此字段(为空),不影响现有网页回复链路。
    notify_message_id: Optional[str] = Field(default=None, index=True)


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


class HarnessAgentLoopRecord(SQLModel, table=True):
    """Durable logical AgentLoop shared across Harness activations."""

    __tablename__ = "harness_agent_loops"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "loop_key", name="uq_harness_agent_loop_session_key"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("hloop"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    loop_key: str = Field(index=True)
    kind: str = Field(default="general", index=True)
    status: str = Field(default="active", index=True)
    owner_task_frame_record_id: Optional[str] = Field(default=None, index=True)
    skill_id: Optional[str] = Field(default=None, index=True)
    workspace_scope_id: Optional[str] = Field(default=None, index=True)
    checkpoint_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_run_id: Optional[str] = Field(default=None, index=True)
    state_version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None


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
    agent_loop_id: Optional[str] = Field(default=None, index=True)
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
    agent_loop_id: Optional[str] = Field(default=None, index=True)
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


class EvolutionProposal(SQLModel, table=True):
    __tablename__ = "evolution_proposals"

    id: str = Field(default_factory=lambda: new_id("evo"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    resource_type: str = Field(index=True)
    resource_id: str = Field(index=True)
    resource_key: str = Field(index=True)
    resource_name: str
    base_version: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="ready_for_review", index=True)
    trigger_type: str = Field(default="feedback", index=True)
    risk_level: str = Field(default="medium", index=True)
    hypothesis: str = ""
    rationale: str = ""
    expected_outcome: str = ""
    source_feedback_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    evidence_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    candidate_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    diff_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    evaluation_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    published_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: Optional[str] = None
    created_by_user_id: str = Field(index=True)
    reviewed_by_user_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    reviewed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None


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


class TeamRun(SQLModel, table=True):
    """One durable TL plan from delegation through final team synthesis."""

    __tablename__ = "team_runs"
    __table_args__ = (
        UniqueConstraint(
            "tl_session_id",
            "source_turn_id",
            name="uq_team_run_tl_session_source_turn",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("team_run"), primary_key=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    tl_session_id: str = Field(index=True)
    source_turn_id: str = Field(index=True)
    created_by_user_id: Optional[str] = Field(default=None, index=True)
    # planning -> running/awaiting_input -> synthesizing -> completed/failed
    status: str = Field(default="planning", index=True)
    synthesis_session_id: Optional[str] = Field(default=None, index=True)
    final_message_id: Optional[str] = Field(default=None, index=True)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None


class TeamTask(SQLModel, table=True):
    """团队任务:blocked -> pending -> in_progress -> review -> done/rework/escalated;

    rework -> in_progress 重入;pending -> bidding -> pending 为任务池竞标链路。
    """

    __tablename__ = "team_tasks"

    id: str = Field(default_factory=lambda: new_id("team_task"), primary_key=True)
    team_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    team_run_id: Optional[str] = Field(default=None, index=True)
    source_turn_id: Optional[str] = Field(default=None, index=True)
    parent_task_id: Optional[str] = Field(default=None, index=True)
    title: str
    description: Optional[str] = None
    priority: str = Field(default="normal", index=True)
    status: str = Field(default="pending", index=True)
    created_by_user_id: Optional[str] = Field(default=None, index=True)
    created_by_tl: bool = False
    assignee_agent_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    depends_on_task_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    activation_condition_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
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


# ---------------------------------------------------------------------------
# AI Reviewer（代码评审）：平台凭证 / 工作区 / 全局 review 要求 / 异步评审任务
# 整体是「提交任务 → rq 后台跑 ocr CLI → 回写结果 → 用户在任务列表看结果」的
# 异步模型；PR/MR 快照在建任务时落库，ocr 不在请求路径上。
# ---------------------------------------------------------------------------


class AiReviewCredential(SQLModel, table=True):
    """平台访问凭证（**租户级全局**）：拉取 PR/MR 列表与详情用。

    一个租户每个平台最多一条（GitHub / GitLab 各一）。token 以明文落库，
    API 层只对外回 ``token_last4``；GitLab 支持自托管，所以保留 ``base_url``。
    """

    __tablename__ = "ai_review_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "platform", name="uq_ai_review_credential_platform"),
    )

    id: str = Field(default_factory=lambda: new_id("airevcred"), primary_key=True)
    tenant_id: str = Field(index=True)
    # github / gitlab
    platform: str = Field(index=True)
    # GitLab 自托管时填实例根地址（https://gitlab.example.com）；github 侧忽略
    base_url: str = ""
    token: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AiReviewWorkspace(SQLModel, table=True):
    """一个 workspace = 一个待评审仓库（按 repo 区分），挂平台与克隆地址。"""

    __tablename__ = "ai_review_workspaces"

    id: str = Field(default_factory=lambda: new_id("airevws"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    # github / gitlab（决定用哪张全局凭证与哪套 REST 调用）
    platform: str = Field(index=True)
    # https 克隆地址，如 https://gitlab.example.com/group/repo.git
    repo_url: str
    # 平台展示路径，如 group/repo（列表接口要用）
    repo_path: str = ""
    default_branch: str = "main"
    created_by: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class AiReviewPreset(SQLModel, table=True):
    """全局「review 要求」预设：提交任务时可附加，作为公共评审口径。"""

    __tablename__ = "ai_review_presets"

    id: str = Field(default_factory=lambda: new_id("airevpreset"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    content: str
    # 每个租户至多一条 default：新建任务时用它预填
    is_default: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AiReviewRuleFile(SQLModel, table=True):
    """租户级全局自定义评审规则（对应 ocr 的 ``rule.json``）。

    执行时以 ``ocr review --rule <file>`` 注入，属于四层规则链里**最高优先级**
    （高于项目/全局配置文件与系统内置规则）。结构：
    ``{"include": [...], "exclude": [...], "rules": [{"path", "rule", "merge_system_rule"}]}``。
    一个租户至多一份（唯一约束）。
    """

    __tablename__ = "ai_review_rule_files"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_ai_review_rule_file_tenant"),
    )

    id: str = Field(default_factory=lambda: new_id("airevrule"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str = ""
    content: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AiReviewTask(SQLModel, table=True):
    """一次异步评审任务：建任务时冻结 PR/MR 快照，ocr 结果回写 ``result_json``。

    ``status`` 四态：queued（已入队）→ running（ocr 执行中）→ succeeded / failed。
    全部字段在**建任务时**固化（标题、描述、分支、作者），后续 MR 更新不影响
    本次评审的口径； ``result_json`` 存 ocr 的 comments 数组。
    """

    __tablename__ = "ai_review_tasks"
    # 同一 PR/MR 同时只允许一条进行中任务（部分唯一索引，PG 与 SQLite 都支持
    # WHERE 子句）。应用层在创建 / 重试时已拦一道，这里是并发写入的兜底；
    # 已存在的表由 database._migrate_ai_review_schema() 补建同名索引。
    __table_args__ = (
        Index(
            "uq_ai_review_task_in_flight",
            "tenant_id",
            "workspace_id",
            "mr_number",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: str = Field(default_factory=lambda: new_id("airevtask"), primary_key=True)
    tenant_id: str = Field(index=True)
    workspace_id: str = Field(index=True)
    # queued / running / succeeded / failed
    status: str = Field(default="queued", index=True)
    # ---- PR/MR 快照（建任务时冻结）----
    mr_number: int = 0
    mr_title: str = ""
    mr_description: str = ""
    source_branch: str = ""
    target_branch: str = ""
    author: str = ""
    web_url: str = ""
    # ---- 用户附加的评审要求（快照，预设后续改动不影响已建任务）----
    requirements: str = ""
    # ---- 执行结果 ----
    result_json: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    summary_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str = ""
    # ---- 结果回写平台评论区（查看报告时可把意见同步到 PR/MR）----
    platform_synced_at: Optional[datetime] = None
    platform_sync_url: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=utc_now, index=True)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class PlazaNewsCache(SQLModel, table=True):
    """开放广场首页新闻的每日缓存。

    同一租户同一天只抓取一次 RSS 源（tenant_id + cache_date 唯一）；
    抓取失败时回退读最近一天缓存，再退到 API 内置种子新闻。
    """

    __tablename__ = "plaza_news_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cache_date", name="uq_plaza_news_tenant_date"),
    )

    id: str = Field(default_factory=lambda: new_id("pnews"), primary_key=True)
    tenant_id: str = Field(index=True)
    # 缓存归属日期（YYYY-MM-DD，租户本地时区语义上按「天」粒度即可）
    cache_date: str = Field(index=True)
    # {"ai": [item...], "travel": [item...]}，item: {title, summary, source, url, published_at, category}
    items_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # provenance: rss / fallback（种子兜底）
    provenance: str = Field(default="rss")
    updated_at: datetime = Field(default_factory=utc_now)


class AppUsageCounter(SQLModel, table=True):
    """「本地清单类」应用的使用计数（如 OA 单据助手 / AI 单据助手）。

    Agent 广场里的定制 Agent（agent-catalog.json）是**前端本地清单、不落库**，
    没有 agent_profiles 行，也就没有 usage_count；但首页热门排行榜要统计它们。
    这里给这类应用一个全局计数器：浏览器书签面板每次代填成功 +1。

    app_key 全局唯一（不按租户分）：面板注入在用户的 OA 页面里、不带租户身份，
    而这个应用对所有租户都是同一个。
    """

    __tablename__ = "app_usage_counters"

    id: str = Field(default_factory=lambda: new_id("appuse"), primary_key=True)
    app_key: str = Field(sa_column=Column(String, nullable=False, unique=True))
    usage_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    updated_at: datetime = Field(default_factory=utc_now)
