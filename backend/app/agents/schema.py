from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentProfileCreateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = False
    source_mode: Literal["copy", "blank"] = "copy"
    copy_from_agent_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class AgentResourceBindingRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    resource_type: Literal["skill", "general_skill", "knowledge_base"]
    resource_id: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentProfileRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[AgentResourceBindingRead] = Field(default_factory=list)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentScopeRead(BaseModel):
    tenant_id: str
    agents: list[AgentProfileRead] = Field(default_factory=list)


class AgentResourceBindingInput(BaseModel):
    resource_type: Literal["skill", "general_skill", "knowledge_base"]
    resource_id: str
    status: Literal["active", "inactive"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResourcesUpdateRequest(BaseModel):
    tenant_id: str
    resources: list[AgentResourceBindingInput] = Field(default_factory=list)


class AgentResourceImportRequest(BaseModel):
    tenant_id: str
    source_agent_id: str
    resource_type: Literal["skill", "general_skill", "knowledge_base"]
    resource_ids: list[str] = Field(default_factory=list)


class AgentModelBindingInput(BaseModel):
    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class AgentModelsUpdateRequest(BaseModel):
    tenant_id: str
    bindings: list[AgentModelBindingInput] = Field(default_factory=list)


class AgentSkillRollbackRequest(BaseModel):
    tenant_id: str
    version: str
