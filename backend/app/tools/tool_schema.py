from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_skills: list[str]
    enabled: bool
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
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    sample_arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeResponse(BaseModel):
    success: bool
    status_code: Optional[int] = None
    data_preview: Optional[Any] = None
    inferred_output_schema: dict[str, Any] = Field(default_factory=dict)
    error: Optional[ToolError] = None
