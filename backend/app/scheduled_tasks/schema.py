from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ScheduleType = Literal["once", "daily", "weekly", "monthly", "every_5_min"]
ScheduledTaskStatus = Literal["active", "paused", "completed", "archived"]
ConcurrencyPolicy = Literal["forbid", "allow"]
MisfirePolicy = Literal["coalesce", "skip"]


class ScheduledTaskBase(BaseModel):
    tenant_id: str
    agent_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    status: ScheduledTaskStatus = "active"
    concurrency_policy: ConcurrencyPolicy = "forbid"
    misfire_policy: MisfirePolicy = "coalesce"
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduledTaskCreateRequest(ScheduledTaskBase):
    pass


class ScheduledTaskUpdateRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    schedule_type: Optional[ScheduleType] = None
    schedule: Optional[dict[str, Any]] = None
    timezone: Optional[str] = None
    rrule: Optional[str] = None
    status: Optional[ScheduledTaskStatus] = None
    concurrency_policy: Optional[ConcurrencyPolicy] = None
    misfire_policy: Optional[MisfirePolicy] = None
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ScheduledTaskDraftRequest(BaseModel):
    tenant_id: str
    agent_id: str
    session_id: Optional[str] = None
    message: str
    timezone: Optional[str] = None


class ScheduledTaskDraftRead(BaseModel):
    should_create: bool
    tenant_id: str
    agent_id: str
    title: str = ""
    prompt: str = ""
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None
    source_session_id: Optional[str] = None


class ScheduledTaskRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    created_by_user_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: str
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str
    rrule: Optional[str] = None
    status: str
    concurrency_policy: str
    misfire_policy: str
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    run_count: int
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ScheduledTaskRunRead(BaseModel):
    id: str
    tenant_id: str
    scheduled_task_id: str
    task_title: Optional[str] = None
    task_status: Optional[str] = None
    agent_id: str
    user_id: str
    session_id: Optional[str] = None
    scheduled_for: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result_summary: Optional[str] = None
    error: Optional[str] = None
    trace: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
