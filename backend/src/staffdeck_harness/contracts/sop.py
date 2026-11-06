"""SOP values crossing the lifecycle boundary; independent of persistence."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SopDefinition:
    id: str
    tenant_id: str
    skill_id: str
    version: str
    name: str
    content_json: dict[str, Any]
    status: str = "published"
    description: str = ""
    business_domain: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class SopState:
    id: str
    tenant_id: str
    agent_id: str | None = None
    user_id: str | None = None
    active_skill_id: str | None = None
    active_step_id: str | None = None
    slots_json: dict[str, Any] = field(default_factory=dict)
    pending_tasks_json: list = field(default_factory=list)
    skill_stack_json: list = field(default_factory=list)
    awaiting_input_json: dict | None = None
    resume_after_answer_json: dict | None = None
    last_agent_question: str | None = None
    summary: str | None = None
    status: str = "active"
    updated_at: datetime | None = None
