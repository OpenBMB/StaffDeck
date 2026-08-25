from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db.models import ChatSession, Skill
from app.session.session_schema import PlannedTaskFrame


CapabilityKind = Literal[
    "general_skill",
    "knowledge",
    "tool",
    "file",
    "internal",
]


class CapabilityDescriptor(BaseModel):
    capability_id: str
    name: str
    kind: CapabilityKind
    capability_scope: Literal["general", "sop_specific"] = "general"
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    available: bool = True
    unavailable_reason: str | None = None


class CapabilityCatalogEntry(BaseModel):
    capability_id: str
    name: str
    kind: CapabilityKind
    capability_scope: Literal["general", "sop_specific"] = "general"
    description: str = ""


class CapabilityManifest(BaseModel):
    available: list[CapabilityDescriptor] = Field(default_factory=list)
    catalog: list[CapabilityCatalogEntry] = Field(default_factory=list)
    catalog_total: int = 0
    catalog_truncated: bool = False
    catalog_budget_chars: int = 8_000
    unavailable_references: list[CapabilityDescriptor] = Field(default_factory=list)
    snapshot_revision: str = ""

    def allowed_names(self) -> set[str]:
        return {
            item.name for item in self.available if item.available and str(item.name or "").strip()
        }


class TaskRequirement(BaseModel):
    task_frame_id: str
    kind: Literal["sop", "conversation"]
    goal: str
    source_user_message: str = ""
    out_of_scope_task_intents: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    sop_context: dict[str, Any] = Field(default_factory=dict)
    required_slots: list[str] = Field(default_factory=list)
    known_slots: dict[str, Any] = Field(default_factory=dict)
    completion_criteria: list[str] = Field(default_factory=list)
    required_capability_names: list[str] = Field(default_factory=list)
    required_knowledge_base_ids: list[str] = Field(default_factory=list)
    allowed_transitions: list[dict[str, Any]] = Field(default_factory=list)
    memory_projection: list[dict[str, str]] = Field(default_factory=list)
    prior_task_results: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    published_deliverables: list[dict[str, Any]] = Field(default_factory=list)
    capability_manifest: CapabilityManifest = Field(default_factory=CapabilityManifest)


class TaskExecutionResult(BaseModel):
    task_frame_id: str
    status: Literal[
        "completed",
        "awaiting_user",
        "handoff",
        "failed",
        "blocked",
        "action_budget",
    ]
    reply_fragment: str = ""
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    next_step_id: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_results: list[dict[str, Any]] = Field(default_factory=list)
    capability_results: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    task_summary: str = ""
    action_count: int = 0
    error: dict[str, Any] | None = None
    structured_result: Any | None = None
    loop_checkpoint: dict[str, Any] = Field(default_factory=dict, exclude=True)


class TaskRequestCompiler:
    """Compiles one persisted frame and the current SOP node into one task."""

    def compile(
        self,
        frame: PlannedTaskFrame,
        session: ChatSession,
        skill: Skill | None,
        manifest: CapabilityManifest,
        memory_context: list[dict[str, object]] | None = None,
        prior_task_results: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        published_deliverables: list[dict[str, Any]] | None = None,
        source_user_message: str | None = None,
        out_of_scope_task_intents: list[str] | None = None,
    ) -> TaskRequirement:
        current_node = _current_node(skill, frame.target_step_id or session.active_step_id)
        expected_fields = _text_list((current_node or {}).get("expected_user_info"))
        known_slots = (
            {
                str(key): value
                for key, value in dict(session.slots_json or {}).items()
                if value not in (None, "", [], {})
            }
            if frame.kind == "sop"
            else {}
        )
        required_slots = [
            field for field in expected_fields if not _slot_satisfied(known_slots.get(field))
        ]
        requirements = _unique(
            [
                str((current_node or {}).get("instruction") or "").strip(),
                ("补齐以下字段：" + "、".join(required_slots) if required_slots else ""),
                *frame.requirements,
            ]
        )
        goal = _goal(frame, skill, current_node, requirements)
        completion_criteria = _unique(
            [
                (
                    "收集并确认当前步骤要求的字段：" + "、".join(expected_fields)
                    if expected_fields
                    else ""
                ),
                *(_text_list((skill.content_json or {}).get("goal")) if skill is not None else []),
                "完整处理 TaskRequirement 中的全部子需求。",
            ]
        )
        required_capability_names, required_knowledge_base_ids = _required_step_capabilities(
            current_node,
            manifest,
        )
        if required_capability_names:
            completion_criteria = _unique(
                [
                    *completion_criteria,
                    "成功调用当前 SOP 节点标记为强制执行的能力："
                    + "、".join(required_capability_names),
                ]
            )
        if required_knowledge_base_ids:
            completion_criteria = _unique(
                [
                    *completion_criteria,
                    "检索当前 SOP 节点要求的知识库：" + "、".join(required_knowledge_base_ids),
                ]
            )
        return TaskRequirement(
            task_frame_id=str(frame.task_id or ""),
            kind=frame.kind,
            goal=goal,
            source_user_message=str(source_user_message or "").strip()[:4_000],
            out_of_scope_task_intents=_unique(
                [str(item or "") for item in out_of_scope_task_intents or []]
            ),
            requirements=requirements or [goal],
            sop_context=_sop_context(skill, current_node),
            required_slots=required_slots,
            known_slots=known_slots,
            completion_criteria=completion_criteria,
            required_capability_names=required_capability_names,
            required_knowledge_base_ids=required_knowledge_base_ids,
            allowed_transitions=_transitions(skill, current_node),
            memory_projection=_memory_projection(memory_context),
            prior_task_results=list(prior_task_results or []),
            attachments=list(attachments or []),
            published_deliverables=list(published_deliverables or []),
            capability_manifest=manifest,
        )


def current_step_capability_refs(skill: Skill | None, step_id: str | None) -> dict[str, list[str]]:
    node = _current_node(skill, step_id)
    refs = (node or {}).get("capability_refs")
    result = {
        "general_skill_ids": [],
        "tool_ids": [],
        "knowledge_base_ids": [],
    }
    if isinstance(refs, dict):
        for key in result:
            result[key] = _text_list(refs.get(key))

    # Backward compatibility while old SOP content is migrated.
    for action in _text_list((node or {}).get("allowed_actions")):
        if action.startswith("call_tool:"):
            tool_ref = action.partition(":")[2].strip()
            if tool_ref and tool_ref not in result["tool_ids"]:
                result["tool_ids"].append(tool_ref)
    knowledge_scope = (node or {}).get("knowledge_scope")
    if isinstance(knowledge_scope, dict):
        for kb_id in _text_list(knowledge_scope.get("knowledge_base_ids")):
            if kb_id not in result["knowledge_base_ids"]:
                result["knowledge_base_ids"].append(kb_id)
    return result


def _current_node(skill: Skill | None, step_id: str | None) -> dict[str, Any] | None:
    if skill is None:
        return None
    content = skill.content_json or {}
    resolved_step_id = str(step_id or content.get("start_node_id") or "").strip()
    for node in content.get("nodes") or content.get("steps") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id") or node.get("step_id") or "").strip()
        if node_id and node_id == resolved_step_id:
            return node
    return None


def _required_step_capabilities(
    current_node: dict[str, Any] | None,
    manifest: CapabilityManifest,
) -> tuple[list[str], list[str]]:
    if not current_node:
        return [], []
    refs = current_node.get("capability_refs")
    if not isinstance(refs, dict):
        return [], []
    required_tool_refs = set(_text_list(refs.get("required_tool_ids")))
    required_skill_refs = set(_text_list(refs.get("required_general_skill_ids")))
    required_knowledge_base_ids = _text_list(refs.get("required_knowledge_base_ids"))
    required: list[str] = []
    for descriptor in manifest.available:
        if not descriptor.available:
            continue
        matches_tool = descriptor.kind == "tool" and (
            descriptor.capability_id in required_tool_refs or descriptor.name in required_tool_refs
        )
        matches_skill = descriptor.kind == "general_skill" and (
            descriptor.capability_id in required_skill_refs
            or descriptor.name in required_skill_refs
        )
        if matches_tool or matches_skill:
            required.append(descriptor.name)
    if required_knowledge_base_ids:
        required.append("knowledge_search")
    return _unique(required), required_knowledge_base_ids


def _transitions(skill: Skill | None, current_node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if skill is None or current_node is None:
        return []
    node_id = str(current_node.get("node_id") or current_node.get("step_id") or "").strip()
    transitions: list[dict[str, Any]] = []
    for edge in (skill.content_json or {}).get("edges") or []:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source_node_id") or "").strip() != node_id:
            continue
        transitions.append(
            {
                key: edge.get(key)
                for key in ("next_node_id", "condition", "label", "priority")
                if edge.get(key) not in (None, "")
            }
        )
    return transitions


def _goal(
    frame: PlannedTaskFrame,
    skill: Skill | None,
    current_node: dict[str, Any] | None,
    requirements: list[str],
) -> str:
    node_name = str((current_node or {}).get("name") or "").strip()
    skill_name = str(getattr(skill, "name", "") or "").strip()
    if frame.kind == "sop" and (node_name or skill_name):
        return f"完成 {skill_name or 'SOP'} 的{node_name or '当前步骤'}。"
    if requirements:
        return requirements[0]
    return str(frame.user_intent or "完成用户本轮请求。").strip()


def _sop_context(skill: Skill | None, current_node: dict[str, Any] | None) -> dict[str, Any]:
    if skill is None:
        return {}
    return {
        "skill_id": skill.skill_id,
        "skill_name": skill.name,
        "step": current_node or {},
    }


def _memory_projection(
    memory_context: list[dict[str, object]] | None,
) -> list[dict[str, str]]:
    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in memory_context or []:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split()).strip()
        if not content or content in seen:
            continue
        seen.add(content)
        projected.append(
            {
                "kind": str(item.get("kind") or "memory"),
                "content": content[:1_000],
            }
        )
    return projected


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _unique([str(item or "").strip() for item in value])


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split()).strip()
        if text and text not in result:
            result.append(text)
    return result


def _slot_satisfied(value: object) -> bool:
    return value not in (None, "", [], {})
