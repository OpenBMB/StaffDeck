from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillCapabilityRefs(BaseModel):
    """Capabilities explicitly exposed while this SOP node is active."""

    general_skill_ids: list[str] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)


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
    capability_refs: SkillCapabilityRefs = Field(default_factory=SkillCapabilityRefs)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillGraphEdge(BaseModel):
    source_node_id: str
    next_node_id: str
    condition: Optional[str] = None
    priority: int = 0
    label: Optional[str] = None


class SkillCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    nodes: list[SkillGraphNode] = Field(default_factory=list)
    edges: list[SkillGraphEdge] = Field(default_factory=list)
    start_node_id: str
    terminal_node_ids: list[str] = Field(default_factory=list)
    interruption_policy: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "SkillCard":
        if not self.nodes:
            raise ValueError("Skill graph requires at least one node.")
        node_ids = [node.node_id for node in self.nodes]
        duplicate_ids = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicate_ids:
            raise ValueError(f"Skill graph node_id must be unique: {', '.join(duplicate_ids)}")
        node_id_set = set(node_ids)
        if self.start_node_id not in node_id_set:
            raise ValueError("start_node_id must reference an existing node.")
        if not self.terminal_node_ids:
            raise ValueError("terminal_node_ids must contain at least one node id.")
        missing_terminal_ids = [node_id for node_id in self.terminal_node_ids if node_id not in node_id_set]
        if missing_terminal_ids:
            raise ValueError(f"terminal_node_ids reference missing nodes: {', '.join(missing_terminal_ids)}")
        for edge in self.edges:
            if edge.source_node_id not in node_id_set:
                raise ValueError(f"edge source_node_id references missing node: {edge.source_node_id}")
            if edge.next_node_id not in node_id_set:
                raise ValueError(f"edge next_node_id references missing node: {edge.next_node_id}")
        return self


class ToolSuggestion(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "技能自发现工具"
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
    agent_id: Optional[str] = None
    branch_status: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    branch_head_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    agent_id: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SkillDistillRequest(BaseModel):
    tenant_id: str
    title: str
    raw_content: str
    business_domain: Optional[str] = None
    model_config_id: Optional[str] = None
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class SkillDistillResponse(BaseModel):
    draft_skill: SkillCard
    warnings: list[str] = Field(default_factory=list)
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)


class SkillRewriteRequest(BaseModel):
    tenant_id: str
    current_skill: SkillCard
    instruction: str
    model_config_id: Optional[str] = None
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
