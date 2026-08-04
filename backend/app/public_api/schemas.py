from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class CursorPage(BaseModel):
    data: list[Any]
    next_cursor: str | None = None
    request_id: str | None = None


class APIClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(default_factory=lambda: ["agents:read", "runs:*"])
    metadata: dict[str, Any] = Field(default_factory=dict)


class APIClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] | None = None
    status: Literal["active", "archived"] | None = None
    metadata: dict[str, Any] | None = None


class APIClientRead(BaseModel):
    id: str
    name: str
    description: str | None
    scopes: list[str]
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class APICredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["runs:create", "runs:read"])
    agent_id: str | None = None
    expires_at: datetime | None = None


class APICredentialRead(BaseModel):
    id: str
    client_id: str
    agent_id: str | None
    name: str
    key_prefix: str
    scopes: list[str]
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class APICredentialCreated(APICredentialRead):
    api_key: str


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: HttpUrl
    events: list[str] = Field(default_factory=lambda: ["run.*"])


class WebhookRead(BaseModel):
    id: str
    name: str
    url: str
    events: list[str]
    status: str
    secret_masked: str
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    persona_prompt: str | None = None
    source_mode: Literal["copy", "blank"] = "blank"
    copy_from_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    persona_prompt: str | None = None
    status: Literal["active", "archived"] | None = None
    metadata: dict[str, Any] | None = None


class ResourceBindingInput(BaseModel):
    resource_type: Literal["skill", "general_skill", "knowledge_base", "tool"]
    resource_id: str
    status: Literal["active", "inactive"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceBindingsUpdate(BaseModel):
    resources: list[ResourceBindingInput]


class ModelBindingInput(BaseModel):
    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class ModelBindingsUpdate(BaseModel):
    bindings: list[ModelBindingInput]


class PublicSessionCreate(BaseModel):
    external_session_id: str | None = Field(default=None, max_length=200)
    external_user_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublicSessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: Literal["active", "closed"] | None = None
    metadata: dict[str, Any] | None = None


class AgentRunCreate(BaseModel):
    input: str = Field(min_length=1)
    session_id: str | None = None
    session_mode: Literal["stateful", "stateless"] = "stateful"
    external_session_id: str | None = None
    external_user_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    webhook_url: HttpUrl | None = None


class JobRead(BaseModel):
    id: str
    kind: str
    status: str
    stage: str
    progress: float
    agent_id: str | None
    session_id: str | None
    retryable: bool
    error: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class SOPGenerateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    raw_content: str = Field(min_length=1)
    business_domain: str | None = None
    model_config_id: str | None = None


class SOPRewritePublicRequest(BaseModel):
    instruction: str = Field(min_length=1)
    target_paths: list[str] = Field(default_factory=list)
    model_config_id: str | None = None
    draft_id: str | None = None


class SOPStructuredCreate(BaseModel):
    content: dict[str, Any]


class SOPPublishRequest(BaseModel):
    draft_id: str


class KnowledgeEntry(BaseModel):
    external_id: str | None = None
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeEntriesUpsert(BaseModel):
    entries: list[KnowledgeEntry] = Field(min_length=1, max_length=100)


class ScheduledTaskPublicCreate(BaseModel):
    title: str
    prompt: str
    description: str | None = None
    schedule_type: Literal["once", "daily", "weekly", "monthly"] = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: str | None = None
    status: Literal["active", "paused"] = "active"
    concurrency_policy: Literal["forbid", "allow"] = "forbid"
    misfire_policy: Literal["coalesce", "skip"] = "coalesce"
    max_runs: int | None = None
    end_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
