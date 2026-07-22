from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolExecutionPolicy(BaseModel):
    timeout_seconds: float = Field(ge=1, le=300)


class ToolCreateRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "未分桶"
    tool_type: Literal["http", "mcp"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    execution_policy: Optional[ToolExecutionPolicy] = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_skills: list[str] = Field(default_factory=list)
    enabled: bool = True


class ToolUpdateRequest(ToolCreateRequest):
    pass


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
    enabled: bool
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


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    code: str
    message: str


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: Optional[Any] = None
    error: Optional[ToolError] = None


class ToolTestRequest(BaseModel):
    tenant_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "技能自发现工具"
    tool_type: Literal["http", "mcp"] = "http"
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


class MCPServerConnection(BaseModel):
    """MCP Server 连接配置（对齐标准 MCP Client 的连接语义）。"""

    transport: MCPTransport = "streamable_http"
    url: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None


class MCPServerCreateRequest(BaseModel):
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "MCP 工具"
    connection: MCPServerConnection = Field(default_factory=MCPServerConnection)
    enabled: bool = True


class MCPServerUpdateRequest(MCPServerCreateRequest):
    pass


class MCPDiscoveredTool(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # 该工具是否已同步为 Tool 行
    imported: bool = False
    tool_id: Optional[str] = None
    enabled: Optional[bool] = None


class MCPServerRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str
    connection: MCPServerConnection
    enabled: bool
    last_synced_at: Optional[str] = None
    tool_count: int = 0
    created_at: str
    updated_at: str


class MCPDiscoverRequest(BaseModel):
    tenant_id: str
    # 未保存前用连接配置直接探测；已保存则可只传 server_id
    connection: Optional[MCPServerConnection] = None


class MCPDiscoverResponse(BaseModel):
    success: bool
    tools: list[MCPDiscoveredTool] = Field(default_factory=list)
    error: Optional[ToolError] = None


class MCPSyncRequest(BaseModel):
    tenant_id: str
    # 需要导入/更新的工具名；为空表示导入全部发现到的工具
    tool_names: Optional[list[str]] = None


class MCPSyncResponse(BaseModel):
    success: bool
    imported: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None
