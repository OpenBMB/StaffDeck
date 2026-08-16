from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TeamRole = Literal["leader", "member"]
ReviewVerdict = Literal["approve", "rework", "escalate"]


class TeamCreateRequest(BaseModel):
    tenant_id: str
    name: str
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class TeamUpdateRequest(BaseModel):
    tenant_id: str
    name: str | None = None
    description: str | None = None
    status: Literal["active", "archived"] | None = None
    config: dict[str, Any] | None = None


class TeamMemberAddRequest(BaseModel):
    tenant_id: str
    agent_id: str
    role: TeamRole = "member"


class TeamLeaderUpdateRequest(BaseModel):
    tenant_id: str
    agent_id: str


class TeamMemberRead(BaseModel):
    id: str
    team_id: str
    agent_id: str
    role: str
    agent_name: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner_user_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    status: str
    members: list[TeamMemberRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamTaskEventRead(BaseModel):
    id: str
    task_id: str
    team_id: str
    actor_type: str
    actor_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamTaskBidRead(BaseModel):
    id: str
    task_id: str
    agent_id: str
    agent_name: str | None = None
    round: int
    kind: str
    content: str
    score: float | None = None
    score_rationale: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamTaskRead(BaseModel):
    id: str
    team_id: str
    tenant_id: str
    parent_task_id: str | None = None
    title: str
    description: str | None = None
    priority: str
    status: str
    created_by_user_id: str | None = None
    created_by_tl: bool
    assignee_agent_id: str | None = None
    session_id: str | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    version: int
    events: list[TeamTaskEventRead] = Field(default_factory=list)
    bids: list[TeamTaskBidRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamTaskCreateRequest(BaseModel):
    tenant_id: str
    title: str
    description: str | None = None
    priority: str = "normal"
    assignee_agent_id: str | None = None


class TeamTLChatRequest(BaseModel):
    tenant_id: str
    message: str
    session_id: str | None = None


class TeamTLChatResponse(BaseModel):
    reply: str
    session_id: str
    created_tasks: list[TeamTaskRead] = Field(default_factory=list)


class TeamTLSessionRequest(BaseModel):
    tenant_id: str


class TeamTLSessionResponse(BaseModel):
    session_id: str


class ReviewOverrideRequest(BaseModel):
    tenant_id: str
    verdict: ReviewVerdict
    comment: str | None = None


class TeamTaskResumeRequest(BaseModel):
    tenant_id: str
    answer: str


class AwardOverrideRequest(BaseModel):
    tenant_id: str
    agent_id: str
    comment: str | None = None


class TeamBlackboardEntryCreateRequest(BaseModel):
    tenant_id: str
    content: str
    tags: list[str] = Field(default_factory=list)


class TeamBlackboardEntryUpdateRequest(BaseModel):
    tenant_id: str
    content: str | None = None
    tags: list[str] | None = None
    pinned: bool | None = None


class TeamBlackboardEntryArchiveRequest(BaseModel):
    tenant_id: str


class TeamBlackboardEntryRead(BaseModel):
    id: str
    team_id: str
    tenant_id: str
    content: str
    tags: list[str] = Field(default_factory=list)
    source_type: str
    source_agent_id: str | None = None
    source_task_id: str | None = None
    citation: dict[str, Any] = Field(default_factory=dict)
    status: str
    pinned: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamBlackboardWriteResponse(BaseModel):
    entries: list[TeamBlackboardEntryRead] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class TeamEventRead(BaseModel):
    """团队级审计事件(全团队 task_events 聚合视图,含任务标题便于展示)。"""

    id: str
    task_id: str
    team_id: str
    task_title: str | None = None
    actor_type: str
    actor_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamThreadRead(BaseModel):
    """跨团队统一线程:TL 对话会话(tl_chat)或任务执行会话(task)。"""

    team_id: str
    team_name: str
    kind: Literal["tl_chat", "task"]
    session_id: str
    task_id: str | None = None
    title: str
    task_status: str | None = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamBlackboardPromoteRequest(BaseModel):
    tenant_id: str


class TeamBlackboardPromoteResponse(BaseModel):
    entry: TeamBlackboardEntryRead
    knowledge_base_id: str
    ingest_job_id: str
    already_promoted: bool = False


TeamConversationKind = Literal["tl_chat", "member_task", "member_bid", "tl_review"]


class TeamConversationTLRead(BaseModel):
    """团队 TL 信息:session_id 为已有 TL 对话会话,无则 None(前端再调 tl/session 创建)。"""

    agent_id: str
    agent_name: str | None = None
    session_id: str | None = None


class TeamConversationRead(BaseModel):
    """团队维度会话条目:kind 由会话标题前缀判定(见 teams.py 注释),preview 为末条消息截取。"""

    session_id: str
    kind: TeamConversationKind
    agent_id: str | None = None
    agent_name: str | None = None
    task_id: str | None = None
    task_status: str | None = None
    needs_input: bool = False
    pending_question: str | None = None
    title: str
    preview: str = ""
    created_at: datetime
    updated_at: datetime


class TeamConversationsResponse(BaseModel):
    team_id: str
    team_name: str
    tl: TeamConversationTLRead | None = None
    conversations: list[TeamConversationRead] = Field(default_factory=list)


class TeamConversationMessageRead(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamConversationStreamRead(BaseModel):
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    content: str = ""
    phase: str | None = None
    updated_at: datetime | None = None
