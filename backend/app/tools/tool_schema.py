from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.capability_scope import CapabilityScope


class ToolExecutionPolicy(BaseModel):
    timeout_seconds: float = Field(ge=1, le=3600)


class ToolCreateRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "未分桶"
    tool_type: Literal["http", "a2a", "mcp"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    execution_policy: Optional[ToolExecutionPolicy] = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_skills: list[str] = Field(default_factory=list)
    capability_scope: CapabilityScope = "general"
    enabled: bool = True


class ToolUpdateRequest(ToolCreateRequest):
    capability_scope: Optional[CapabilityScope] = None


class ToolRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str
    tool_type: str
    method: str
    url: str
    headers: dict[str, Any]
    auth: dict[str, Any]
    mcp_config: dict[str, Any]
    execution_policy: Optional[ToolExecutionPolicy] = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_skills: list[str]
    mcp_server_id: Optional[str] = None
    capability_scope: CapabilityScope
    enabled: bool
    # 员工范围下这是该员工绑定的启用状态；工具广场范围下为 None。
    binding_status: Optional[str] = None
    # 被多少个数字员工引用（不包含开放广场的内部绑定）。
    reference_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ToolBucketRead(BaseModel):
    bucket: str
    total: int
    enabled_count: int
    disabled_count: int
    tool_ids: list[str] = Field(default_factory=list)




class SwaggerParseRequest(BaseModel):
    tenant_id: str
    spec: str = Field(min_length=1)
    default_group: Optional[str] = None
    base_url: Optional[str] = None


class SwaggerToolDraft(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str = ""
    url: str
    group: str = "Swagger 导入"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    request_options: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False


class SwaggerParseResponse(BaseModel):
    success: bool = True
    title: str = ""
    version: str = ""
    base_url: str = ""
    group: str = "Swagger 导入"
    endpoints: list[SwaggerToolDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None


class SwaggerImportRequest(BaseModel):
    tenant_id: str
    group: Optional[str] = None
    endpoints: list[SwaggerToolDraft] = Field(min_length=1)
    enabled: bool = True


class SwaggerImportItemResult(BaseModel):
    name: str
    status: Literal["imported", "skipped", "error"]
    tool_id: Optional[str] = None
    display_name: Optional[str] = None
    error: Optional[str] = None


class SwaggerImportResponse(BaseModel):
    success: bool = True
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[SwaggerImportItemResult] = Field(default_factory=list)
    error: Optional[ToolError] = None


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    code: str
    message: str


class MCPAppDescriptor(BaseModel):
    server_id: str
    resource_uri: str
    tool_name: str
    visibility: list[str] = Field(default_factory=lambda: ["model", "app"])
    mime_type: str = "text/html;profile=mcp-app"
    tenant_id: Optional[str] = None
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    active_skill_id: Optional[str] = None
    initial_result: Optional[Any] = None
    initial_meta: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: Optional[Any] = None
    error: Optional[ToolError] = None
    mcp_app: Optional[MCPAppDescriptor] = None
    mcp_metadata: dict[str, Any] = Field(default_factory=dict)


class ToolTestRequest(BaseModel):
    tenant_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "技能自发现工具"
    tool_type: Literal["http", "a2a", "mcp"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    execution_policy: Optional[ToolExecutionPolicy] = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    sample_arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeResponse(BaseModel):
    success: bool
    status_code: Optional[int] = None
    data_preview: Optional[Any] = None
    inferred_output_schema: dict[str, Any] = Field(default_factory=dict)
    error: Optional[ToolError] = None


MCPTransport = Literal["stdio", "streamable_http", "sse", "builtin"]
MCPAppsMode = Literal["disabled", "auto"]


class MCPServerConnection(BaseModel):
    """MCP Server 连接配置（对齐标准 MCP Client 的连接语义）。"""

    transport: MCPTransport = "streamable_http"
    url: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None
    # 该 server 派生工具的默认调用超时（秒）。None = 沿用 settings.tool_timeout_seconds。
    # 同步工具时会写进每个 Tool 的 config_json.execution；因为 MCP 子工具在界面上
    # 不可单独编辑，慢操作（渲染/转 PDF/图片生成/联网搜索）只能靠这里调大。
    timeout_seconds: Optional[float] = Field(default=None, ge=1, le=3600)


class MCPServerCreateRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "MCP 工具"
    connection: MCPServerConnection = Field(default_factory=MCPServerConnection)
    apps_mode: MCPAppsMode = "disabled"
    capability_scope: CapabilityScope = "general"
    enabled: bool = True


class MCPServerUpdateRequest(MCPServerCreateRequest):
    capability_scope: Optional[CapabilityScope] = None


class MCPDiscoveredTool(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    app: Optional[dict[str, Any]] = None
    # 该工具是否已同步为 Tool 行
    imported: bool = False
    tool_id: Optional[str] = None
    enabled: Optional[bool] = None
    capability_scope: Optional[CapabilityScope] = None


class MCPServerRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str
    connection: MCPServerConnection
    apps_mode: MCPAppsMode = "disabled"
    apps_negotiated: bool = False
    negotiated_capabilities: dict[str, Any] = Field(default_factory=dict)
    capability_scope: CapabilityScope
    enabled: bool
    last_synced_at: Optional[str] = None
    tool_count: int = 0
    created_at: str
    updated_at: str


class MCPDiscoverRequest(BaseModel):
    tenant_id: str
    # 未保存前用连接配置直接探测；已保存则可只传 server_id
    connection: Optional[MCPServerConnection] = None
    apps_mode: MCPAppsMode = "disabled"


class MCPDiscoverResponse(BaseModel):
    success: bool
    tools: list[MCPDiscoveredTool] = Field(default_factory=list)
    server_capabilities: dict[str, Any] = Field(default_factory=dict)
    server_info: dict[str, Any] = Field(default_factory=dict)
    error: Optional[ToolError] = None


class MCPAppResourceRead(BaseModel):
    server_id: str
    uri: str
    mime_type: str
    text: str
    meta: dict[str, Any] = Field(default_factory=dict)


class MCPAppToolCallRequest(BaseModel):
    tenant_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    active_skill_id: Optional[str] = None
    confirm_side_effect: bool = False


class MCPAppToolCallResponse(BaseModel):
    success: bool
    result: Optional[ToolResult] = None
    requires_confirmation: bool = False
    error: Optional[ToolError] = None


class MCPSyncRequest(BaseModel):
    tenant_id: str
    # 需要导入/更新的工具名；为空表示导入全部发现到的工具
    tool_names: Optional[list[str]] = None
    capability_scope_overrides: dict[str, CapabilityScope] = Field(default_factory=dict)


class MCPSyncResponse(BaseModel):
    success: bool
    imported: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None
