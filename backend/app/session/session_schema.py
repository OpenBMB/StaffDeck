from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_schema import ToolCall, ToolResult


RouterDecisionValue = Literal[
    "start_skill",
    "continue_current_skill",
    "jump_within_current_skill",
    "continue_active",
    "switch_to_pending",
    "create_pending",
    "update_pending",
    "complete_task",
    "start_new_task",
    "answer_only",
    "answer_related_question_then_resume",
    "answer_chitchat_then_resume",
    "suspend_current_and_start_new_skill",
    "exit_current_skill",
    "handoff",
    "handoff_human",
    "clarify",
]
MessageFeedbackValue = Literal["up", "down"]


class TaskFrame(BaseModel):
    task_id: Optional[str] = None
    status: str = "pending"
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    intent_summary: Optional[str] = None
    source_turn_id: Optional[str] = None
    source_message: Optional[str] = None
    parent_task_id: Optional[str] = None
    resume_policy: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PendingTask(BaseModel):
    task_id: Optional[str] = None
    status: str = "pending"
    decision: RouterDecisionValue = "start_skill"
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    task_id: str
    status: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    remove: bool = False


class AwaitingInput(BaseModel):
    task_id: Optional[str] = None
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    expected_fields: list[str] = Field(default_factory=list)
    question_summary: Optional[str] = None
    turn_id: Optional[str] = None


class TaskScheduleDecision(BaseModel):
    action: Literal["run_tasks", "stop"] = "stop"
    selected_task_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reason: Optional[str] = None


class RouterDecision(BaseModel):
    decision: RouterDecisionValue
    selected_task_id: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    should_resume_after_answer: bool = False
    clarification_question: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    pending_tasks: list[PendingTask] = Field(default_factory=list)
    task_updates: list[TaskUpdate] = Field(default_factory=list)
    created_tasks: list[PendingTask] = Field(default_factory=list)
    awaiting_input: Optional[AwaitingInput] = None


class KnowledgeQuery(BaseModel):
    query: str
    reason: Optional[str] = None
    scope: dict[str, Any] = Field(default_factory=dict)
    max_chunks: int = 6


class StepAgentResult(BaseModel):
    reply: Optional[str] = None
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    tool_call: Optional[ToolCall] = None
    knowledge_query: Optional[KnowledgeQuery] = None
    knowledge_results: list[dict[str, Any]] = Field(default_factory=list)
    next_step_id: Optional[str] = None
    is_step_completed: bool = False
    handoff: bool = False


class SessionPublic(BaseModel):
    session_id: str
    tenant_id: str
    user_id: Optional[str] = None
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    skill_stack: list[dict[str, Any]] = Field(default_factory=list)
    pending_tasks: list[dict[str, Any]] = Field(default_factory=list)
    resume_after_answer: Optional[dict[str, Any]] = None
    awaiting_input: Optional[dict[str, Any]] = None
    knowledge_context: list[dict[str, Any]] = Field(default_factory=list)
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"


class ChatTurnRequest(BaseModel):
    tenant_id: str
    session_id: Optional[str] = None
    user_id: str = ""
    message: str
    channel: str = "web"
    debug: bool = False


class ChatTurnResponse(BaseModel):
    reply: str
    session_id: str
    router_decision: Optional[RouterDecision] = None
    step_result: Optional[StepAgentResult] = None
    tool_result: Optional[ToolResult] = None
    session_state: SessionPublic


class ChatSessionCreateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    title: Optional[str] = None


class ChatSessionUpdateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    title: str


class ChatSessionRead(BaseModel):
    id: str
    tenant_id: str
    user_id: Optional[str]
    title: Optional[str]
    active_skill_id: Optional[str]
    active_step_id: Optional[str]
    status: str
    summary: Optional[str]
    last_agent_question: Optional[str]
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class MessageRead(BaseModel):
    id: str
    tenant_id: str
    session_id: str
    role: str
    content: str
    created_at: str
    feedback_rating: Optional[MessageFeedbackValue] = None

    model_config = ConfigDict(from_attributes=True)


class MessageFeedbackRequest(BaseModel):
    tenant_id: str
    rating: MessageFeedbackValue
