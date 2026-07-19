from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import Column, JSON, UniqueConstraint
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
    base_url: Optional[str] = None
    api_key_encrypted: str
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
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
    agent_loop_max_actions: int = 6
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
    # 最近一次发现的原始工具定义（预览/审计用）
    discovered_tools_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    last_synced_at: Optional[datetime] = None
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelBinding(SQLModel, table=True):
    __tablename__ = "channel_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "channel", name="uq_channel_binding_agent_channel"),)

    id: str = Field(default_factory=lambda: new_id("chan"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    channel: str = Field(default="wechat", index=True)
    # pending/active/expired/disabled
    status: str = Field(default="pending", index=True)
    # Fernet 加密后的渠道凭证（如微信 bot_token），绝不回传明文
    credentials_enc: Optional[str] = None
    # ilink_bot_id、baseurl、get_updates_buf 游标、session_expired、bound_at 等
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    connected: bool = False
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
    """微信身份自助绑定码:网页端生成,微信侧 /绑定 <码> 核销。"""

    __tablename__ = "channel_bind_codes"

    id: str = Field(default_factory=lambda: new_id("chbc"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    code: str = Field(index=True)
    expires_at: datetime
    used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class ChannelIdentity(SQLModel, table=True):
    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel", "external_user_id", name="uq_channel_identity_external"),)

    id: str = Field(default_factory=lambda: new_id("chident"), primary_key=True)
    tenant_id: str = Field(index=True)
    channel: str = Field(index=True)
    external_user_id: str
    staffdeck_user_id: str = Field(index=True)
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChannelInboundEvent(SQLModel, table=True):
    __tablename__ = "channel_inbound_events"
    __table_args__ = (UniqueConstraint("channel", "event_id", name="uq_channel_inbound_event"),)

    id: str = Field(default_factory=lambda: new_id("chevt"), primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(index=True)
    channel: str = Field(index=True)
    event_id: str
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # received/processing/done/failed
    status: str = Field(default="received", index=True)
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
    last_error: Optional[str] = None
    # 回复类投递 = message_id，天然幂等
    idempotency_key: str = Field(unique=True, index=True)
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
