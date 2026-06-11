from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillStep(BaseModel):
    step_id: str
    name: str
    instruction: str
    expected_user_info: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)


class SkillGraphNode(BaseModel):
    node_id: str
    type: str = "collect_info"
    name: str
    instruction: str = ""
    optional: bool = False
    condition: Optional[str] = None
    expected_user_info: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    knowledge_scope: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillGraphEdge(BaseModel):
    source_node_id: str
    next_node_id: str
    condition: Optional[str] = None
    priority: int = 0
    label: Optional[str] = None


class SkillCard(BaseModel):
    skill_id: str
    name: str
    version: str = "1.0.0"
    business_domain: Optional[str] = None
    description: str = ""
    trigger_intents: list[str] = Field(default_factory=list)
    user_utterance_examples: list[str] = Field(default_factory=list)
    goal: list[str] = Field(default_factory=list)
    required_info: list[str] = Field(default_factory=list)
    slot_filling_policy: dict[str, Any] = Field(default_factory=dict)
    response_rules: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(default_factory=list)
    nodes: list[SkillGraphNode] = Field(default_factory=list)
    edges: list[SkillGraphEdge] = Field(default_factory=list)
    start_node_id: Optional[str] = None
    terminal_node_ids: list[str] = Field(default_factory=list)
    interruption_policy: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_graph_and_steps(self) -> "SkillCard":
        if not self.nodes and self.steps:
            self.nodes = [_step_to_node(step) for step in self.steps]
            self.edges = [
                SkillGraphEdge(
                    source_node_id=self.steps[index].step_id,
                    next_node_id=self.steps[index + 1].step_id,
                    priority=index,
                    label="默认推进",
                )
                for index in range(len(self.steps) - 1)
            ]
            self.start_node_id = self.start_node_id or self.steps[0].step_id
            self.terminal_node_ids = self.terminal_node_ids or [self.steps[-1].step_id]
        elif self.nodes and not self.steps:
            self.steps = [_node_to_step(node) for node in self.nodes]
            self.start_node_id = self.start_node_id or self.nodes[0].node_id
            self.terminal_node_ids = self.terminal_node_ids or [self.nodes[-1].node_id]
        elif self.nodes:
            self.start_node_id = self.start_node_id or self.nodes[0].node_id
            self.terminal_node_ids = self.terminal_node_ids or [self.nodes[-1].node_id]
        return self


def _step_to_node(step: SkillStep) -> SkillGraphNode:
    actions = list(step.allowed_actions)
    node_type = "collect_info" if step.expected_user_info else "response"
    if any(action.startswith("call_tool:") for action in actions):
        node_type = "tool_call"
    if any(action == "handoff_human" for action in actions):
        node_type = "handoff"
    return SkillGraphNode(
        node_id=step.step_id,
        type=node_type,
        name=step.name,
        instruction=step.instruction,
        expected_user_info=list(step.expected_user_info),
        allowed_actions=actions,
    )


def _node_to_step(node: SkillGraphNode) -> SkillStep:
    return SkillStep(
        step_id=node.node_id,
        name=node.name,
        instruction=node.instruction,
        expected_user_info=list(node.expected_user_info),
        allowed_actions=list(node.allowed_actions),
    )


class ToolSuggestion(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    sample_arguments: dict[str, Any] = Field(default_factory=dict)
    source_excerpt: Optional[str] = None
    probe_result: Optional[dict[str, Any]] = None
    reason: str = ""
    resolution_status: Literal["existing", "new_candidate", "incomplete"] = "new_candidate"
    matched_tool_id: Optional[str] = None
    matched_tool_name: Optional[str] = None
    matched_tool_display_name: Optional[str] = None
    missing_reason: Optional[str] = None


class SkillCreateRequest(BaseModel):
    tenant_id: str
    content: SkillCard
    status: Literal["draft", "published", "archived"] = "draft"


class SkillUpdateRequest(BaseModel):
    tenant_id: str
    content: SkillCard
    status: Optional[Literal["draft", "published", "archived"]] = None


class SkillRead(BaseModel):
    id: str
    tenant_id: str
    skill_id: str
    version: str
    name: str
    business_domain: Optional[str]
    description: Optional[str]
    content: SkillCard
    status: str
    call_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    positive_rate: float = 0.0
    negative_rate: float = 0.0
    total_call_count: int = 0
    total_positive_feedback_count: int = 0
    total_negative_feedback_count: int = 0
    total_positive_rate: float = 0.0
    total_negative_rate: float = 0.0
    recent_versions: list[str] = Field(default_factory=list)
    recent_call_count: int = 0
    recent_positive_feedback_count: int = 0
    recent_negative_feedback_count: int = 0
    recent_positive_rate: float = 0.0
    recent_negative_rate: float = 0.0
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SkillVersionRead(BaseModel):
    id: str
    tenant_id: str
    skill_id: str
    version: str
    name: str
    business_domain: Optional[str]
    description: Optional[str]
    content: SkillCard
    status: str
    call_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    positive_rate: float = 0.0
    negative_rate: float = 0.0
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SkillDistillRequest(BaseModel):
    tenant_id: str
    title: str
    raw_content: str
    business_domain: Optional[str] = None
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillDistillResponse(BaseModel):
    draft_skill: SkillCard
    warnings: list[str] = Field(default_factory=list)
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)


class SkillRewriteRequest(BaseModel):
    tenant_id: str
    current_skill: SkillCard
    instruction: str
    target_path: str = "all"
    target_paths: list[str] = Field(default_factory=list)
    target_label: Optional[str] = None
    conversation: list[dict[str, str]] = Field(default_factory=list)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillRewriteResponse(BaseModel):
    draft_skill: SkillCard
    assistant_message: str
    changed_paths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)


class SkillFileExtractRequest(BaseModel):
    filename: str
    content_base64: str


class SkillFileExtractResponse(BaseModel):
    filename: str
    text: str
