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
    max_output_tokens: int = 2048
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
    method: str
    url: str
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    auth_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    input_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    allowed_skills_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChatSession(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: Optional[str] = Field(default=None, index=True)
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill_stack_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    resume_after_answer_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    role: str
    content: str
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
