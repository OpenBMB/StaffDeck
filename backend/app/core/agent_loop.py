from __future__ import annotations

import json
import queue
import re
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import sleep
from types import SimpleNamespace
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    model_for_agent,
    visible_knowledge_base_ids,
    visible_published_skills,
    visible_skill,
    visible_tool_rows,
)
from app.core.conversation_context import build_conversation_context
from app.core.cancellation import clear_chat_turn_cancelled, is_chat_turn_cancelled
from app.core.reflection_agent import ReflectionAgent, ReflectionDecision, action_needs_reflection
from app.core.response_generator import (
    FALLBACK_REPLY,
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    HumanHandoffRequest,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    Tool,
    UIConfig,
    User,
    new_id,
    utc_now,
)
from app.general_skills import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import GeneralSkillRunResponse, GeneralSkillSelection
from app.knowledge import KnowledgeService
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    knowledge_citations_from_results,
)
from app.knowledge.schema import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    resolve_model_config_for_runtime,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService, memory_read
from app.observability import EventLog
from app.session.attachments import (
    message_content_with_attachment_context,
    message_images_from_metadata,
)
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    KnowledgeQuery,
    PendingTask,
    RouterDecision,
    StepAgentResult,
)
from app.tools import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


StatusCallback = Callable[[str, dict[str, object]], None]
STREAM_CHUNK_INTERVAL_SECONDS = 0.045
DEFAULT_REFLECTION_MAX_ROUNDS = 1
REFLECTION_MAX_ROUNDS_LIMIT = 5
MAX_TOOL_ACTIONS_PER_TURN = 6
TOOL_CALL_HISTORY_SLOT = "_tool_call_history"
TOOL_RESULTS_SLOT = "_tool_results"
GRAPH_PENDING_STEPS_SLOT = "_graph_pending_steps"
GENERAL_SKILL_TOOL_PREFIX = "general_skill."
CANCELLED_ASSISTANT_REPLY = "已停止生成"
IDEMPOTENT_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ERROR_TRACEBACK_CHAR_LIMIT = 6000
AGENT_PERSONA_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("role_name", "岗位"),
    ("position", "岗位"),
    ("job_title", "岗位"),
    ("role", "角色"),
    ("title", "职务"),
    ("department", "部门"),
    ("team", "团队"),
    ("work_styles", "工作风格"),
    ("expertise_tags", "擅长领域"),
    ("work_modes", "工作方式"),
)

ExecutionFinalizeState = Literal["continued", "completed", "handoff"]


def _agent_identity_prompt(agent: AgentProfile) -> str:
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    lines = [
        "你正在扮演一个企业数字员工。请始终以该员工的身份、岗位和职责口径回复用户，不要自称其他员工。",
        f"员工名称：{_single_line_text(agent.name)}",
    ]
    description = _single_line_text(agent.description)
    if description:
        lines.append(f"员工描述：{description}")
    seen_labels: set[str] = set()
    for key, label in AGENT_PERSONA_METADATA_FIELDS:
        value = _metadata_prompt_text(metadata.get(key))
        if not value:
            continue
        if label in seen_labels and label in {"岗位"}:
            continue
        seen_labels.add(label)
        lines.append(f"{label}：{value}")
    persona = str(agent.persona_prompt or "").strip()
    if persona:
        lines.append("")
        lines.append("员工角色补充要求：")
        lines.append(persona)
    return "\n".join(lines)


def _metadata_prompt_text(value: object) -> str:
    if isinstance(value, str):
        return _single_line_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        items = [_single_line_text(item) for item in value]
        return "、".join(item for item in items if item)
    return ""


def _single_line_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_action(action: object) -> str:
    text = str(action or "").strip().strip("`'\"").strip()
    if not text:
        return ""
    if text.startswith("call_tool:"):
        tool_name = text.split(":", 1)[1].strip().strip("`'\"").strip()
        return f"call_tool:{tool_name}" if tool_name else ""
    return text


def _slot_has_value(slots: dict[str, Any], field: str) -> bool:
    value = slots.get(field)
    return value is not None and value != "" and value != []


def _skill_expected_fields(skill: Skill) -> set[str]:
    content = skill.content_json or {}
    fields: set[str] = set()
    required_info = content.get("required_info")
    if isinstance(required_info, list):
        fields.update(str(item) for item in required_info if str(item).strip())
    nodes = content.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            expected = node.get("expected_user_info")
            if isinstance(expected, list):
                fields.update(str(item) for item in expected if str(item).strip())
    return fields


def _profile_name_from_memory(memory_context: list[dict[str, object]]) -> str:
    for memory in memory_context:
        if memory.get("kind") != "profile":
            continue
        metadata = memory.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        content = str(memory.get("content") or "").strip()
        if key != "preferred_name":
            continue
        if content:
            return content[:40]
    return ""


def _node_as_step(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": node.get("node_id"),
        "node_id": node.get("node_id"),
        "type": node.get("type"),
        "name": node.get("name"),
        "instruction": node.get("instruction"),
        "optional": node.get("optional", False),
        "condition": node.get("condition"),
        "expected_user_info": node.get("expected_user_info") or [],
        "allowed_actions": node.get("allowed_actions") or [],
        "knowledge_scope": node.get("knowledge_scope") or {},
        "retry_policy": node.get("retry_policy") or {},
        "metadata": node.get("metadata") or {},
    }


class AgentLoopPreconditionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PreparedTurn:
    chat_session: ChatSession
    model_config: ModelConfig
    active_skill: Skill | None
    router_decision: RouterDecision
    step_result: StepAgentResult
    tool_result: ToolResult | None
    memory_context: list[dict[str, object]]
    conversation_context: dict[str, object]
    general_response: ChatTurnResponse | None = None
    reply_override: str | None = None
    user_message_id: str | None = None


@dataclass
class QueuedTaskContinuation:
    reply: str
    task_results: list[dict[str, object]]
    active_skill: Skill | None
    router_decision: RouterDecision
    step_result: StepAgentResult
    tool_result: ToolResult | None


class AgentLoop:
    def __init__(self, db: Session):
        self.db = db
        self.events = EventLog(db)
        self.router = Router()
        self.runtime = SkillRuntime()
        self.step_agent = StepAgent()
        self.reflection_agent = ReflectionAgent()
        self.response_generator = ResponseGenerator()
        self.general_skill_selector = GeneralSkillSelector()
        self.general_skill_runner = GeneralSkillRunner()
        self.tool_executor = ToolExecutor(db)
        self.memory = MemoryService(db)
        self._validated_general_skill_calls: set[tuple[str, str, str]] = set()

    def _turn_payload(self, payload: dict[str, Any], user_message_id: str | None) -> dict[str, Any]:
        data = dict(payload)
        if user_message_id:
            data.setdefault("user_message_id", user_message_id)
            data.setdefault("turn_id", user_message_id)
        return data

    def _hydrate_router_decision_from_context(
        self,
        chat_session: ChatSession,
        router_decision: RouterDecision,
        skills: list[Skill],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        skills_by_id = {skill.skill_id: skill for skill in skills}
        hydrated: dict[str, Any] = {}

        target_skill = skills_by_id.get(
            router_decision.target_skill_id or chat_session.active_skill_id or ""
        )
        base_slots = dict(chat_session.slots_json or {})
        base_slots.update(dict(router_decision.slot_hints or {}))
        patch = self._slot_hydration_patch(target_skill, base_slots, memory_context)
        if patch:
            router_decision.slot_hints = {**dict(router_decision.slot_hints or {}), **patch}
            hydrated["primary"] = patch
        remaining_awaiting = self._trim_satisfied_awaiting_fields(
            router_decision, {**base_slots, **patch}
        )
        if remaining_awaiting is not None:
            hydrated["awaiting_input_expected_fields"] = remaining_awaiting

        task_patches: list[dict[str, Any]] = []
        for task in [
            *router_decision.task_frames,
            *router_decision.pending_tasks,
            *router_decision.created_tasks,
        ]:
            task_skill = skills_by_id.get(task.target_skill_id or "")
            task_slots = dict(task.slot_hints or {})
            task_patch = self._slot_hydration_patch(task_skill, task_slots, memory_context)
            if task_patch:
                task.slot_hints = {**task_slots, **task_patch}
                task_patches.append(
                    {
                        "task_id": task.task_id,
                        "target_skill_id": task.target_skill_id,
                        "slots": task_patch,
                    }
                )
        if task_patches:
            hydrated["tasks"] = task_patches
        return hydrated

    def _slot_hydration_patch(
        self,
        skill: Skill | None,
        slots: dict[str, Any],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        if not skill:
            return {}
        expected_fields = _skill_expected_fields(skill)
        patch: dict[str, Any] = {}
        if "user_name" in expected_fields and not _slot_has_value(slots, "user_name"):
            profile_name = _profile_name_from_memory(memory_context)
            if profile_name:
                patch["user_name"] = profile_name
        return patch

    def _trim_satisfied_awaiting_fields(
        self, router_decision: RouterDecision, slots: dict[str, Any]
    ) -> list[str] | None:
        if not router_decision.awaiting_input:
            return None
        original = list(router_decision.awaiting_input.expected_fields)
        remaining = [
            field
            for field in router_decision.awaiting_input.expected_fields
            if not _slot_has_value(slots, field)
        ]
        if remaining == original:
            return None
        if remaining:
            router_decision.awaiting_input.expected_fields = remaining
        else:
            router_decision.awaiting_input = None
        return remaining

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        memory_model_config: ModelConfig | None = None
        prepared_user_message_id: str | None = None
        try:
            prepared = self._prepare_turn(request)
            prepared_user_message_id = prepared.user_message_id
            if prepared.general_response:
                return prepared.general_response
            chat_session = prepared.chat_session
            memory_model_config = prepared.model_config
            router_decision = prepared.router_decision
            turn_followup_frames = self._turn_followup_task_frames(router_decision)
            step_result = prepared.step_result
            tool_result = prepared.tool_result
            memory_context = prepared.memory_context
            conversation_context = prepared.conversation_context
            if prepared.reply_override is not None:
                reply = prepared.reply_override
            else:
                if turn_followup_frames:
                    task_results = [
                        self._task_response_context(
                            chat_session,
                            prepared.active_skill,
                            router_decision,
                            step_result,
                            tool_result,
                        )
                    ]
                    primary_router_decision = router_decision
                    primary_step_result = step_result
                    primary_tool_result = tool_result
                    finalize_state = self._finalize_execution_after_reply(
                        request.tenant_id,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                    paused_primary = None
                    if finalize_state == "continued" and prepared.active_skill:
                        paused_primary = self.runtime.suspend_current_skill(chat_session)
                    continuation = self._try_continue_pending_after_completion(
                        request,
                        chat_session,
                        prepared.model_config,
                        self._list_published_skills(request.tenant_id, chat_session.agent_id),
                        self._tools_with_general_skills(
                            request.tenant_id,
                            self._list_enabled_tools(
                                request.tenant_id, chat_session.agent_id
                            ),
                            chat_session.agent_id,
                        ),
                        self._get_persona_prompt(
                            request.tenant_id, chat_session.agent_id
                        ),
                        memory_context,
                        conversation_context,
                        "",
                        turn_task_frames=turn_followup_frames,
                    )
                    if continuation:
                        task_results.extend(continuation.task_results)
                    if paused_primary:
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.runtime.restore_task_frame(chat_session, paused_primary)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        router_decision = primary_router_decision
                        step_result = primary_step_result
                        tool_result = primary_tool_result
                    elif continuation:
                        router_decision = continuation.router_decision
                        step_result = continuation.step_result
                        tool_result = continuation.tool_result
                    response_active_skill = (
                        prepared.active_skill
                        if paused_primary or not continuation
                        else continuation.active_skill
                    )
                    reply = self._generate_reply_segment(
                        request.message,
                        chat_session,
                        response_active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                        prepared.model_config,
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                        task_results,
                    )
                else:
                    reply = self._generate_reply_segment(
                        request.message,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                        prepared.model_config,
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                    )
                    finalize_state = self._finalize_execution_after_reply(
                        request.tenant_id,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                if (
                    not turn_followup_frames
                    and finalize_state == "completed"
                    and request.interaction_mode == "scheduled_task"
                ):
                    continuation = self._try_continue_pending_after_completion(
                        request,
                        chat_session,
                        prepared.model_config,
                        self._list_published_skills(request.tenant_id, chat_session.agent_id),
                        self._tools_with_general_skills(
                            request.tenant_id,
                            self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
                            chat_session.agent_id,
                        ),
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                        reply,
                    )
                    if continuation:
                        reply = "\n\n".join(
                            part
                            for part in (reply.strip(), continuation.reply.strip())
                            if part
                        )
                        router_decision = continuation.router_decision
                        step_result = continuation.step_result
                        tool_result = continuation.tool_result
                    elif chat_session.pending_tasks_json:
                        self.events.record(
                            request.tenant_id,
                            chat_session.id,
                            "pending_tasks_waiting",
                            {"pending_tasks": chat_session.pending_tasks_json or []},
                        )

        except AgentLoopPreconditionError as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            return self._finish_with_error(chat_session, exc.code, exc.message)
        except LLMError as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "LLM_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc)
            )
        except Exception as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "AGENT_LOOP_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )

        if not chat_session:
            chat_session = self._get_or_create_session(request)
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=prepared_user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        if memory_model_config:
            self._enqueue_memory_capture(
                request,
                chat_session,
                step_result,
                tool_result,
                memory_model_config,
            )
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )

    def _try_handle_general_skill_after_scene_router(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        user_message_id: str | None = None,
        capability: tuple[GeneralSkill | None, GeneralSkillSelection] | None = None,
    ) -> ChatTurnResponse | None:
        if not self._scene_router_deferred_to_general(router_decision):
            return None
        capability = capability or self._select_general_capability(
            request.message,
            model_config,
            chat_session.agent_id,
            conversation_context,
            memory_context,
        )
        skill, selection = capability
        if skill is None:
            return None
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json"),
                },
                user_message_id,
            ),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json"),
                },
                user_message_id,
            ),
        )
        run_response = self.general_skill_runner.run(
            skill,
            request.message,
            model_config,
            request.user_id,
            conversation_context=conversation_context,
            memory_context=memory_context,
        )
        self._record_general_skill_run_events(
            request.tenant_id, chat_session, run_response, user_message_id
        )
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        knowledge_step = self._auto_knowledge_step_result(
            request,
            chat_session,
            model_config,
            router_decision,
            selection,
        )
        self._merge_capability_knowledge(step_result, knowledge_step)
        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        reply = self._generate_reply_segment(
            request.message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
            memory_context or [],
            (
                conversation_context
                if conversation_context is not None
                else self._conversation_context(chat_session)
            ),
        )
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            step_result,
            tool_result,
            model_config,
        )
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )

    def _general_skill_agent_outputs(
        self, run_response: GeneralSkillRunResponse
    ) -> tuple[StepAgentResult, ToolResult]:
        success = (
            bool(run_response.structured_result.get("success", True))
            and not run_response.stderr.strip()
        )
        data = {
            "skill_slug": run_response.skill_slug,
            "reply": run_response.reply,
            "structured_result": run_response.structured_result,
            "stdout": run_response.stdout,
            "stderr": run_response.stderr,
        }
        tool_result = ToolResult(
            tool_name=f"{GENERAL_SKILL_TOOL_PREFIX}{run_response.skill_slug}",
            success=success,
            data=data if success else None,
            error=None
            if success
            else ToolError(
                code="GENERAL_SKILL_FAILED", message=run_response.stderr or run_response.reply
            ),
        )
        step_result = StepAgentResult(
            reply=run_response.reply,
            is_step_completed=success,
            tool_call=None,
        )
        return step_result, tool_result

    @staticmethod
    def _merge_capability_knowledge(
        step_result: StepAgentResult,
        knowledge_step: StepAgentResult,
    ) -> None:
        if knowledge_step.knowledge_query is not None:
            step_result.knowledge_query = knowledge_step.knowledge_query
        if knowledge_step.knowledge_results:
            step_result.knowledge_results = knowledge_step.knowledge_results

    def _scene_router_deferred_to_general(self, router_decision: RouterDecision) -> bool:
        if router_decision.selected_task_id:
            return False
        if (
            router_decision.task_frames
            or
            router_decision.pending_tasks
            or router_decision.created_tasks
            or router_decision.task_updates
        ):
            return False
        return router_decision.decision in {
            "answer_only",
            "clarify",
        }

    def _should_run_step_agent(
        self, router_decision: RouterDecision, active_skill: Skill | None
    ) -> bool:
        if active_skill is None:
            return False
        return router_decision.decision not in {
            "answer_only",
            "clarify",
            "create_pending",
            "update_pending",
            "complete_task",
            "handoff_human",
        }

    def _stream_general_skill_response(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        selected_general_skill: tuple[GeneralSkill, GeneralSkillSelection],
        router_decision: RouterDecision | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        persona_prompt: str | None = None,
        user_message_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, object]]:
        skill, selection = selected_general_skill
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json")
                    if router_decision
                    else None,
                },
                user_message_id,
            ),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json")
                    if router_decision
                    else None,
                },
                user_message_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "general_skill_intent",
            "正在判断意图",
            {"skill_slug": skill.slug, "skill_name": skill.name, "reason": selection.reason},
            user_message_id=user_message_id,
        )
        yield self._stream_event(
            "general_skill_trace",
            chat_session,
            self._turn_payload(
                {
                    "phase": "intent_checked",
                    "message": "判断意图",
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "reason": selection.reason,
                    "confidence": selection.confidence,
                },
                user_message_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "general_skill_routing",
            "正在选择通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
            user_message_id=user_message_id,
        )
        yield self._stream_event(
            "general_skill_state",
            chat_session,
            self._turn_payload(
                {"skillSlug": skill.slug, "skillName": skill.name, "state": "selected"},
                user_message_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "general_skill_running",
            "正在运行通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
            user_message_id=user_message_id,
        )
        general_skill_events: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        skill_snapshot = SimpleNamespace(
            slug=skill.slug,
            name=skill.name,
            description=skill.description,
            homepage=skill.homepage,
            skill_markdown=skill.skill_markdown,
            skill_files_json=skill.skill_files_json or [],
            metadata_json=skill.metadata_json or {},
            permissions_json=skill.permissions_json or {},
            runtime_config_json=skill.runtime_config_json or {},
            status=skill.status,
        )
        model_snapshot = model_config

        def general_skill_sink(trace_item: dict[str, Any]) -> None:
            general_skill_events.put(("trace", trace_item))

        def general_skill_worker() -> None:
            try:
                response = GeneralSkillRunner().run(
                    skill_snapshot,
                    request.message,
                    model_snapshot,
                    request.user_id,
                    event_sink=general_skill_sink,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                general_skill_events.put(("complete", response))
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                general_skill_events.put(("error", exc))
            finally:
                general_skill_events.put(None)

        threading.Thread(target=general_skill_worker, daemon=True).start()
        run_response: GeneralSkillRunResponse | None = None
        streamed_trace_count = 0
        while True:
            if is_cancelled and is_cancelled():
                return
            try:
                queued = general_skill_events.get(timeout=0.5)
            except queue.Empty:
                continue
            if queued is None:
                break
            event_name, payload = queued
            if event_name == "trace":
                if is_cancelled and is_cancelled():
                    return
                streamed_trace_count += 1
                yield self._stream_event(
                    "general_skill_trace",
                    chat_session,
                    self._turn_payload(payload, user_message_id),
                )
            elif event_name == "complete":
                run_response = payload
            elif event_name == "error":
                raise payload

        if run_response is None:
            raise LLMError("General skill stream ended without a result")
        if is_cancelled and is_cancelled():
            return
        self._record_general_skill_run_events(
            request.tenant_id,
            chat_session,
            run_response,
            user_message_id,
            include_trace=streamed_trace_count == 0,
        )
        if is_cancelled and is_cancelled():
            return
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        resolved_router_decision = router_decision or RouterDecision(
            decision="answer_only", user_intent="通用技能执行结果回复"
        )
        knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
        knowledge_step = self._auto_knowledge_step_result(
            request,
            chat_session,
            model_config,
            resolved_router_decision,
            selection,
            stream_events=knowledge_stream_events,
        )
        self._merge_capability_knowledge(step_result, knowledge_step)
        for event_name, payload in knowledge_stream_events:
            yield self._stream_event(
                event_name,
                chat_session,
                self._turn_payload(payload, user_message_id),
            )
        yield self._stream_status(
            chat_session, "responding", "正在生成回复", user_message_id=user_message_id
        )
        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        reply = ""
        for chunk in self._generate_reply_stream_segment(
            request.message,
            chat_session,
            active_skill,
            resolved_router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt
            if persona_prompt is not None
            else self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
            memory_context or [],
            (
                conversation_context
                if conversation_context is not None
                else self._conversation_context(chat_session)
            ),
        ):
            reply += chunk
            yield self._stream_event(
                "stream_delta",
                chat_session,
                self._turn_payload({"content": chunk}, user_message_id),
            )
            self._pace_stream()
        if not reply.strip():
            reply = run_response.reply
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event(
                    "stream_delta",
                    chat_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
        if is_cancelled and is_cancelled():
            return
        yield self._stream_event(
            "stream_end", chat_session, self._turn_payload({}, user_message_id)
        )
        if is_cancelled and is_cancelled():
            return
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            step_result,
            tool_result,
            model_config,
        )
        result = ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )
        yield self._stream_event(
            "complete",
            chat_session,
            self._turn_payload(result.model_dump(mode="json"), user_message_id),
        )

    def _stream_continue_pending_after_completion(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        skills: list[Skill],
        tools: list[Any],
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        completed_reply: str,
        completed_skill_ids_this_turn: set[str] | None = None,
        user_message_id: str | None = None,
        turn_task_frames: list[PendingTask] | None = None,
    ) -> Iterator[dict[str, object]]:
        remaining_turn_frames = list(turn_task_frames or [])
        uses_turn_frames = turn_task_frames is not None
        if uses_turn_frames and not remaining_turn_frames:
            return None
        if not uses_turn_frames and not chat_session.pending_tasks_json:
            return None
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        task_results: list[dict[str, object]] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        for queue_round in range(max_actions):
            if uses_turn_frames:
                if not remaining_turn_frames:
                    break
                turn_frame = remaining_turn_frames.pop(0)
                task_id = turn_frame.task_id or f"turn_task_{queue_round + 1}"
            else:
                if not chat_session.pending_tasks_json:
                    break
                task_id = self._next_pending_task_id(chat_session)
                if not task_id:
                    break
            yield self._stream_status(
                chat_session,
                "routing",
                "正在继续后续任务",
                {"queue_round": queue_round + 1},
                user_message_id=user_message_id,
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_execution_order_advanced",
                {"task_id": task_id, "queue_round": queue_round + 1},
            )

            for task_id in [task_id]:
                if executed_actions >= max_actions:
                    break
                router_decision = (
                    self._router_decision_from_turn_task_frame(turn_frame)
                    if uses_turn_frames
                    else self._router_decision_from_task_frame(
                        chat_session,
                        task_id,
                        "按 Router 已确定的任务顺序继续执行。",
                    )
                )
                if not router_decision:
                    continue
                yield self._stream_event(
                    "router_decision",
                    chat_session,
                    self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
                )

                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                self.runtime.apply_decision(chat_session, router_decision)
                state_pruned = self._drop_unavailable_skill_state(
                    request.tenant_id, chat_session, skills
                )
                if self._should_record_runtime_event_after_prune(
                    router_decision, chat_session, skills, state_pruned
                ):
                    self._record_runtime_event(
                        request.tenant_id, chat_session, before_skill, before_step, router_decision
                    )
                self.db.commit()
                self.db.refresh(chat_session)

                active_skill = self._get_active_skill(
                    request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
                )
                if not self._should_run_step_agent(router_decision, active_skill):
                    continue
                yield self._stream_event(
                    "skill_state",
                    chat_session,
                    self._skill_state_payload(
                        chat_session,
                        skills,
                        self._runtime_stream_context(
                            router_decision, before_skill, before_step, chat_session
                        ),
                        user_message_id=user_message_id,
                    ),
                )
                yield self._stream_status(
                    chat_session,
                    "stepping",
                    "正在思考",
                    {
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                    },
                    user_message_id=user_message_id,
                )
                repair_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._run_step_agent_with_context_repair(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    router_decision,
                    memory_context,
                    conversation_context,
                    repair_stream_events,
                )
                yield self._stream_event(
                    "step_result",
                    chat_session,
                    self._turn_payload(step_result.model_dump(mode="json"), user_message_id),
                )
                self.db.commit()
                self.db.refresh(chat_session)
                for event_name, payload in repair_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

                tool_result = None
                if step_result.tool_call:
                    tool_stream_events: list[tuple[str, dict[str, object]]] = []
                    step_result, tool_result = self._execute_tool_action_cycle(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        step_result,
                        tool_stream_events,
                        conversation_context=conversation_context,
                        memory_context=memory_context,
                    )
                    for event_name, payload in tool_stream_events:
                        yield self._stream_event(
                            event_name, chat_session, self._turn_payload(payload, user_message_id)
                        )

                reflection_stream_events: list[tuple[str, dict[str, object]]] = []
                reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
                if reflection_max_rounds > 0 and self._should_try_reflection(
                    router_decision, step_result, tool_result
                ):
                    yield self._stream_status(
                        chat_session,
                        "reflecting",
                        "正在反思",
                        {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
                        user_message_id=user_message_id,
                    )
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._run_reflection_rounds(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    reflection_max_rounds,
                    conversation_context,
                    reflection_stream_events,
                    completed_skill_ids_this_turn,
                    memory_context=memory_context,
                )
                for event_name, payload in reflection_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

                graph_stream_events: list[tuple[str, dict[str, object]]] = []
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._auto_progress_skill_graph(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    memory_context,
                    conversation_context,
                    graph_stream_events,
                    completed_skill_ids_this_turn,
                )
                for event_name, payload in graph_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

                task_results.append(
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                )
                draft = self._task_response_draft(step_result)
                if draft:
                    replies, _ = self._merge_queued_reply_segment(replies, draft)
                executed_actions += 1
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if finalize_state == "completed" and active_skill:
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                if finalize_state == "handoff":
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                if finalize_state == "continued":
                    if uses_turn_frames and remaining_turn_frames:
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        continue
                    if self._should_attempt_queued_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        queue_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {
                        "pending_tasks": chat_session.pending_tasks_json or [],
                        "round": queue_round + 1,
                    },
                )
            if executed_actions >= max_actions:
                break

        return self._queued_continuation(
            replies,
            task_results,
            active_skill,
            router_decision,
            step_result,
            tool_result,
        )

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        reply = ""
        memory_model_config: ModelConfig | None = None
        turn_finalized = False
        user_message_id: str | None = None

        def record_current_turn_cancelled(client_turn_id: str | None = None) -> bool:
            nonlocal turn_finalized
            if not chat_session or not user_message_id:
                return False
            if turn_finalized:
                return False
            normalized_client_turn_id = (client_turn_id or request.client_turn_id or "").strip()
            self.db.rollback()
            existing_cancel = self.db.exec(
                select(AgentEvent)
                .where(
                    AgentEvent.tenant_id == request.tenant_id,
                    AgentEvent.session_id == chat_session.id,
                    AgentEvent.event_type == "stream_cancelled",
                )
                .order_by(AgentEvent.created_at.desc())
            ).all()
            for event in existing_cancel:
                payload = event.payload_json or {}
                event_turn_ids = {
                    str(payload.get("turn_id") or "").strip(),
                    str(payload.get("user_message_id") or "").strip(),
                    str(payload.get("message_id") or "").strip(),
                    str(payload.get("client_turn_id") or "").strip(),
                }
                matches_server_turn = user_message_id in event_turn_ids
                matches_client_turn = bool(
                    normalized_client_turn_id and normalized_client_turn_id in event_turn_ids
                )
                if matches_server_turn or matches_client_turn:
                    self._persist_cancelled_assistant_message(
                        request.tenant_id,
                        chat_session,
                        user_message_id,
                        normalized_client_turn_id,
                    )
                    clear_chat_turn_cancelled(chat_session.id, user_message_id)
                    if normalized_client_turn_id:
                        clear_chat_turn_cancelled(chat_session.id, normalized_client_turn_id)
                    turn_finalized = True
                    return True
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "stream_cancelled",
                self._turn_payload(
                    {
                        "phase": "cancelled",
                        "text": "已停止生成",
                        "client_turn_id": normalized_client_turn_id or None,
                    },
                    user_message_id,
                ),
            )
            self._persist_cancelled_assistant_message(
                request.tenant_id,
                chat_session,
                user_message_id,
                normalized_client_turn_id,
            )
            self.db.commit()
            clear_chat_turn_cancelled(chat_session.id, user_message_id)
            if normalized_client_turn_id:
                clear_chat_turn_cancelled(chat_session.id, normalized_client_turn_id)
            turn_finalized = True
            return True

        def mark_current_turn_cancelled() -> bool:
            if not chat_session or not user_message_id:
                return False
            client_turn_id = (request.client_turn_id or "").strip()
            server_cancelled = is_chat_turn_cancelled(chat_session.id, user_message_id)
            client_cancelled = bool(
                client_turn_id and is_chat_turn_cancelled(chat_session.id, client_turn_id)
            )
            if not server_cancelled and not client_cancelled:
                return False
            return record_current_turn_cancelled(client_turn_id)

        def finalize_turn_once(
            target_session: ChatSession,
            final_reply: str,
            final_step_result: StepAgentResult | None = None,
            final_source_message: str | None = None,
        ) -> None:
            nonlocal turn_finalized
            if turn_finalized:
                return
            self._finalize_turn(
                target_session,
                request.tenant_id,
                final_reply,
                final_step_result,
                final_source_message,
                user_message_id=user_message_id,
            )
            turn_finalized = True

        def recover_chat_session_after_exception() -> ChatSession:
            nonlocal chat_session
            session_id = str(
                (chat_session.id if chat_session else request.session_id) or ""
            ).strip()
            self.db.rollback()
            recovered = self.db.get(ChatSession, session_id) if session_id else None
            if recovered is None:
                recovered = self._get_or_create_session(request)
            chat_session = recovered
            return recovered

        def exception_payload(code: str, message: str) -> dict[str, object]:
            payload: dict[str, object] = {
                "code": code,
                "message": message,
                "client_turn_id": request.client_turn_id or None,
                "error_traceback": traceback.format_exc()[-ERROR_TRACEBACK_CHAR_LIMIT:],
            }
            return self._turn_payload(payload, user_message_id)

        def stream_failure_response(
            title: str,
            error: object,
            code: str,
            suggestion: str,
            message: str | None = None,
        ) -> Iterator[dict[str, object]]:
            nonlocal reply
            target_session = recover_chat_session_after_exception()
            if mark_current_turn_cancelled():
                return
            error_message = message if message is not None else str(error)
            reply = format_runtime_failure_reply(title, error_message, code, suggestion)
            payload = exception_payload(code, error_message)
            self.events.record(request.tenant_id, target_session.id, "error_occurred", payload)
            self.db.commit()
            yield self._stream_status(
                target_session,
                "error",
                reply,
                {"code": code, "message": error_message},
                user_message_id=user_message_id,
            )
            yield self._stream_event("error_occurred", target_session, payload)
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event(
                    "stream_delta",
                    target_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
            yield self._stream_event(
                "stream_end", target_session, self._turn_payload({}, user_message_id)
            )
            finalize_turn_once(target_session, reply, step_result, request.message)
            self.db.commit()
            self.db.refresh(target_session)

        try:
            chat_session = self._get_or_create_session(request)
            self._mark_session_running(chat_session)
            yield self._stream_event(
                "session_created",
                chat_session,
                {"newSessionId": chat_session.id, "sessionId": chat_session.id},
            )
            user_message = self._append_message(
                request.tenant_id,
                chat_session.id,
                "user",
                request.message,
                metadata=self._user_message_metadata(request),
            )
            user_message_id = user_message.id
            bind_event_turn = getattr(self.events, "bind_turn", None)
            if callable(bind_event_turn):
                bind_event_turn(user_message.id, request.client_turn_id)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "user_message_received",
                {
                    "message_id": user_message.id,
                    "client_turn_id": request.client_turn_id,
                    "message": request.message,
                    "channel": request.channel,
                    "user_id": request.user_id,
                },
            )
            yield self._stream_event(
                "user_message_received",
                chat_session,
                self._turn_payload(
                    {
                        "message_id": user_message.id,
                        "client_turn_id": request.client_turn_id,
                        "message": request.message,
                        "channel": request.channel,
                        "user_id": request.user_id,
                    },
                    user_message.id,
                ),
            )
            self.db.commit()
            self.db.refresh(chat_session)
            self.db.refresh(user_message)
            model_config = self._get_request_model(request, chat_session.agent_id)
            if not model_config:
                raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
            memory_model_config = model_config
            skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
            tools = self._tools_with_general_skills(
                request.tenant_id,
                self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
                chat_session.agent_id,
            )
            persona_prompt = self._get_persona_prompt(request.tenant_id, chat_session.agent_id)
            self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
            if not skills:
                no_skill_context = self._conversation_context(
                    chat_session, model_config=model_config
                )
                if self._context_compacted_now(no_skill_context):
                    yield self._stream_status(
                        chat_session,
                        "preparing",
                        "正在整理上下文",
                        user_message_id=user_message_id,
                    )
                yield self._stream_status(
                    chat_session, "routing", "正在判断用户意图", user_message_id=user_message_id
                )
                capability = self._select_general_capability(
                    request.message,
                    model_config,
                    chat_session.agent_id,
                    no_skill_context,
                    [],
                )
                if capability[0] is not None:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        (capability[0], capability[1]),
                        None,
                        [],
                        no_skill_context,
                        persona_prompt,
                        user_message.id,
                        mark_current_turn_cancelled,
                    )
                    return
                router_decision = RouterDecision(
                    decision="answer_only",
                    reason="No published scene skills are available; answer as chat.",
                )
                yield self._stream_event(
                    "router_decision",
                    chat_session,
                    self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
                )
                knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    capability[1],
                    stream_events=knowledge_stream_events,
                )
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
                yield self._stream_status(
                    chat_session, "responding", "正在生成回复", user_message_id=user_message_id
                )
                reply = ""
                for chunk in self._generate_reply_stream_segment(
                    request.message,
                    chat_session,
                    None,
                    router_decision,
                    step_result,
                    None,
                    model_config,
                    persona_prompt,
                    [],
                    no_skill_context,
                ):
                    reply += chunk
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
                if mark_current_turn_cancelled():
                    return
                yield self._stream_event(
                    "stream_end", chat_session, self._turn_payload({}, user_message_id)
                )
                if mark_current_turn_cancelled():
                    return
                finalize_turn_once(chat_session, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event(
                    "complete",
                    chat_session,
                    self._turn_payload(result.model_dump(mode="json"), user_message_id),
                )
                return
            self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
            memory_context = [
                memory_read(row)
                for row in self.memory.context_memories(
                    request.tenant_id,
                    request.user_id,
                    agent_id=chat_session.agent_id,
                )
            ]
            if memory_context:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "memory_recalled",
                    {"memories": memory_context},
                )
            self.db.commit()
            self.db.refresh(chat_session)
            conversation_context = self._conversation_context(chat_session, model_config=model_config)
            if self._context_compacted_now(conversation_context):
                yield self._stream_status(
                    chat_session,
                    "preparing",
                    "正在整理上下文",
                    user_message_id=user_message_id,
                )

            yield self._stream_status(
                chat_session, "routing", "正在判断用户意图", user_message_id=user_message_id
            )
            router_decision = self.router.decide(
                request.message,
                chat_session,
                skills,
                model_config,
                conversation_context,
                memory_context,
            )
            turn_followup_frames = self._turn_followup_task_frames(router_decision)
            hydrated_slots = self._hydrate_router_decision_from_context(
                chat_session, router_decision, skills, memory_context
            )
            if hydrated_slots:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "router_slots_hydrated",
                    hydrated_slots,
                )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_decision_created",
                self._turn_payload(router_decision.model_dump(), user_message_id),
            )
            yield self._stream_event(
                "router_decision",
                chat_session,
                self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
            )
            capability_selection: GeneralSkillSelection | None = None
            if self._scene_router_deferred_to_general(router_decision):
                capability = self._select_general_capability(
                    request.message,
                    model_config,
                    chat_session.agent_id,
                    conversation_context,
                    memory_context,
                )
                capability_selection = capability[1]
                if capability[0] is not None:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        (capability[0], capability[1]),
                        router_decision,
                        memory_context,
                        conversation_context,
                        persona_prompt,
                        user_message.id,
                        mark_current_turn_cancelled,
                    )
                    return

            before_skill = chat_session.active_skill_id
            before_step = chat_session.active_step_id
            self.runtime.apply_decision(chat_session, router_decision)
            state_pruned = self._drop_unavailable_skill_state(
                request.tenant_id, chat_session, skills
            )
            if self._should_record_runtime_event_after_prune(
                router_decision, chat_session, skills, state_pruned
            ):
                self._record_runtime_event(
                    request.tenant_id, chat_session, before_skill, before_step, router_decision
                )
            self.db.commit()
            self.db.refresh(chat_session)

            active_skill = self._get_active_skill(
                request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
            )
            if not self._should_run_step_agent(router_decision, active_skill):
                knowledge_stream_events = []
                auto_step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    capability_selection,
                    stream_events=knowledge_stream_events,
                )
                if auto_step_result.knowledge_query:
                    step_result = auto_step_result
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
                yield self._stream_status(
                    chat_session, "responding", "正在生成回复", user_message_id=user_message_id
                )
                for chunk in self._generate_reply_stream_segment(
                    request.message,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    model_config,
                    persona_prompt,
                    memory_context,
                    conversation_context,
                ):
                    reply += chunk
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
                yield self._stream_event(
                    "stream_end", chat_session, self._turn_payload({}, user_message_id)
                )
                finalize_turn_once(chat_session, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    tool_result=tool_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event(
                    "complete",
                    chat_session,
                    self._turn_payload(result.model_dump(mode="json"), user_message_id),
                )
                return
            yield self._stream_event(
                "skill_state",
                chat_session,
                self._skill_state_payload(
                    chat_session,
                    skills,
                    self._runtime_stream_context(
                        router_decision, before_skill, before_step, chat_session
                    ),
                    user_message_id=user_message_id,
                ),
            )
            yield self._stream_status(
                chat_session,
                "stepping",
                "正在思考",
                {
                    "active_skill_id": chat_session.active_skill_id,
                    "active_step_id": chat_session.active_step_id,
                },
                user_message_id=user_message_id,
            )
            repair_stream_events: list[tuple[str, dict[str, object]]] = []
            step_result = self._run_step_agent_with_context_repair(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                router_decision,
                memory_context,
                conversation_context,
                repair_stream_events,
            )
            yield self._stream_event(
                "step_result",
                chat_session,
                self._turn_payload(step_result.model_dump(mode="json"), user_message_id),
            )
            self.db.commit()
            self.db.refresh(chat_session)
            for event_name, payload in repair_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            if step_result.knowledge_query:
                knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._execute_knowledge_query_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    memory_context,
                    conversation_context,
                    knowledge_stream_events,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
            if step_result.tool_call:
                tool_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result, tool_result = self._execute_tool_action_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    tool_stream_events,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                for event_name, payload in tool_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

            reflection_stream_events: list[tuple[str, dict[str, object]]] = []
            reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
            if reflection_max_rounds > 0 and self._should_try_reflection(
                router_decision, step_result, tool_result
            ):
                yield self._stream_status(
                    chat_session,
                    "reflecting",
                    "正在反思",
                    {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
                    user_message_id=user_message_id,
                )
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
            ) = self._run_reflection_rounds(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                reflection_max_rounds,
                conversation_context,
                reflection_stream_events,
                memory_context=memory_context,
            )
            for event_name, payload in reflection_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            graph_stream_events: list[tuple[str, dict[str, object]]] = []
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
            ) = self._auto_progress_skill_graph(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                memory_context,
                conversation_context,
                graph_stream_events,
            )
            for event_name, payload in graph_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            response_task_results: list[dict[str, object]] | None = None
            finalize_before_reply = bool(turn_followup_frames) or (
                request.interaction_mode == "scheduled_task"
            )
            if finalize_before_reply:
                response_task_results = [
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                ]
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
            else:
                finalize_state = "continued"

            if turn_followup_frames and finalize_state != "handoff":
                primary_active_skill = active_skill
                primary_router_decision = router_decision
                primary_step_result = step_result
                primary_tool_result = tool_result
                paused_primary = None
                if finalize_state == "continued" and active_skill:
                    paused_primary = self.runtime.suspend_current_skill(chat_session)
                continuation = None
                if turn_followup_frames:
                    continuation = yield from self._stream_continue_pending_after_completion(
                        request,
                        chat_session,
                        model_config,
                        skills,
                        tools,
                        persona_prompt,
                        memory_context,
                        conversation_context,
                        "",
                        user_message_id=user_message_id,
                        turn_task_frames=turn_followup_frames,
                    )
                if continuation and response_task_results is not None:
                    response_task_results.extend(continuation.task_results)
                if paused_primary:
                    if chat_session.active_skill_id:
                        self.runtime.suspend_current_skill(chat_session, enqueue=True)
                    self.runtime.restore_task_frame(chat_session, paused_primary)
                    self.db.commit()
                    self.db.refresh(chat_session)
                    active_skill = primary_active_skill
                    router_decision = primary_router_decision
                    step_result = primary_step_result
                    tool_result = primary_tool_result
                elif continuation:
                    active_skill = continuation.active_skill
                    router_decision = continuation.router_decision
                    step_result = continuation.step_result
                    tool_result = continuation.tool_result
            elif finalize_state == "completed" and request.interaction_mode == "scheduled_task":
                continuation = None
                if self._next_pending_task_id(chat_session):
                    continuation = yield from self._stream_continue_pending_after_completion(
                        request,
                        chat_session,
                        model_config,
                        skills,
                        tools,
                        persona_prompt,
                        memory_context,
                        conversation_context,
                        "",
                        user_message_id=user_message_id,
                    )
                if continuation:
                    if response_task_results is not None:
                        response_task_results.extend(continuation.task_results)
                    active_skill = continuation.active_skill
                    router_decision = continuation.router_decision
                    step_result = continuation.step_result
                    tool_result = continuation.tool_result
                elif chat_session.pending_tasks_json:
                    self.events.record(
                        request.tenant_id,
                        chat_session.id,
                        "pending_tasks_waiting",
                        {"pending_tasks": chat_session.pending_tasks_json or []},
                    )

            yield self._stream_status(
                chat_session, "responding", "正在生成回复", user_message_id=user_message_id
            )
            chunks: list[str] = []
            for chunk in self._generate_reply_stream_segment(
                request.message,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                model_config,
                persona_prompt,
                memory_context,
                conversation_context,
                response_task_results,
            ):
                chunks.append(chunk)
                yield self._stream_event(
                    "stream_delta",
                    chat_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
            reply = "".join(chunks).strip() or FALLBACK_REPLY
            if not chunks:
                for chunk in self.response_generator.chunk_text(reply):
                    chunks.append(chunk)
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
            if not finalize_before_reply:
                self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
            if mark_current_turn_cancelled():
                return
            yield self._stream_event(
                "stream_end", chat_session, self._turn_payload({}, user_message_id)
            )
            if mark_current_turn_cancelled():
                return

        except GeneratorExit:
            if chat_session and user_message_id:
                try:
                    if not mark_current_turn_cancelled():
                        self.db.rollback()
                except Exception:
                    self.db.rollback()
            raise
        except AgentLoopPreconditionError as exc:
            yield from stream_failure_response(
                "系统配置错误",
                exc.message,
                exc.code,
                "请在管理端补齐配置后重试。",
                message=exc.message,
            )
            return
        except LLMError as exc:
            yield from stream_failure_response(
                "模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc)
            )
            return
        except Exception as exc:
            turn_finalized = False
            yield from stream_failure_response(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )
            return

        turn_commit_completed = False
        try:
            if not chat_session:
                chat_session = self._get_or_create_session(request)
            if mark_current_turn_cancelled():
                return
            finalize_turn_once(chat_session, reply, step_result, request.message)
            self.db.commit()
            turn_commit_completed = True
            self.db.refresh(chat_session)
            if memory_model_config:
                self._enqueue_memory_capture(
                    request,
                    chat_session,
                    step_result,
                    tool_result,
                    memory_model_config,
                )
            result = ChatTurnResponse(
                reply=reply,
                session_id=chat_session.id,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=tool_result,
                session_state=public_session(chat_session),
            )
            yield self._stream_event(
                "complete",
                chat_session,
                self._turn_payload(result.model_dump(mode="json"), user_message_id),
            )
        except Exception as exc:
            if not turn_commit_completed:
                turn_finalized = False
            yield from stream_failure_response(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )

    def _stream_status(
        self,
        chat_session: ChatSession,
        phase: str,
        text: str,
        extra: dict[str, object] | None = None,
        user_message_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"phase": phase, "text": text, **(extra or {})}
        if user_message_id:
            payload = self._turn_payload(payload, user_message_id)
            if phase != "received":
                self.events.record(
                    chat_session.tenant_id, chat_session.id, "stream_status", payload
                )
                self.db.commit()
        return self._stream_event(
            "status",
            chat_session,
            payload,
        )

    def _stream_event(
        self,
        kind: str,
        chat_session: ChatSession,
        payload: dict[str, object],
    ) -> dict[str, object]:
        persisted_stream_events = {
            "agent_loop_completed",
            "agent_loop_continued",
            "general_skill_run_finished",
            "general_skill_trace",
            "knowledge_result",
            "reflection_decision",
            "skill_state",
            "step_result",
            "stream_delta",
            "stream_replace",
            "stream_end",
            "tool_result",
        }
        if kind in persisted_stream_events and (
            payload.get("turn_id") or payload.get("user_message_id")
        ):
            self.events.record(chat_session.tenant_id, chat_session.id, kind, payload)
            self.db.commit()
        data = {
            "kind": kind,
            "sessionId": chat_session.id,
            "timestamp": utc_now().isoformat(),
            "provider": "skill",
            **payload,
        }
        return {"event": kind, "data": data}

    def _pace_stream(self) -> None:
        sleep(STREAM_CHUNK_INTERVAL_SECONDS)

    def _prepare_turn(
        self, request: ChatTurnRequest, status_callback: StatusCallback | None = None
    ) -> PreparedTurn:
        def status(phase: str, payload: dict[str, object] | None = None) -> None:
            if status_callback:
                status_callback(phase, payload or {})

        chat_session = self._get_or_create_session(request)
        self._mark_session_running(chat_session)
        status("received", {"session_id": chat_session.id})
        user_message = self._append_message(
            request.tenant_id,
            chat_session.id,
            "user",
            request.message,
            metadata=self._user_message_metadata(request),
        )
        bind_event_turn = getattr(self.events, "bind_turn", None)
        if callable(bind_event_turn):
            bind_event_turn(user_message.id, request.client_turn_id)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {
                "message_id": user_message.id,
                "client_turn_id": request.client_turn_id,
                "message": request.message,
                "channel": request.channel,
                "user_id": request.user_id,
            },
        )

        model_config = self._get_request_model(request, chat_session.agent_id)
        skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
        tools = self._tools_with_general_skills(
            request.tenant_id,
            self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
            chat_session.agent_id,
        )
        if not model_config:
            raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
        self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if not skills:
            no_skill_context = self._conversation_context(
                chat_session, model_config=model_config
            )
            if self._context_compacted_now(no_skill_context):
                status("preparing", {"compacted_now": True})
            capability = self._select_general_capability(
                request.message,
                model_config,
                chat_session.agent_id,
                no_skill_context,
                [],
            )
            router_decision = RouterDecision(
                decision="answer_only",
                reason="No published scene skills are available; try general skills, then answer as chat.",
            )
            general_response = self._try_handle_general_skill_after_scene_router(
                request,
                chat_session,
                model_config,
                router_decision,
                [],
                no_skill_context,
                user_message.id,
                capability,
            )
            if general_response:
                return PreparedTurn(
                    chat_session=chat_session,
                    model_config=model_config,
                    active_skill=None,
                    router_decision=router_decision,
                    step_result=StepAgentResult(),
                    tool_result=None,
                    memory_context=[],
                    conversation_context=no_skill_context,
                    general_response=general_response,
                    user_message_id=user_message.id,
                )
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
                capability[1],
                status_callback=status,
            )
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=None,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=None,
                memory_context=[],
                conversation_context=no_skill_context,
                user_message_id=user_message.id,
            )
        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        memory_context = [
            memory_read(row)
            for row in self.memory.context_memories(
                request.tenant_id,
                request.user_id,
                agent_id=chat_session.agent_id,
            )
        ]
        if memory_context:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_recalled",
                {"memories": memory_context},
            )
        self.db.commit()
        self.db.refresh(chat_session)
        conversation_context = self._conversation_context(chat_session, model_config=model_config)
        if self._context_compacted_now(conversation_context):
            status("preparing", {"compacted_now": True})

        status("routing")
        router_decision = self.router.decide(
            request.message,
            chat_session,
            skills,
            model_config,
            conversation_context,
            memory_context,
        )
        hydrated_slots = self._hydrate_router_decision_from_context(
            chat_session, router_decision, skills, memory_context
        )
        if hydrated_slots:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_slots_hydrated",
                hydrated_slots,
            )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "router_decision_created",
            self._turn_payload(router_decision.model_dump(), user_message.id),
        )
        capability: tuple[GeneralSkill | None, GeneralSkillSelection] | None = None
        if self._scene_router_deferred_to_general(router_decision):
            capability = self._select_general_capability(
                request.message,
                model_config,
                chat_session.agent_id,
                conversation_context,
                memory_context,
            )
        general_response = self._try_handle_general_skill_after_scene_router(
            request,
            chat_session,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
            user_message.id,
            capability,
        )
        if general_response:
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=None,
                router_decision=router_decision,
                step_result=StepAgentResult(),
                tool_result=None,
                memory_context=memory_context,
                conversation_context=conversation_context,
                general_response=general_response,
                user_message_id=user_message.id,
            )

        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.apply_decision(chat_session, router_decision)
        state_pruned = self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if self._should_record_runtime_event_after_prune(
            router_decision, chat_session, skills, state_pruned
        ):
            self._record_runtime_event(
                request.tenant_id, chat_session, before_skill, before_step, router_decision
            )
        self.db.commit()
        self.db.refresh(chat_session)

        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        if not self._should_run_step_agent(router_decision, active_skill):
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
                capability[1] if capability else None,
                status_callback=status,
            )
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=active_skill,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=None,
                memory_context=memory_context,
                conversation_context=conversation_context,
                user_message_id=user_message.id,
            )
        status(
            "stepping",
            {
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
            },
        )
        step_result = self._run_step_agent_with_context_repair(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
        )

        tool_result: ToolResult | None = None
        self.db.commit()
        self.db.refresh(chat_session)
        if step_result.knowledge_query:
            step_result = self._execute_knowledge_query_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                memory_context,
                conversation_context,
                status_callback=status,
            )
            self.db.commit()
            self.db.refresh(chat_session)
        if step_result.tool_call:
            step_result, tool_result = self._execute_tool_action_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                status_callback=status,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )

        (
            active_skill,
            router_decision,
            step_result,
            tool_result,
        ) = self._run_reflection_rounds(
            request,
            chat_session,
            skills,
            tools,
            model_config,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            self._get_reflection_max_rounds(request.tenant_id),
            conversation_context,
            memory_context=memory_context,
        )
        (
            active_skill,
            router_decision,
            step_result,
            tool_result,
        ) = self._auto_progress_skill_graph(
            request,
            chat_session,
            skills,
            tools,
            model_config,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
        )

        return PreparedTurn(
            chat_session=chat_session,
            model_config=model_config,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            memory_context=memory_context,
            conversation_context=conversation_context,
            user_message_id=user_message.id,
        )

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        requested_handoff = router_decision.decision == "handoff_human" or step_result.handoff
        if requested_handoff:
            if self._current_step_allows_human_handoff(active_skill, chat_session.active_step_id):
                self._create_human_handoff_request(
                    tenant_id, chat_session, active_skill, step_result
                )
                return "handoff"
            else:
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "human_handoff_ignored",
                    {
                        "reason": "current_step_does_not_declare_handoff",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "router_decision": router_decision.decision,
                        "step_handoff": step_result.handoff,
                    },
                )
        if self._should_complete_skill(active_skill, chat_session, step_result, tool_result):
            self._complete_active_skill(tenant_id, chat_session, active_skill, "step_completed")
            return "completed"
        return "continued"

    def _current_step_allows_human_handoff(
        self, skill: Skill | None, active_step_id: str | None
    ) -> bool:
        if not skill:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        if not current_step:
            return False
        return self._step_declares_human_handoff(current_step)

    def _step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        node_type = str(step.get("type") or "").strip()
        return node_type == "handoff" or "handoff_human" in self._step_actions(step)

    def _create_human_handoff_request(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
    ) -> HumanHandoffRequest:
        existing = self.db.exec(
            select(HumanHandoffRequest)
            .where(HumanHandoffRequest.tenant_id == tenant_id)
            .where(HumanHandoffRequest.session_id == chat_session.id)
            .where(HumanHandoffRequest.status == "pending")
        ).first()
        if existing:
            chat_session.status = "handoff"
            chat_session.awaiting_input_json = {
                "type": "human_handoff",
                "handoff_id": existing.id,
                "pending_question": existing.pending_question,
            }
            chat_session.updated_at = utc_now()
            return existing

        current_step = (
            self._current_skill_step(active_skill, chat_session.active_step_id)
            if active_skill
            else None
        )
        handoff = HumanHandoffRequest(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            agent_id=chat_session.agent_id,
            requester_user_id=chat_session.user_id,
            assignee_user_id=self._human_handoff_assignee_user_id(
                tenant_id, chat_session.agent_id, chat_session.user_id
            ),
            trigger_skill_id=chat_session.active_skill_id,
            trigger_step_id=chat_session.active_step_id,
            context_summary=self._human_handoff_context_summary(chat_session),
            pending_question=self._human_handoff_pending_question(current_step, step_result),
            resume_payload_json={
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "slots": chat_session.slots_json or {},
                "pending_tasks": chat_session.pending_tasks_json or [],
            },
            metadata_json={
                "step": current_step or {},
                "step_reply": step_result.reply,
                "step_handoff": step_result.handoff,
            },
        )
        self.db.add(handoff)
        chat_session.status = "handoff"
        chat_session.awaiting_input_json = {
            "type": "human_handoff",
            "handoff_id": handoff.id,
            "pending_question": handoff.pending_question,
        }
        chat_session.updated_at = utc_now()
        self.events.record(
            tenant_id,
            chat_session.id,
            "human_handoff_requested",
            {
                "handoff_id": handoff.id,
                "agent_id": handoff.agent_id,
                "assignee_user_id": handoff.assignee_user_id,
                "trigger_skill_id": handoff.trigger_skill_id,
                "trigger_step_id": handoff.trigger_step_id,
                "pending_question": handoff.pending_question,
            },
        )
        return handoff

    def _human_handoff_assignee_user_id(
        self, tenant_id: str, agent_id: str | None, fallback_user_id: str | None
    ) -> str | None:
        if agent_id:
            agent = self.db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id, AgentProfile.id == agent_id
                )
            ).first()
            metadata = agent.metadata_json if agent else {}
            if isinstance(metadata, dict):
                for key in (
                    "owner_user_id",
                    "created_by_user_id",
                    "creator_user_id",
                    "created_by",
                    "owner_id",
                ):
                    value = metadata.get(key)
                    if value:
                        return str(value)
        tenant_admin = self._human_handoff_tenant_admin_user_id(tenant_id)
        if tenant_admin:
            return tenant_admin
        return fallback_user_id

    def _human_handoff_tenant_admin_user_id(self, tenant_id: str) -> str | None:
        row = self.db.exec(
            select(User)
            .where(User.tenant_id == tenant_id, User.role == "admin")
            .order_by(User.created_at)
        ).first()
        return row.id if row else None

    def _human_handoff_context_summary(self, chat_session: ChatSession) -> str:
        rows = self.db.exec(
            select(Message)
            .where(Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
            .limit(8)
        ).all()
        lines: list[str] = []
        for message in reversed(rows):
            content = re.sub(r"\s+", " ", message.content or "").strip()
            if not content:
                continue
            lines.append(f"{message.role}: {content[:240]}")
        return "\n".join(lines)

    def _human_handoff_pending_question(
        self, current_step: dict[str, Any] | None, step_result: StepAgentResult
    ) -> str:
        candidates: list[Any] = [
            step_result.reply,
            current_step.get("handoff_question") if current_step else None,
            current_step.get("question") if current_step else None,
            current_step.get("name") if current_step else None,
        ]
        for candidate in candidates:
            text = re.sub(r"\s+", " ", str(candidate or "")).strip()
            if text:
                return text[:600]
        return "当前 SOP 需要人工确认后继续执行。"

    def _generate_reply_segment(
        self,
        message: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        task_results: list[dict[str, object]] | None = None,
    ) -> str:
        return self.response_generator.generate(
            message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )

    def _generate_reply_stream_segment(
        self,
        message: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        task_results: list[dict[str, object]] | None = None,
    ) -> Iterator[str]:
        yield from self.response_generator.generate_stream(
            message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )

    def _task_response_context(
        self,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> dict[str, object]:
        return {
            "task": router_decision.user_intent
            or (active_skill.name if active_skill else "当前任务"),
            "current_step_id": chat_session.active_step_id,
            "skill_content": dict(active_skill.content_json or {}) if active_skill else None,
            "slots": dict(chat_session.slots_json or {}),
            "step_result": step_result.model_dump(mode="json"),
            "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
        }

    def _task_response_draft(self, step_result: StepAgentResult) -> str:
        return str(step_result.reply or "").strip()

    def _try_continue_pending_after_completion(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        skills: list[Skill],
        tools: list[Any],
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        completed_reply: str,
        completed_skill_ids_this_turn: set[str] | None = None,
        turn_task_frames: list[PendingTask] | None = None,
    ) -> QueuedTaskContinuation | None:
        remaining_turn_frames = list(turn_task_frames or [])
        uses_turn_frames = turn_task_frames is not None
        if uses_turn_frames and not remaining_turn_frames:
            return None
        if not uses_turn_frames and not chat_session.pending_tasks_json:
            return None
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        task_results: list[dict[str, object]] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        for queue_round in range(max_actions):
            if uses_turn_frames:
                if not remaining_turn_frames:
                    break
                turn_frame = remaining_turn_frames.pop(0)
                task_id = turn_frame.task_id or f"turn_task_{queue_round + 1}"
            else:
                if not chat_session.pending_tasks_json:
                    break
                task_id = self._next_pending_task_id(chat_session)
                if not task_id:
                    break
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_execution_order_advanced",
                {"task_id": task_id, "queue_round": queue_round + 1},
            )

            for task_id in [task_id]:
                if executed_actions >= max_actions:
                    break
                router_decision = (
                    self._router_decision_from_turn_task_frame(turn_frame)
                    if uses_turn_frames
                    else self._router_decision_from_task_frame(
                        chat_session,
                        task_id,
                        "按 Router 已确定的任务顺序继续执行。",
                    )
                )
                if not router_decision:
                    continue

                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                self.runtime.apply_decision(chat_session, router_decision)
                state_pruned = self._drop_unavailable_skill_state(
                    request.tenant_id, chat_session, skills
                )
                if self._should_record_runtime_event_after_prune(
                    router_decision, chat_session, skills, state_pruned
                ):
                    self._record_runtime_event(
                        request.tenant_id, chat_session, before_skill, before_step, router_decision
                    )
                self.db.commit()
                self.db.refresh(chat_session)

                active_skill = self._get_active_skill(
                    request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
                )
                if not self._should_run_step_agent(router_decision, active_skill):
                    continue
                step_result = self._run_step_agent_with_context_repair(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    router_decision,
                    memory_context,
                    conversation_context,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                tool_result = None
                if step_result.tool_call:
                    step_result, tool_result = self._execute_tool_action_cycle(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        step_result,
                        conversation_context=conversation_context,
                        memory_context=memory_context,
                    )

                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._run_reflection_rounds(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    self._get_reflection_max_rounds(request.tenant_id),
                    conversation_context,
                    completed_skill_ids_this_turn=completed_skill_ids_this_turn,
                    memory_context=memory_context,
                )
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._auto_progress_skill_graph(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    memory_context,
                    conversation_context,
                    completed_skill_ids_this_turn=completed_skill_ids_this_turn,
                )
                task_results.append(
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                )
                draft = self._task_response_draft(step_result)
                if draft:
                    replies, _ = self._merge_queued_reply_segment(replies, draft)
                executed_actions += 1
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if finalize_state == "completed" and active_skill:
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                if finalize_state == "handoff":
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                if finalize_state == "continued":
                    if uses_turn_frames and remaining_turn_frames:
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        continue
                    if self._should_attempt_queued_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        queue_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {
                        "pending_tasks": chat_session.pending_tasks_json or [],
                        "round": queue_round + 1,
                    },
                )
            if executed_actions >= max_actions:
                break

        return self._queued_continuation(
            replies,
            task_results,
            active_skill,
            router_decision,
            step_result,
            tool_result,
        )

    def _queued_continuation(
        self,
        replies: list[str],
        task_results: list[dict[str, object]],
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> QueuedTaskContinuation | None:
        if not replies and not task_results:
            return None
        return QueuedTaskContinuation(
            reply="\n\n".join(replies).strip(),
            task_results=task_results,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
        )

    def _should_attempt_queued_task_followup(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        completed_reply: str,
        schedule_round: int,
    ) -> bool:
        if request.interaction_mode != "scheduled_task":
            return False
        if chat_session.awaiting_input_json:
            return False
        if not chat_session.pending_tasks_json:
            return False

        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        self.db.commit()
        self.db.refresh(chat_session)

        if chat_session.awaiting_input_json or chat_session.active_skill_id:
            return False
        if not chat_session.pending_tasks_json:
            return False

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "scheduled_task_followup_requested",
            {
                "round": schedule_round,
                "pending_tasks": chat_session.pending_tasks_json or [],
                "completed_reply": completed_reply[:500],
                "reason": "scheduled_task_mode_attempts_to_finish_pending_work",
            },
        )
        return True

    def _merge_queued_reply_segment(
        self, replies: list[str], segment: str
    ) -> tuple[list[str], bool]:
        clean_segment = str(segment or "").strip()
        if not clean_segment:
            return replies, False
        return [*replies, clean_segment], False

    def _router_decision_from_task_frame(
        self,
        chat_session: ChatSession,
        task_id: str,
        order_reason: str | None = None,
    ) -> RouterDecision | None:
        frame = self._find_task_frame(chat_session, task_id)
        if not frame:
            return None
        skill_id = frame.get("skill_id") or frame.get("target_skill_id")
        if not skill_id:
            return None
        slot_hints = {}
        if isinstance(frame.get("slots"), dict):
            slot_hints = dict(frame["slots"])
        elif isinstance(frame.get("slot_hints"), dict):
            slot_hints = dict(frame["slot_hints"])
        return RouterDecision(
            decision="switch_to_pending",
            selected_task_id=str(task_id),
            target_skill_id=str(skill_id),
            target_step_id=frame.get("step_id") or frame.get("target_step_id"),
            confidence=float(frame.get("confidence") or 0.0),
            user_intent=frame.get("intent_summary") or frame.get("user_intent"),
            reason=order_reason or frame.get("reason"),
            source_message=frame.get("source_message"),
            slot_hints=slot_hints,
        )

    def _find_task_frame(self, chat_session: ChatSession, task_id: str) -> dict[str, Any] | None:
        for frame in chat_session.pending_tasks_json or []:
            if isinstance(frame, dict) and str(frame.get("task_id") or "") == str(task_id):
                return frame
        return None

    def _turn_followup_task_frames(
        self, router_decision: RouterDecision
    ) -> list[PendingTask]:
        frames = list(router_decision.task_frames or [])
        if not frames:
            return []
        first = frames[0]
        if first.target_skill_id == router_decision.target_skill_id:
            return frames[1:]
        return frames

    def _router_decision_from_turn_task_frame(
        self, frame: PendingTask
    ) -> RouterDecision:
        return RouterDecision(
            decision="start_new_task",
            target_skill_id=frame.target_skill_id,
            target_step_id=frame.target_step_id,
            confidence=frame.confidence,
            user_intent=frame.user_intent,
            reason=frame.reason or "按 Router 本轮 task_frames 顺序继续执行。",
            source_message=frame.source_message,
            slot_hints=dict(frame.slot_hints or {}),
            task_frames=[frame],
        )

    def _next_pending_task_id(self, chat_session: ChatSession) -> str | None:
        for frame in chat_session.pending_tasks_json or []:
            if not isinstance(frame, dict):
                continue
            if str(frame.get("status") or "pending") != "pending":
                continue
            if not (frame.get("skill_id") or frame.get("target_skill_id")):
                continue
            task_id = str(frame.get("task_id") or "").strip()
            if task_id:
                return task_id
        return None

    def _run_reflection_rounds(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        max_rounds: int,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        rounds = max(0, min(max_rounds, REFLECTION_MAX_ROUNDS_LIMIT))
        if rounds <= 0:
            if self._should_try_reflection(router_decision, step_result, tool_result):
                payload = {
                    "needs_retry": False,
                    "reason": "企业端反思轮数配置为 0，已跳过反思。",
                    "target_skill_id": None,
                    "target_step_id": None,
                    "target_tool_name": None,
                    "skipped": True,
                    "skip_reason": "reflection_disabled",
                }
                events = getattr(self, "events", None)
                if events is not None:
                    events.record(request.tenant_id, chat_session.id, "reflection_skipped", payload)
                if stream_events is not None:
                    stream_events.append(("reflection_decision", payload))
            return active_skill, router_decision, step_result, tool_result
        for round_index in range(rounds):
            if not self._should_try_reflection(router_decision, step_result, tool_result):
                break
            if stream_events is not None and round_index > 0:
                stream_events.append(
                    (
                        "status",
                        {
                            "phase": "reflecting",
                            "text": "正在反思",
                            "reflection_round": round_index + 1,
                            "reflection_max_rounds": rounds,
                        },
                    )
                )
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
                retried,
            ) = self._reflect_and_retry(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                conversation_context,
                stream_events,
                completed_skill_ids_this_turn,
                memory_context,
            )
            if not retried:
                break
        return active_skill, router_decision, step_result, tool_result

    def _auto_progress_skill_graph(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        for iteration in range(max_actions):
            active_skill = self._get_active_skill(
                request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
            )
            if not active_skill or not step_result.is_step_completed:
                break
            if step_result.tool_call or step_result.handoff:
                break
            if not self._graph_flow_has_unfinished_work(active_skill, chat_session, step_result):
                break
            if not self._current_step_expected_info_satisfied(active_skill, chat_session):
                break

            payload = {
                "phase": "skill",
                "text": "继续推进 SOP 分支",
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "pending_step_ids": self._graph_pending_steps(chat_session),
                "iteration": iteration + 1,
                "max_iterations": max_actions,
            }
            self.events.record(
                request.tenant_id, chat_session.id, "graph_auto_progress_started", payload
            )
            if stream_events is not None:
                stream_events.append(("status", payload))

            before_state = (
                chat_session.active_step_id,
                tuple(self._graph_pending_steps(chat_session)),
            )
            router_decision = RouterDecision(
                decision="continue_active",
                target_skill_id=active_skill.skill_id,
                target_step_id=chat_session.active_step_id,
                confidence=max(router_decision.confidence, 0.7),
                user_intent=router_decision.user_intent or "继续执行 SOP 图",
                reason="SOP 图还有可自动执行的后续节点。",
                source_message=router_decision.source_message or request.message,
                slot_hints={},
            )
            repair_events: list[tuple[str, dict[str, object]]] | None = (
                [] if stream_events is not None else None
            )
            step_result = self._run_step_agent_with_context_repair(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                router_decision,
                memory_context,
                conversation_context,
                repair_events,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            if repair_events:
                stream_events.extend(repair_events)

            if step_result.knowledge_query:
                knowledge_events: list[tuple[str, dict[str, object]]] | None = (
                    [] if stream_events is not None else None
                )
                step_result = self._execute_knowledge_query_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    memory_context,
                    conversation_context,
                    knowledge_events,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                if knowledge_events:
                    stream_events.extend(knowledge_events)

            if step_result.tool_call:
                tool_events: list[tuple[str, dict[str, object]]] | None = (
                    [] if stream_events is not None else None
                )
                step_result, tool_result = self._execute_tool_action_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    tool_events,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                if tool_events:
                    stream_events.extend(tool_events)

            reflection_events: list[tuple[str, dict[str, object]]] | None = (
                [] if stream_events is not None else None
            )
            active_skill, router_decision, step_result, tool_result = self._run_reflection_rounds(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                self._get_reflection_max_rounds(request.tenant_id),
                conversation_context,
                reflection_events,
                completed_skill_ids_this_turn,
                memory_context,
            )
            if reflection_events:
                stream_events.extend(reflection_events)

            after_state = (
                chat_session.active_step_id,
                tuple(self._graph_pending_steps(chat_session)),
            )
            if (
                after_state == before_state
                and not step_result.tool_call
                and not step_result.knowledge_query
            ):
                break
        return active_skill, router_decision, step_result, tool_result

    def _reflect_and_retry(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None, bool]:
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        if not self._should_try_reflection(router_decision, step_result, tool_result):
            return active_skill, router_decision, step_result, tool_result, False

        try:
            reflection = self.reflection_agent.review(
                request.message,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                skills,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "reflection_error",
                {"message": str(exc)},
            )
            if stream_events is not None:
                stream_events.append(
                    (
                        "reflection_decision",
                        {
                            "needs_retry": False,
                            "reason": f"反思失败：{exc}",
                            "target_skill_id": None,
                            "target_step_id": None,
                            "target_tool_name": None,
                        },
                    )
                )
            return active_skill, router_decision, step_result, tool_result, False

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_decision_created",
            reflection.model_dump(),
        )
        if stream_events is not None:
            stream_events.append(("reflection_decision", reflection.model_dump(mode="json")))
        if not reflection.needs_retry:
            return active_skill, router_decision, step_result, tool_result, False

        retry_tool_call = self._tool_call_from_reflection(
            reflection,
            chat_session,
            tools,
            request.message,
        )
        if retry_tool_call and self._reflection_tool_retry_targets_current_skill(
            reflection, chat_session
        ):
            retry_result = self._retry_with_reflection_tool_call(
                request,
                chat_session,
                active_skill,
                router_decision,
                retry_tool_call,
                reflection.reason,
                stream_events,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
            return (*retry_result, True)

        retry_router_decision = self._router_decision_from_reflection(
            reflection,
            chat_session,
            skills,
            router_decision,
            completed_skill_ids_this_turn,
        )
        if retry_router_decision:
            retry_result = self._retry_with_router_decision(
                request,
                chat_session,
                skills,
                tools,
                retry_router_decision,
                model_config,
                conversation_context,
                stream_events,
                memory_context,
            )
            return (*retry_result, True)

        if retry_tool_call:
            retry_result = self._retry_with_reflection_tool_call(
                request,
                chat_session,
                active_skill,
                router_decision,
                retry_tool_call,
                reflection.reason,
                stream_events,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
            return (*retry_result, True)

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_skipped",
            {
                "reason": reflection.reason,
                "target_skill_id": reflection.target_skill_id,
                "target_tool_name": reflection.target_tool_name,
            },
        )
        return active_skill, router_decision, step_result, tool_result, False

    def _retry_with_reflection_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        retry_tool_call: ToolCall,
        retry_reason: str | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        tools: list[Tool] | None = None,
        model_config: ModelConfig | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        retry_step_result = StepAgentResult(
            tool_call=retry_tool_call,
            next_step_id=chat_session.active_step_id,
            is_step_completed=True,
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_started",
            {
                "mode": "tool",
                "reason": retry_reason,
                "target_tool_name": retry_tool_call.name,
            },
        )
        retry_step_result, retry_tool_result = self._execute_tool_action_cycle(
            request,
            chat_session,
            active_skill,
            tools or [],
            model_config,
            retry_step_result,
            stream_events,
            conversation_context=conversation_context,
            memory_context=memory_context,
        )
        return active_skill, router_decision, retry_step_result, retry_tool_result

    def _reflection_tool_retry_targets_current_skill(
        self, reflection: ReflectionDecision, chat_session: ChatSession
    ) -> bool:
        return bool(
            reflection.target_tool_name
            and (
                not reflection.target_skill_id
                or reflection.target_skill_id == chat_session.active_skill_id
            )
        )

    def _retry_with_router_decision(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        router_decision: RouterDecision,
        model_config: ModelConfig,
        conversation_context: dict[str, object],
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_started",
            {
                "mode": "skill",
                "target_skill_id": router_decision.target_skill_id,
                "target_step_id": router_decision.target_step_id,
                "reason": router_decision.reason,
            },
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "router_decision_created",
            router_decision.model_dump(),
        )

        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.apply_decision(chat_session, router_decision)
        state_pruned = self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if self._should_record_runtime_event_after_prune(
            router_decision, chat_session, skills, state_pruned
        ):
            self._record_runtime_event(
                request.tenant_id, chat_session, before_skill, before_step, router_decision
            )
        self.db.commit()
        self.db.refresh(chat_session)

        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        if stream_events is not None:
            stream_events.append(
                (
                    "skill_state",
                    self._skill_state_payload(
                        chat_session,
                        skills,
                        self._runtime_stream_context(
                            router_decision, before_skill, before_step, chat_session
                        ),
                    ),
                )
            )
            stream_events.append(
                (
                    "status",
                    {
                        "phase": "stepping",
                        "text": "正在思考",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                    },
                )
            )

        step_result = self._run_step_agent_with_context_repair(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context=memory_context,
            conversation_context=conversation_context,
            stream_events=stream_events,
        )
        self.db.commit()
        self.db.refresh(chat_session)

        tool_result: ToolResult | None = None
        if step_result.tool_call:
            step_result, tool_result = self._execute_tool_action_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        return active_skill, router_decision, step_result, tool_result

    def _tool_loop_decision_payload(
        self,
        iteration: int,
        mode: str,
        tool_call: ToolCall | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"mode": mode, "iteration": iteration}
        if tool_call:
            payload["tool_call"] = tool_call.model_dump(mode="json")
            payload["target_tool_name"] = tool_call.name
        return payload

    def _execute_tool_action_cycle(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig | None,
        step_result: StepAgentResult,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[StepAgentResult, ToolResult | None]:
        tool_result: ToolResult | None = None
        current_knowledge = list(step_result.knowledge_results or [])
        seen_calls: set[str] = set()
        max_actions = self._get_agent_loop_max_actions(request.tenant_id)
        for iteration in range(max_actions):
            tool_call = step_result.tool_call
            if not tool_call:
                break
            tool_call_id = new_id("toolcall")
            signature = self._tool_call_signature(tool_call)
            if signature in seen_calls:
                if tool_result and tool_result.success and step_result.reply:
                    step_result = step_result.model_copy(
                        update={"tool_call": None, "is_step_completed": True}
                    )
                    payload = self._tool_loop_decision_payload(
                        iteration + 1, "respond_after_duplicate"
                    )
                    self.events.record(
                        request.tenant_id, chat_session.id, "agent_loop_completed", payload
                    )
                    if stream_events is not None:
                        stream_events.append(("agent_loop_completed", payload))
                    break
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "agent_loop_stopped",
                    {"reason": "duplicate_tool_call", "tool_call": tool_call.model_dump()},
                )
                break
            seen_calls.add(signature)
            self._emit_tool_status(tool_call, tool_call_id, stream_events, status_callback)
            tool_result = self._execute_tool_call(
                request,
                chat_session,
                tool_call,
                tool_call_id,
                stream_events=stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
            self._record_tool_result_in_slots(chat_session, tool_call, tool_result)
            if stream_events is not None:
                stream_events.append(
                    (
                        "tool_result",
                        self._tool_activity_payload(
                            request.tenant_id,
                            tool_call.name,
                            tool_result,
                            tool_call,
                            tool_call_id,
                        ),
                    )
                )
            self.db.commit()
            self.db.refresh(chat_session)
            if not tool_result.success:
                if (
                    model_config
                    and tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
                    and active_skill is not None
                ):
                    self._emit_thinking_status(
                        chat_session, iteration + 1, stream_events, status_callback
                    )
                    continuation_result = self._run_step_agent_once(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        repair_reason="tool_continuation",
                        repair_context=self._tool_continuation_context(
                            request.tenant_id,
                            tool_call,
                            tool_result,
                            chat_session,
                            iteration + 1,
                        ),
                        memory_context=memory_context,
                        conversation_context=conversation_context,
                        current_knowledge=current_knowledge,
                        allow_general_skill_selection=False,
                    )
                    self._apply_step_result(
                        request.tenant_id,
                        chat_session,
                        continuation_result,
                        active_skill,
                    )
                    self.db.commit()
                    self.db.refresh(chat_session)
                    step_result = continuation_result
                break

            if not model_config:
                self._advance_after_successful_tool(
                    request.tenant_id, chat_session, active_skill, step_result, tool_result
                )
                self.db.commit()
                self.db.refresh(chat_session)
                break

            self._emit_thinking_status(chat_session, iteration + 1, stream_events, status_callback)
            continuation_result = self._run_step_agent_once(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                repair_reason="tool_continuation",
                repair_context=self._tool_continuation_context(
                    request.tenant_id,
                    tool_call,
                    tool_result,
                    chat_session,
                    iteration + 1,
                ),
                memory_context=memory_context,
                conversation_context=conversation_context,
                current_knowledge=current_knowledge,
                allow_general_skill_selection=False,
            )
            if current_knowledge and not continuation_result.knowledge_results:
                continuation_result.knowledge_results = current_knowledge
            self._apply_step_result(
                request.tenant_id, chat_session, continuation_result, active_skill
            )
            self.db.commit()
            self.db.refresh(chat_session)
            step_result = continuation_result
            if step_result.tool_call:
                payload = self._tool_loop_decision_payload(
                    iteration + 1,
                    "model_tool_call",
                    step_result.tool_call,
                )
                self.events.record(
                    request.tenant_id, chat_session.id, "agent_loop_continued", payload
                )
                if stream_events is not None:
                    stream_events.append(("agent_loop_continued", payload))
                continue

            payload = self._tool_loop_decision_payload(iteration + 1, "respond")
            self.events.record(request.tenant_id, chat_session.id, "agent_loop_completed", payload)
            if stream_events is not None:
                stream_events.append(("agent_loop_completed", payload))
            self._advance_after_successful_tool(
                request.tenant_id,
                chat_session,
                active_skill,
                StepAgentResult(
                    tool_call=tool_call,
                    next_step_id=step_result.next_step_id,
                    is_step_completed=True,
                ),
                tool_result,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            break
        return step_result, tool_result

    def _execute_knowledge_query_cycle(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        step_result: StepAgentResult,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> StepAgentResult:
        query = step_result.knowledge_query
        if not query or not query.query.strip():
            return step_result
        payload = {
            "phase": "knowledge",
            "text": "正在检索知识",
            "query": query.model_dump(mode="json"),
            "active_skill_id": chat_session.active_skill_id,
            "active_step_id": chat_session.active_step_id,
        }
        self.events.record(request.tenant_id, chat_session.id, "knowledge_query_started", payload)
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("knowledge", payload)

        knowledge_base_ids = self._agent_visible_knowledge_base_ids(
            request.tenant_id,
            chat_session.agent_id,
        )
        if (
            self._agent_requires_resource_filter(request.tenant_id, chat_session.agent_id)
            and not knowledge_base_ids
        ):
            search_response = KnowledgeSearchResponse(
                selected_buckets=[],
                chunks=[],
                trace=[],
                route_trace=[],
                selected_documents=[],
                expanded_sections=[],
                evidence_pack=[],
            )
        else:
            search_query = query.query.strip()
            original_message = request.message.strip()
            if original_message and original_message not in search_query:
                search_query = f"{search_query}\n{original_message}"
            search_response = KnowledgeService(self.db).search(
                KnowledgeSearchRequest(
                    tenant_id=request.tenant_id,
                    agent_id=chat_session.agent_id,
                    query=search_query,
                    mode="chat",
                    knowledge_base_ids=knowledge_base_ids,
                    max_chunks=max(1, min(query.max_chunks, 12)),
                    max_buckets=4,
                    max_depth=max(1, min(query.max_depth, 4)),
                    need_evidence_pack=True,
                ),
                model_config,
            )
        knowledge_items = {
            "query": query.model_dump(mode="json"),
            "source_message": request.message,
            "selected_buckets": [
                item.model_dump(mode="json") for item in search_response.selected_buckets
            ],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "knowledge_query_finished",
            knowledge_items,
        )
        if stream_events is not None:
            for trace in search_response.route_trace or search_response.trace:
                stream_events.append(("status", {"phase": "knowledge", **trace}))
            stream_events.append(("knowledge_result", knowledge_items))

        continuation_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            repair_reason="knowledge_continuation",
            repair_context={
                "reason": "knowledge_continuation",
                "knowledge_results": knowledge_items,
                "instruction": "基于知识结果继续判断下一步动作；如果知识足够，推进、调用工具或回复；如果不足，由模型决定是否继续追问或停止。",
            },
            memory_context=memory_context,
            conversation_context=conversation_context,
            current_knowledge=[knowledge_items],
            allow_general_skill_selection=False,
        )
        continuation_result.knowledge_results = [knowledge_items]
        self._apply_step_result(request.tenant_id, chat_session, continuation_result, active_skill)
        return continuation_result

    def _auto_knowledge_step_result(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        router_decision: RouterDecision,
        selection: GeneralSkillSelection | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> StepAgentResult:
        del router_decision
        if selection is None or not selection.use_knowledge:
            return StepAgentResult()

        query_text = (selection.knowledge_query or request.message).strip()
        query = KnowledgeQuery(
            query=query_text,
            reason=selection.reason or "第二轮能力选择判断需要企业知识",
            max_chunks=8,
            max_depth=3,
        )
        payload = {
            "phase": "knowledge",
            "text": "正在检索业务资料",
            "query": query.model_dump(mode="json"),
            "auto": True,
        }
        self.events.record(request.tenant_id, chat_session.id, "knowledge_query_started", payload)
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("knowledge", payload)

        knowledge_items = self._knowledge_items_for_message(
            request.tenant_id,
            chat_session.agent_id,
            request.message,
            query,
            model_config,
        )
        finished_payload = knowledge_items or {
            "query": query.model_dump(mode="json"),
            "source_message": request.message,
            "selected_buckets": [],
            "chunks": [],
            "trace": [],
            "selected_documents": [],
            "selected_concepts": [],
            "expanded_sections": [],
            "okf_citations": [],
            "evidence_pack": [],
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "knowledge_query_finished",
            {**finished_payload, "auto": True},
        )
        if stream_events is not None:
            for trace in finished_payload.get("trace") or []:
                stream_events.append(("status", {"phase": "knowledge", **trace}))
            stream_events.append(("knowledge_result", finished_payload))
        return StepAgentResult(
            knowledge_query=query,
            knowledge_results=[knowledge_items] if knowledge_items else [],
        )

    def _knowledge_items_for_message(
        self,
        tenant_id: str,
        agent_id: str,
        message: str,
        query: KnowledgeQuery | None = None,
        model_config: ModelConfig | None = None,
    ) -> dict[str, Any] | None:
        knowledge_base_ids = self._agent_visible_knowledge_base_ids(tenant_id, agent_id)
        if self._agent_requires_resource_filter(tenant_id, agent_id) and not knowledge_base_ids:
            return None
        knowledge_query = query or KnowledgeQuery(
            query=message,
            reason="用户要求基于业务资料或规则回答",
            max_chunks=8,
            max_depth=3,
        )
        search_response = KnowledgeService(self.db).search(
            KnowledgeSearchRequest(
                tenant_id=tenant_id,
                agent_id=agent_id,
                query=knowledge_query.query.strip() or message,
                mode="chat",
                knowledge_base_ids=knowledge_base_ids,
                max_chunks=8,
                max_buckets=4,
                max_depth=3,
                need_evidence_pack=True,
            ),
            model_config,
        )
        if not (
            search_response.selected_concepts
            or search_response.okf_citations
            or search_response.evidence_pack
            or search_response.chunks
        ):
            return None
        return {
            "query": knowledge_query.model_dump(mode="json"),
            "source_message": message,
            "selected_buckets": [
                item.model_dump(mode="json") for item in search_response.selected_buckets
            ],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }

    def _tool_continuation_context(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        tool_result: ToolResult,
        chat_session: ChatSession,
        completed_actions: int,
    ) -> dict[str, object]:
        slots = chat_session.slots_json or {}
        max_actions = self._get_agent_loop_max_actions(tenant_id)
        return {
            "reason": "tool_continuation",
            "previous_tool_call": tool_call.model_dump(mode="json"),
            "previous_tool_result": tool_result.model_dump(mode="json"),
            "accumulated_tool_results": slots.get(TOOL_RESULTS_SLOT, []),
            "tool_call_history": slots.get(TOOL_CALL_HISTORY_SLOT, []),
            "completed_tool_actions_this_turn": completed_actions,
            "max_tool_actions_per_turn": max_actions,
            "instruction": (
                "基于工具结果、slots、当前技能步骤和用户目标判断是否已经完成。"
                "如果还需要工具调用，由模型输出下一次 tool_call；"
                "如果已经足够回复，输出无 tool_call 的结果并推进到可回复步骤。"
                "不要重复调用 tool_call_history 中相同 name + arguments 的工具。"
            ),
        }

    def _emit_tool_status(
        self,
        tool_call: ToolCall,
        tool_call_id: str,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        status_callback: StatusCallback | None,
    ) -> None:
        payload = {
            "phase": "tool",
            "text": f"正在调用工具 {tool_call.name}",
            "tool_name": tool_call.name,
            "tool_call_id": tool_call_id,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("tool", {"tool_name": tool_call.name})

    def _emit_thinking_status(
        self,
        chat_session: ChatSession,
        iteration: int,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        status_callback: StatusCallback | None,
    ) -> None:
        payload = {
            "phase": "stepping",
            "text": "正在思考",
            "active_skill_id": chat_session.active_skill_id,
            "active_step_id": chat_session.active_step_id,
            "repair_reason": "tool_continuation",
            "iteration": iteration,
        }
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback(
                "stepping",
                {
                    "active_skill_id": chat_session.active_skill_id,
                    "active_step_id": chat_session.active_step_id,
                    "repair_reason": "tool_continuation",
                },
            )

    def _run_step_agent_with_context_repair(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> StepAgentResult:
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        selected_general_result = self._preselect_general_skill_for_scene(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
            stream_events,
        )
        if selected_general_result is not None:
            return selected_general_result
        step_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context=memory_context,
            conversation_context=conversation_context,
            allow_general_skill_selection=False,
        )
        self._apply_step_result(request.tenant_id, chat_session, step_result, active_skill)
        step_result = self._retry_slot_validation_if_needed(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            step_result,
            memory_context,
            conversation_context,
        )

        return step_result

    def _preselect_general_skill_for_scene(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None,
        conversation_context: dict[str, object] | None,
        stream_events: list[tuple[str, dict[str, object]]] | None,
    ) -> StepAgentResult | None:
        if active_skill is None:
            return None
        general_query = str(router_decision.general_intent or "").strip()
        if not general_query:
            return None
        skill, selection = self._select_general_capability(
            general_query,
            model_config,
            chat_session.agent_id,
            conversation_context,
            memory_context,
        )
        if skill is None:
            return None
        tool_name = f"{GENERAL_SKILL_TOOL_PREFIX}{skill.slug}"
        if not any(
            getattr(tool, "enabled", False)
            and str(getattr(tool, "name", "") or "") == tool_name
            for tool in tools
        ):
            return None

        query = general_query
        self._validated_general_skill_calls.add(
            self._general_skill_call_key(chat_session.id, tool_name, query)
        )
        selection_payload = {
            "skill_slug": skill.slug,
            "skill_name": skill.name,
            "confidence": selection.confidence,
            "reason": selection.reason,
            "scene_router_decision": router_decision.model_dump(mode="json"),
            "execution_mode": "scene_and_general",
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            selection_payload,
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            selection_payload,
        )
        if stream_events is not None:
            stream_events.extend(
                [
                    ("general_skill_intent_checked", selection_payload),
                    ("general_skill_selected", selection_payload),
                ]
            )

        result = StepAgentResult(
            action="call_tool",
            tool_call=ToolCall(name=tool_name, arguments={"query": query}),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_created",
            {
                **result.model_dump(mode="json"),
                "execution_source": "general_skill_preselection",
            },
        )
        return result

    def _retry_slot_validation_if_needed(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
    ) -> StepAgentResult:
        missing_fields = self._missing_expected_fields(active_skill, chat_session)
        if (
            not missing_fields
            or step_result.tool_call
            or step_result.handoff
            or not self._router_allows_schema_tool_repair(router_decision, chat_session)
            or not self._slot_validation_retry_is_worthwhile(router_decision, step_result)
        ):
            return step_result

        validation_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            repair_reason="slot_validation",
            repair_context={
                "reason": "slot_validation",
                "missing_expected_user_info": missing_fields,
                "previous_step_result": step_result.model_dump(mode="json"),
            },
            memory_context=memory_context,
            conversation_context=conversation_context,
            allow_general_skill_selection=False,
        )
        if not self._step_result_has_progress(
            validation_result
        ) and not self._step_result_has_reply_repair(
            step_result,
            validation_result,
        ):
            return step_result
        if not validation_result.reply and step_result.reply:
            validation_result.reply = step_result.reply
        self._apply_step_result(request.tenant_id, chat_session, validation_result, active_skill)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_repaired",
            {
                "mode": "slot_validation",
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "missing_expected_user_info": missing_fields,
                "slot_updates": validation_result.slot_updates,
                "tool_call": validation_result.tool_call.model_dump()
                if validation_result.tool_call
                else None,
            },
        )
        return validation_result

    def _slot_validation_retry_is_worthwhile(
        self, router_decision: RouterDecision, step_result: StepAgentResult
    ) -> bool:
        if step_result.slot_updates:
            return True
        return router_decision.decision in {
            "start_new_task",
            "continue_active",
        }

    def _step_result_has_progress(self, step_result: StepAgentResult) -> bool:
        return bool(
            step_result.slot_updates
            or step_result.tool_call
            or step_result.knowledge_query
            or step_result.handoff
        )

    def _step_result_has_reply_repair(
        self, previous_result: StepAgentResult, validation_result: StepAgentResult
    ) -> bool:
        previous_reply = (previous_result.reply or "").strip()
        repaired_reply = (validation_result.reply or "").strip()
        return bool(repaired_reply and repaired_reply != previous_reply)

    def _missing_expected_fields(self, skill: Skill | None, chat_session: ChatSession) -> list[str]:
        if not skill:
            return []
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return []
        slots = chat_session.slots_json or {}
        return [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._skill_slot_satisfied(slots, str(field))
        ]

    def _router_allows_schema_tool_repair(
        self, router_decision: RouterDecision, chat_session: ChatSession
    ) -> bool:
        if router_decision.decision not in {
            "start_new_task",
            "continue_active",
        }:
            return False
        if (
            router_decision.target_skill_id
            and chat_session.active_skill_id
            and router_decision.target_skill_id != chat_session.active_skill_id
        ):
            return False
        return True

    def _run_step_agent_once(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision | None = None,
        repair_reason: str | None = None,
        repair_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        current_knowledge: list[dict[str, object]] | None = None,
        allow_general_skill_selection: bool = True,
    ) -> StepAgentResult:
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        recent_messages = [
            message
            for message in conversation_context.get("messages", [])
            if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        ]
        step_result = self.step_agent.run(
            message=request.message,
            session=chat_session,
            skill=active_skill,
            tools=self._step_agent_tools(
                active_skill,
                tools,
                request.message,
                model_config,
                chat_session.agent_id,
                conversation_context,
                memory_context,
                active_step_id=chat_session.active_step_id,
                slots=chat_session.slots_json,
                allow_general_skill_selection=allow_general_skill_selection,
            ),
            model_config=model_config,
            router_decision=router_decision,
            repair_context=repair_context,
            recent_messages=recent_messages,
            memory_context=memory_context,
            conversation_context=conversation_context,
            current_knowledge=current_knowledge,
        )
        payload = step_result.model_dump()
        if repair_reason:
            payload["repair_reason"] = repair_reason
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_created",
            payload,
        )
        return step_result

    def _step_agent_tools(
        self,
        active_skill: Skill | None,
        tools: list[Tool],
        user_message: str | None = None,
        model_config: ModelConfig | None = None,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        *,
        active_step_id: str | None = None,
        slots: dict[str, object] | None = None,
        allow_general_skill_selection: bool = True,
    ) -> list[Tool]:
        if active_skill is None:
            return []
        current_step = self._current_skill_step(active_skill, active_step_id)
        if not current_step:
            return []
        actions = {
            str(action).strip()
            for action in current_step.get("allowed_actions") or []
            if str(action).strip()
        }
        explicit_tool_names = {
            action.split(":", 1)[1]
            for action in actions
            if action.startswith("call_tool:") and ":" in action
        }
        allow_any_tool = "call_tool" in actions
        active_skill_id = active_skill.skill_id
        scoped_tools: list[Tool] = []
        general_skill_tools: list[Tool] = []
        for tool in tools:
            if not getattr(tool, "enabled", False):
                continue
            tool_name = str(getattr(tool, "name", "") or "")
            if tool_name.startswith(GENERAL_SKILL_TOOL_PREFIX):
                if allow_general_skill_selection:
                    general_skill_tools.append(tool)
                continue
            if not allow_any_tool and tool_name not in explicit_tool_names:
                continue
            allowed_skills = [
                str(skill_id)
                for skill_id in (getattr(tool, "allowed_skills_json", None) or [])
                if str(skill_id).strip()
            ]
            if allowed_skills and active_skill_id not in allowed_skills:
                continue
            scoped_tools.append(tool)
        selected_general_tool = self._selected_general_skill_tool_name(
            user_message,
            model_config,
            agent_id,
            general_skill_tools,
            conversation_context,
            memory_context,
        )
        if selected_general_tool:
            scoped_tools.extend(
                tool
                for tool in general_skill_tools
                if str(getattr(tool, "name", "") or "") == selected_general_tool
            )
        return scoped_tools

    def _selected_general_skill_tool_name(
        self,
        user_message: str | None,
        model_config: ModelConfig | None,
        agent_id: str | None,
        general_skill_tools: list[Tool],
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> str | None:
        message = str(user_message or "").strip()
        if not message or not model_config or not general_skill_tools:
            return None
        allowed_slugs = {
            str(getattr(tool, "name", "") or "").removeprefix(GENERAL_SKILL_TOOL_PREFIX)
            for tool in general_skill_tools
            if str(getattr(tool, "name", "") or "").startswith(GENERAL_SKILL_TOOL_PREFIX)
        }
        allowed_slugs = {slug for slug in allowed_slugs if slug}
        if not allowed_slugs:
            return None
        candidates = [
            skill
            for skill in self._list_published_general_skills(model_config.tenant_id, agent_id)
            if skill.slug in allowed_slugs
        ]
        if not candidates:
            return None
        try:
            selection = self.general_skill_selector.decide(
                message,
                candidates,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError:
            return None
        if not selection.use_general_skill or not selection.selected_slug:
            return None
        if selection.selected_slug not in allowed_slugs:
            return None
        return f"{GENERAL_SKILL_TOOL_PREFIX}{selection.selected_slug}"

    def _apply_step_result(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None = None,
    ) -> None:
        if step_result.slot_updates:
            chat_session.slots_json = {
                **(chat_session.slots_json or {}),
                **step_result.slot_updates,
            }
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        if not chat_session.active_skill_id:
            return

        active_skill_matches = bool(
            active_skill and active_skill.skill_id == chat_session.active_skill_id
        )
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            if not self._skill_has_step(active_skill, next_step_id):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "step_agent_result_repaired",
                    {
                        "mode": "invalid_next_step_ignored",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "invalid_next_step_id": step_result.next_step_id,
                    },
                )
                step_result.next_step_id = None
                return

            source_step_id = chat_session.active_step_id
            pending_steps = self._graph_pending_steps(chat_session)
            if pending_steps:
                if next_step_id in pending_steps:
                    pending_steps = [item for item in pending_steps if item != next_step_id]
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                    self._change_active_step(
                        tenant_id,
                        chat_session,
                        next_step_id,
                        reason="graph_merge_step",
                    )
                    return

                if next_step_id not in pending_steps:
                    pending_steps.append(next_step_id)
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                if self._activate_next_pending_graph_step(
                    tenant_id,
                    chat_session,
                    active_skill,
                    reason="graph_sibling_step",
                ):
                    step_result.next_step_id = chat_session.active_step_id
                return

            self._queue_graph_sibling_steps(
                tenant_id,
                chat_session,
                active_skill,
                source_step_id,
                next_step_id,
            )

        if step_result.next_step_id:
            self._change_active_step(tenant_id, chat_session, str(step_result.next_step_id).strip())
            return

        if active_skill_matches and step_result.is_step_completed:
            if self._activate_next_pending_graph_step(
                tenant_id,
                chat_session,
                active_skill,
                reason="graph_pending_step",
            ):
                step_result.next_step_id = chat_session.active_step_id

    def _change_active_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        next_step_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        if previous_step == next_step_id:
            return
        payload: dict[str, Any] = {
            "from_skill_id": chat_session.active_skill_id,
            "to_skill_id": chat_session.active_skill_id,
            "from_step_id": previous_step,
            "to_step_id": next_step_id,
        }
        if reason:
            payload["reason"] = reason
        self.events.record(tenant_id, chat_session.id, "skill_step_changed", payload)

    def _graph_pending_steps(self, chat_session: ChatSession) -> list[str]:
        value = (chat_session.slots_json or {}).get(GRAPH_PENDING_STEPS_SLOT)
        if not isinstance(value, list):
            return []
        pending: list[str] = []
        for item in value:
            step_id = str(item or "").strip()
            if step_id and step_id not in pending:
                pending.append(step_id)
        return pending

    def _store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        slots = dict(chat_session.slots_json or {})
        normalized = []
        for item in pending_steps:
            step_id = str(item or "").strip()
            if step_id and step_id not in normalized:
                normalized.append(step_id)
        if normalized:
            slots[GRAPH_PENDING_STEPS_SLOT] = normalized
        else:
            slots.pop(GRAPH_PENDING_STEPS_SLOT, None)
        chat_session.slots_json = slots
        self.events.record(
            tenant_id,
            chat_session.id,
            "graph_pending_steps_updated",
            {"pending_step_ids": normalized},
        )

    def _queue_graph_sibling_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        source_step_id: str | None,
        selected_step_id: str,
    ) -> None:
        if not source_step_id:
            return
        outgoing = self._graph_outgoing_edges(active_skill).get(source_step_id) or []
        selected_conditions = {
            self._edge_condition(edge)
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip() == selected_step_id
        }
        sibling_steps = [
            str(edge.get("next_node_id") or "").strip()
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip()
            and str(edge.get("next_node_id") or "").strip() != selected_step_id
            and self._edge_condition(edge) in selected_conditions
        ]
        if not sibling_steps:
            return
        pending_steps = self._graph_pending_steps(chat_session)
        for step_id in sibling_steps:
            if step_id not in pending_steps:
                pending_steps.append(step_id)
        self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def _edge_condition(self, edge: dict[str, Any]) -> str:
        return str(edge.get("condition") or "").strip().lower()

    def _activate_next_pending_graph_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        *,
        reason: str,
    ) -> bool:
        pending_steps = self._graph_pending_steps(chat_session)
        while pending_steps:
            next_step_id = pending_steps.pop(0)
            if not self._skill_has_step(active_skill, next_step_id):
                continue
            self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
            self._change_active_step(tenant_id, chat_session, next_step_id, reason=reason)
            return True
        self._store_graph_pending_steps(tenant_id, chat_session, [])
        return False

    def _skill_has_step(self, skill: Skill, step_id: str | None) -> bool:
        if not step_id:
            return False
        return any(node.get("node_id") == step_id for node in self._skill_nodes(skill))

    def _step_actions(self, step: dict[str, Any]) -> list[str]:
        return [
            action
            for action in (_normalize_action(item) for item in step.get("allowed_actions", []))
            if action
        ]

    def _record_tool_result_in_slots(
        self,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> None:
        slots = dict(chat_session.slots_json or {})
        history = self._tool_call_history(slots)
        signature = self._tool_call_signature(tool_call)
        if signature not in {self._tool_history_signature(item) for item in history}:
            history.append({"tool_name": tool_call.name, "arguments": tool_call.arguments})

        results = slots.get(TOOL_RESULTS_SLOT)
        result_items = list(results) if isinstance(results, list) else []
        result_items.append(
            {
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
                "success": tool_result.success,
                "data": tool_result.data,
                "error": tool_result.error.model_dump() if tool_result.error else None,
            }
        )
        slots[TOOL_CALL_HISTORY_SLOT] = history
        slots[TOOL_RESULTS_SLOT] = result_items
        chat_session.slots_json = slots

    def _tool_call_history(self, slots: dict[str, Any]) -> list[dict[str, Any]]:
        history = slots.get(TOOL_CALL_HISTORY_SLOT)
        if not isinstance(history, list):
            return []
        return [item for item in history if isinstance(item, dict)]

    def _tool_history_signature(self, item: dict[str, Any]) -> str:
        return self._tool_signature(
            str(item.get("tool_name") or ""),
            item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
        )

    def _tool_call_signature(self, tool_call: ToolCall) -> str:
        return self._tool_signature(tool_call.name, tool_call.arguments)

    def _tool_signature(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _tool_idempotency_config(self, tool: Tool) -> tuple[bool | None, list[str] | None]:
        raw_config = tool.config_json if isinstance(tool.config_json, dict) else {}
        raw_schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        config = raw_config.get("idempotency", raw_config.get("idempotency_policy"))
        if config is None:
            config = raw_schema.get("x-idempotency", raw_schema.get("x_idempotency"))
        enabled: bool | None = None
        key_fields: list[str] | None = None
        if isinstance(config, dict):
            enabled = self._idempotency_enabled_value(
                config.get("enabled", config.get("mode", config.get("scope")))
            )
            fields = (
                config.get("key_fields") or config.get("fields") or config.get("argument_fields")
            )
            if isinstance(fields, list):
                key_fields = [str(item).strip() for item in fields if str(item).strip()]
        else:
            enabled = self._idempotency_enabled_value(config)

        requires_confirmation = raw_config.get(
            "requires_confirmation", raw_schema.get("requires_confirmation")
        )
        confirmation_enabled = self._idempotency_enabled_value(requires_confirmation)
        if enabled is None and confirmation_enabled is True:
            enabled = True
        return enabled, key_fields

    def _idempotency_enabled_value(self, value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
            "enable",
            "replay",
            "session",
            "session_arguments",
        }:
            return True
        if normalized in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
            "disable",
            "none",
            "read_only",
            "readonly",
        }:
            return False
        return None

    def _tool_requires_idempotent_replay(
        self, tenant_id: str, tool_call: ToolCall
    ) -> tuple[bool, list[str] | None]:
        if tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX):
            return False, None
        if not hasattr(self.db, "exec"):
            return False, None
        statement = select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
        no_autoflush = getattr(self.db, "no_autoflush", None)
        if no_autoflush is None:
            tool = self.db.exec(statement).first()
        else:
            with no_autoflush:
                tool = self.db.exec(statement).first()
        if not tool:
            return False, None
        configured, key_fields = self._tool_idempotency_config(tool)
        if configured is not None:
            return configured, key_fields
        method = str(tool.method or "").upper()
        if method not in IDEMPOTENT_WRITE_METHODS:
            return False, None
        return True, key_fields

    def _idempotency_arguments(
        self, arguments: dict[str, Any], key_fields: list[str] | None
    ) -> dict[str, Any]:
        if not key_fields:
            return arguments
        return {field: arguments.get(field) for field in key_fields if field in arguments}

    def _previous_successful_side_effect_tool_result(
        self,
        tenant_id: str,
        session_id: str,
        tool_call: ToolCall,
    ) -> tuple[ToolResult, str] | None:
        replay_enabled, key_fields = self._tool_requires_idempotent_replay(tenant_id, tool_call)
        if not replay_enabled:
            return None
        target_signature = self._tool_signature(
            tool_call.name,
            self._idempotency_arguments(tool_call.arguments, key_fields),
        )
        rows = self.db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == tenant_id,
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == "tool_call_finished",
            )
            .order_by(AgentEvent.created_at.desc())
            .limit(200)
        ).all()
        for event in rows:
            payload = event.payload_json or {}
            if payload.get("success") is not True:
                continue
            payload_tool_name = str(payload.get("tool_name") or "").strip()
            if payload_tool_name != tool_call.name:
                continue
            payload_tool_call = (
                payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
            )
            payload_arguments = payload_tool_call.get("arguments")
            if not isinstance(payload_arguments, dict):
                payload_arguments = (
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                )
            replay_arguments = self._idempotency_arguments(payload_arguments, key_fields)
            if self._tool_signature(payload_tool_name, replay_arguments) != target_signature:
                continue
            replay_data = payload.get("data")
            if isinstance(replay_data, dict):
                replay_data = {
                    **replay_data,
                    "idempotent_replay": True,
                    "replayed_from_event_id": event.id,
                }
            else:
                replay_data = {
                    "result": replay_data,
                    "idempotent_replay": True,
                    "replayed_from_event_id": event.id,
                }
            return ToolResult(tool_name=tool_call.name, success=True, data=replay_data), event.id
        return None

    def _execute_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_call_id: str | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult:
        if (
            not tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
            and chat_session.agent_id
            and tool_call.name
            not in {
                row.name
                for row in self._list_enabled_tools(request.tenant_id, chat_session.agent_id)
            }
        ):
            tool_result = ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="NOT_ALLOWED", message="当前员工未启用该工具。"),
            )
            started_payload = tool_call.model_dump(mode="json")
            if tool_call_id:
                started_payload["tool_call_id"] = tool_call_id
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_started",
                started_payload,
            )
            finished_payload = tool_result.model_dump(mode="json")
            if tool_call_id:
                finished_payload["tool_call_id"] = tool_call_id
            finished_payload["tool_call"] = tool_call.model_dump(mode="json")
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_finished",
                finished_payload,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            return tool_result

        replayed = self._previous_successful_side_effect_tool_result(
            request.tenant_id,
            chat_session.id,
            tool_call,
        )
        if replayed:
            tool_result, replayed_event_id = replayed
            replay_payload = {
                "tool_name": tool_call.name,
                "tool_call": tool_call.model_dump(mode="json"),
                "replayed_from_event_id": replayed_event_id,
            }
            if tool_call_id:
                replay_payload["tool_call_id"] = tool_call_id
            self.events.record(
                request.tenant_id, chat_session.id, "tool_call_reused", replay_payload
            )
            finished_payload = tool_result.model_dump(mode="json")
            if tool_call_id:
                finished_payload["tool_call_id"] = tool_call_id
            finished_payload["tool_call"] = tool_call.model_dump(mode="json")
            finished_payload["idempotent_replay"] = True
            finished_payload["replayed_from_event_id"] = replayed_event_id
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_finished",
                finished_payload,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            return tool_result

        started_payload = tool_call.model_dump(mode="json")
        if tool_call_id:
            started_payload["tool_call_id"] = tool_call_id
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_started",
            started_payload,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        if tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX):
            if not chat_session.active_skill_id:
                tool_result = ToolResult(
                    tool_name=tool_call.name,
                    success=False,
                    data=None,
                    error=ToolError(
                        code="GENERAL_SKILL_REQUIRES_SCENE_SKILL",
                        message="通用技能只能作为当前场景技能的辅助工具调用。",
                    ),
                )
                finished_payload = tool_result.model_dump(mode="json")
                if tool_call_id:
                    finished_payload["tool_call_id"] = tool_call_id
                finished_payload["tool_call"] = tool_call.model_dump(mode="json")
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "tool_call_finished",
                    finished_payload,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                return tool_result
            tool_result = self._execute_general_skill_tool_call(
                request,
                chat_session,
                tool_call,
                chat_session.agent_id,
                stream_events=stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        else:
            tool_result = self.tool_executor.execute(
                request.tenant_id,
                tool_call,
                chat_session.active_skill_id,
                chat_session.agent_id,
            )
        finished_payload = tool_result.model_dump(mode="json")
        if tool_call_id:
            finished_payload["tool_call_id"] = tool_call_id
        finished_payload["tool_call"] = tool_call.model_dump(mode="json")
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_finished",
            finished_payload,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        return tool_result

    def _execute_general_skill_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        agent_id: str | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult:
        slug = tool_call.name.removeprefix(GENERAL_SKILL_TOOL_PREFIX).strip()
        if not slug:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="INVALID_GENERAL_SKILL", message="通用技能名称为空。"),
            )
        skill = next(
            (
                item
                for item in self._list_published_general_skills(request.tenant_id, agent_id)
                if item.slug == slug
            ),
            None,
        )
        if not skill:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="GENERAL_SKILL_NOT_FOUND", message="通用技能不存在或未发布。"),
            )
        try:
            model_config = self._get_request_model(request, agent_id)
        except AgentLoopPreconditionError as exc:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code=exc.code.upper(), message=exc.message),
            )
        if not model_config:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="MISSING_MODEL_CONFIG", message="没有默认模型配置。"),
            )
        query = str(tool_call.arguments.get("query") or request.message).strip()
        guard_result = self._validate_general_skill_tool_match(
            request,
            chat_session,
            tool_call,
            skill,
            query,
            model_config,
            agent_id,
            conversation_context,
            memory_context,
        )
        if guard_result is not None:
            return guard_result
        emitted_trace_keys: set[str] = set()

        def trace_key(trace_item: dict[str, Any]) -> str:
            return json.dumps(trace_item, ensure_ascii=False, sort_keys=True, default=str)

        def emit_general_skill_trace(trace_item: dict[str, Any]) -> None:
            emitted_trace_keys.add(trace_key(trace_item))
            payload: dict[str, object] = {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                **trace_item,
            }
            self.events.record(request.tenant_id, chat_session.id, "general_skill_trace", payload)
            if stream_events is not None:
                stream_events.append(("general_skill_trace", payload))

        def trace_sink(trace_item: dict[str, Any]) -> None:
            emit_general_skill_trace(trace_item)

        try:
            response = self.general_skill_runner.run(
                skill,
                query,
                model_config,
                request.user_id,
                event_sink=trace_sink,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="GENERAL_SKILL_EXECUTION_ERROR", message=str(exc)),
            )
        for trace_item in response.execution_trace:
            if trace_key(trace_item) not in emitted_trace_keys:
                emit_general_skill_trace(trace_item)
        structured = (
            response.structured_result if isinstance(response.structured_result, dict) else {}
        )
        success = structured.get("success")
        is_success = True if success is None else bool(success)
        finished_payload: dict[str, object] = {
            "skill_slug": response.skill_slug,
            "success": is_success,
            "stdout_preview": response.stdout[:600],
            "stderr_preview": response.stderr[:600],
            "structured_result": response.structured_result,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id, chat_session.id, "general_skill_run_finished", finished_payload
        )
        if stream_events is not None:
            stream_events.append(("general_skill_run_finished", finished_payload))
        data = {
            "skill_slug": response.skill_slug,
            "reply": response.reply,
            "structured_result": response.structured_result,
            "stdout": response.stdout,
            "stderr": response.stderr,
            "generated_code": response.generated_code,
            "execution_trace": response.execution_trace,
        }
        if is_success:
            return ToolResult(tool_name=tool_call.name, success=True, data=data, error=None)
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=data,
            error=ToolError(
                code=str(structured.get("error") or "GENERAL_SKILL_FAILED"),
                message=str(structured.get("message") or response.reply or "通用技能执行失败。"),
            ),
        )

    def _validate_general_skill_tool_match(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        requested_skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        agent_id: str | None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult | None:
        call_key = self._general_skill_call_key(
            chat_session.id,
            tool_call.name,
            query,
        )
        if call_key in self._validated_general_skill_calls:
            self._validated_general_skill_calls.discard(call_key)
            return None
        if not query:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "通用技能调用缺少自然语言任务。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_MISMATCH", message="通用技能调用缺少自然语言任务。"
                ),
            )
        candidates = self._list_published_general_skills(request.tenant_id, agent_id)
        if not candidates:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "当前员工没有可用通用技能。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_NOT_FOUND", message="当前员工没有可用通用技能。"
                ),
            )
        try:
            selection = self.general_skill_selector.decide(
                query,
                candidates,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError:
            return None
        selected_slug = selection.selected_slug if selection.use_general_skill else None
        if selected_slug == requested_skill.slug:
            return None
        payload = {
            "requested_slug": requested_skill.slug,
            "selected_slug": selected_slug,
            "reason": selection.reason,
            "query": query,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id, chat_session.id, "general_skill_guard_rejected", payload
        )
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=payload,
            error=ToolError(
                code="GENERAL_SKILL_MISMATCH",
                message="通用技能与当前子任务不匹配，已取消调用。",
            ),
        )

    def _general_skill_call_key(
        self,
        session_id: str,
        tool_name: str,
        query: str,
    ) -> tuple[str, str, str]:
        return session_id, tool_name.strip(), " ".join(query.split())

    def _advance_after_successful_tool(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if (
            not active_skill
            or not step_result.tool_call
            or step_result.tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
            or (
                step_result.next_step_id and step_result.next_step_id != chat_session.active_step_id
            )
            or not tool_result
            or not tool_result.success
        ):
            return False

        next_step_id = self._next_step_after_successful_tool(
            active_skill, chat_session.active_step_id, chat_session.slots_json or {}
        )
        if not next_step_id:
            return False

        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        step_result.next_step_id = next_step_id
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_step_changed",
            {
                "from_skill_id": chat_session.active_skill_id,
                "to_skill_id": chat_session.active_skill_id,
                "from_step_id": previous_step,
                "to_step_id": next_step_id,
                "reason": "tool_completed",
            },
        )
        return True

    def _next_step_after_successful_tool(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> str | None:
        if not active_step_id:
            return None
        for step in self._next_steps_from_graph(skill, active_step_id):
            step_id = str(step.get("step_id") or "")
            if not step_id:
                continue
            expected = [str(field) for field in step.get("expected_user_info", [])]
            if any(not self._skill_slot_satisfied(slots, field) for field in expected):
                return step_id
            actions = self._step_actions(step)
            if self._actions_allow_final_reply(actions) or any(
                action.startswith("call_tool:") for action in actions
            ):
                return step_id
        return None

    def _router_decision_from_reflection(
        self,
        reflection: ReflectionDecision,
        chat_session: ChatSession,
        skills: list[Skill],
        previous_decision: RouterDecision,
        completed_skill_ids_this_turn: set[str] | None = None,
    ) -> RouterDecision | None:
        if not reflection.target_skill_id:
            return None
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        if (
            reflection.target_skill_id in completed_skill_ids_this_turn
            and chat_session.active_skill_id != reflection.target_skill_id
        ):
            self.events.record(
                chat_session.tenant_id,
                chat_session.id,
                "reflection_retry_skipped_completed_task",
                {
                    "reason": reflection.reason,
                    "target_skill_id": reflection.target_skill_id,
                    "active_skill_id": chat_session.active_skill_id,
                },
            )
            return None
        target_skill = next(
            (skill for skill in skills if skill.skill_id == reflection.target_skill_id),
            None,
        )
        if not target_skill:
            return None
        decision = (
            "continue_active"
            if chat_session.active_skill_id == target_skill.skill_id
            else "start_new_task"
        )
        return RouterDecision(
            decision=decision,
            target_skill_id=target_skill.skill_id,
            target_step_id=reflection.target_step_id or self._first_step_id(target_skill),
            confidence=0.7,
            user_intent=previous_decision.user_intent,
            reason=f"反思重试：{reflection.reason or '当前技能或工具可能不匹配用户诉求'}",
        )

    def _tool_call_from_reflection(
        self,
        reflection: ReflectionDecision,
        chat_session: ChatSession,
        tools: list[Tool],
        user_message: str | None = None,
    ) -> ToolCall | None:
        if not reflection.target_tool_name:
            return None
        tool = next(
            (item for item in tools if item.enabled and item.name == reflection.target_tool_name),
            None,
        )
        if not tool:
            return None
        if str(getattr(tool, "name", "") or "").startswith(GENERAL_SKILL_TOOL_PREFIX):
            query = str(user_message or "").strip()
            if not query:
                return None
            return ToolCall(name=tool.name, arguments={"query": query})
        if (
            chat_session.active_skill_id
            and tool.allowed_skills_json
            and chat_session.active_skill_id not in tool.allowed_skills_json
        ):
            return None
        arguments = self._build_tool_arguments_from_slots(tool, chat_session.slots_json or {})
        required = [str(field) for field in (tool.input_schema or {}).get("required", [])]
        if any(not self._slot_has_value(arguments, field) for field in required):
            return None
        return ToolCall(name=tool.name, arguments=arguments)

    def _build_tool_arguments_from_slots(self, tool: Tool, slots: dict[str, Any]) -> dict[str, Any]:
        schema = tool.input_schema or {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        fields = [str(field) for field in properties]
        for field in schema.get("required", []):
            if str(field) not in fields:
                fields.append(str(field))

        arguments: dict[str, Any] = {}
        for field in fields:
            if self._slot_has_value(slots, field):
                arguments[field] = slots[field]
        used_signatures = {
            self._tool_history_signature(item) for item in self._tool_call_history(slots)
        }
        if self._tool_signature(tool.name, arguments) in used_signatures:
            return {}
        return arguments

    def _should_try_reflection(
        self,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        return action_needs_reflection(router_decision, step_result, tool_result)

    def _first_step_id(self, skill: Skill) -> str | None:
        content = skill.content_json or {}
        start_node_id = str(content.get("start_node_id") or "").strip()
        if start_node_id and self._skill_has_step(skill, start_node_id):
            return start_node_id
        steps = self._skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return [_node_as_step(node) for node in self._ordered_skill_nodes(skill)]

    def _skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        content = skill.content_json or {}
        return [node for node in content.get("nodes", []) if isinstance(node, dict)]

    def _ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        nodes = self._skill_nodes(skill)
        if not nodes:
            return []
        content = skill.content_json or {}
        nodes_by_id = {
            str(node.get("node_id") or ""): node for node in nodes if node.get("node_id")
        }
        start_node_id = str(content.get("start_node_id") or "").strip()
        if not start_node_id or start_node_id not in nodes_by_id:
            start_node_id = str(nodes[0].get("node_id") or "")
        outgoing = self._graph_outgoing_edges(skill)
        ordered: list[dict[str, Any]] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if not node_id or node_id in visited:
                return
            node = nodes_by_id.get(node_id)
            if not node:
                return
            visited.add(node_id)
            ordered.append(node)
            for edge in outgoing.get(node_id, []):
                visit(str(edge.get("next_node_id") or ""))

        visit(start_node_id)
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            if node_id not in visited:
                ordered.append(node)
        return ordered

    def _graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        content = skill.content_json or {}
        edges = [edge for edge in content.get("edges", []) if isinstance(edge, dict)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            source = str(edge.get("source_node_id") or "")
            target = str(edge.get("next_node_id") or "")
            if not source or not target:
                continue
            grouped.setdefault(source, []).append(edge)
        for source, items in grouped.items():
            grouped[source] = sorted(items, key=lambda item: int(item.get("priority") or 0))
        return grouped

    def _next_steps_from_graph(
        self, skill: Skill, active_step_id: str | None
    ) -> list[dict[str, Any]]:
        if not active_step_id:
            return []
        nodes_by_id = {
            str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)
        }
        outgoing = self._graph_outgoing_edges(skill).get(active_step_id, [])
        return [
            nodes_by_id[target_id]
            for target_id in (str(edge.get("next_node_id") or "") for edge in outgoing)
            if target_id in nodes_by_id
        ]

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        nodes_by_id = {
            str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)
        }
        outgoing = self._graph_outgoing_edges(skill).get(active_step_id, [])
        if not outgoing:
            return None
        if len(outgoing) == 1:
            return nodes_by_id.get(str(outgoing[0].get("next_node_id") or ""))
        unconditional = []
        for edge in outgoing:
            condition = str(edge.get("condition") or "").strip().lower()
            if condition in {"", "default", "else"}:
                target = nodes_by_id.get(str(edge.get("next_node_id") or ""))
                if target:
                    unconditional.append(target)
        if len(unconditional) == 1:
            return unconditional[0]
        return None

    def _get_or_create_session(self, request: ChatTurnRequest) -> ChatSession:
        session_id = request.session_id or new_id("session")
        chat_session = self.db.get(ChatSession, session_id)
        if not chat_session:
            chat_session = ChatSession(
                id=session_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
            )
            self.db.add(chat_session)
            self.db.flush()
        elif not chat_session.agent_id and request.agent_id:
            chat_session.agent_id = request.agent_id
        return chat_session

    def _finish_stale_completed_skill(
        self, tenant_id: str, chat_session: ChatSession, skills: list[Skill]
    ) -> None:
        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            chat_session.updated_at = utc_now()
        active_skill = next(
            (skill for skill in skills if skill.skill_id == chat_session.active_skill_id), None
        )
        if active_skill and self._is_terminal_skill_state(active_skill, chat_session):
            self._complete_active_skill(
                tenant_id, chat_session, active_skill, "stale_terminal_state"
            )

    def _should_complete_skill(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        if (
            tool_result
            and tool_result.success
            and self._current_step_can_finish_after_tool(skill, chat_session)
        ):
            return True
        if self._graph_pending_steps(chat_session):
            return False
        if self._is_answer_ready_skill_state(skill, chat_session):
            return True
        if self._is_terminal_skill_state(skill, chat_session):
            return True
        if not step_result.next_step_id and not step_result.tool_call:
            return True
        if self._graph_flow_has_unfinished_work(skill, chat_session, step_result):
            return False
        return self._is_terminal_skill_state(skill, chat_session)

    def _is_terminal_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._is_terminal_skill_position(
            skill, chat_session.active_step_id, chat_session.slots_json or {}
        )

    def _is_answer_ready_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        required = [str(field) for field in (skill.content_json or {}).get("required_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in required
        )

    def _current_step_expected_info_satisfied(
        self, skill: Skill, chat_session: ChatSession
    ) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def _graph_flow_has_unfinished_work(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult | None = None,
    ) -> bool:
        if not skill or chat_session.active_skill_id != skill.skill_id:
            return False
        if self._graph_pending_steps(chat_session):
            return True
        if (
            step_result
            and step_result.next_step_id
            and str(step_result.next_step_id) == str(chat_session.active_step_id)
        ):
            return True
        if not chat_session.active_step_id:
            return False
        return bool(self._graph_outgoing_edges(skill).get(chat_session.active_step_id))

    def _is_terminal_skill_frame(self, skill: Skill, frame: dict[str, Any]) -> bool:
        return self._is_terminal_skill_position(
            skill,
            str(frame.get("step_id") or ""),
            frame.get("slots") if isinstance(frame.get("slots"), dict) else {},
        )

    def _is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        if not active_step_id:
            return False
        content = skill.content_json or {}
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        if not current_step:
            return False

        expected = [str(field) for field in current_step.get("expected_user_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in expected):
            return False

        required = [str(field) for field in content.get("required_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in required):
            return False

        actions = self._step_actions(current_step)
        if not actions:
            return True
        terminal_actions = {
            "answer_user",
            "handoff_human",
            "continue_flow",
            "ask_user",
            "ask_clarification",
        }
        return all(
            action in terminal_actions or action.startswith("call_tool:") for action in actions
        )

    def _current_skill_step(
        self, skill: Skill, active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        for step in self._skill_steps(skill):
            if not isinstance(step, dict):
                continue
            step_ids = {
                str(step.get("step_id") or ""),
                str(step.get("node_id") or ""),
                str(step.get("id") or ""),
            }
            if active_step_id in step_ids:
                return step
        return None

    def _current_step_can_finish_after_tool(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
        actions = [_normalize_action(action) for action in actions]
        return "answer_user" in actions

    def _skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        normalized = field.strip()
        if not normalized:
            return True
        if self._slot_has_value(slots, normalized):
            return True
        return False

    def _slot_has_value(self, slots: dict[str, Any], field: str) -> bool:
        value = slots.get(field)
        return value is not None and value != ""

    def _complete_active_skill(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill, reason: str
    ) -> None:
        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.complete_current_skill(chat_session)
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_completed",
            {
                "skill_id": before_skill or skill.skill_id,
                "step_id": before_step,
                "reason": reason,
                "resumed_skill_id": chat_session.active_skill_id,
                "resumed_step_id": chat_session.active_step_id,
            },
        )

    def _get_request_model(
        self,
        request: ChatTurnRequest,
        agent_id: str | None = None,
        role: str = "default",
    ) -> ModelConfig | None:
        if request.model_config_id:
            row = self.db.get(ModelConfig, request.model_config_id)
            if not row or row.tenant_id != request.tenant_id:
                raise AgentLoopPreconditionError("invalid_model_config", "选中的模型配置不存在。")
            if not row.enabled:
                raise AgentLoopPreconditionError("disabled_model_config", "选中的模型配置已停用。")
            return resolve_model_config_for_runtime(self.db, request.tenant_id, row.id)
        return self._get_default_model(request.tenant_id, agent_id, role)

    def _get_default_model(
        self, tenant_id: str, agent_id: str | None = None, role: str = "default"
    ) -> ModelConfig | None:
        return model_for_agent(self.db, tenant_id, agent_id, role)

    def _get_persona_prompt(self, tenant_id: str, agent_id: str | None = None) -> str | None:
        agent = self._get_agent_profile(tenant_id, agent_id)
        if agent and not agent.is_overall:
            return _agent_identity_prompt(agent)
        if agent and agent.is_overall and agent.persona_prompt:
            return agent.persona_prompt
        row = self.db.get(PersonaConfig, tenant_id)
        return row.system_prompt if row else None

    def _get_reflection_max_rounds(self, tenant_id: str) -> int:
        row = self.db.get(UIConfig, tenant_id)
        value = row.reflection_max_rounds if row else DEFAULT_REFLECTION_MAX_ROUNDS
        return max(0, min(int(value), REFLECTION_MAX_ROUNDS_LIMIT))

    def _get_agent_loop_max_actions(self, tenant_id: str) -> int:
        if not hasattr(self.db, "get"):
            return MAX_TOOL_ACTIONS_PER_TURN
        row = self.db.get(UIConfig, tenant_id)
        value = row.agent_loop_max_actions if row else MAX_TOOL_ACTIONS_PER_TURN
        return max(1, min(int(value), 20))

    def _list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
        return visible_published_skills(self.db, tenant_id, agent_id)

    def _list_published_general_skills(
        self, tenant_id: str, agent_id: str | None = None
    ) -> list[GeneralSkill]:
        agent = self._get_agent_profile(tenant_id, agent_id)
        if not agent or agent.is_overall:
            rows = self.db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id,
                    GeneralSkill.status == "published",
                )
            ).all()
            return [
                row
                for row in rows
                if is_open_gallery_resource(self.db, tenant_id, "general_skill", row)
            ]

        bindings = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).all()
        visible: list[GeneralSkill] = []
        for binding in bindings:
            row = self.db.get(GeneralSkill, binding.resource_id)
            if not row or row.tenant_id != tenant_id or row.status != "published":
                continue
            if is_bound_resource_visible_for_agent(
                self.db,
                tenant_id,
                "general_skill",
                row,
                binding,
            ):
                visible.append(row)
        return visible

    def _select_general_skill(
        self,
        message: str,
        model_config: ModelConfig,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[GeneralSkill, GeneralSkillSelection] | None:
        skill, selection = self._select_general_capability(
            message,
            model_config,
            agent_id,
            conversation_context,
            memory_context,
        )
        if skill is None:
            return None
        return skill, selection

    def _select_general_capability(
        self,
        message: str,
        model_config: ModelConfig,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[GeneralSkill | None, GeneralSkillSelection]:
        general_skills = self._list_published_general_skills(model_config.tenant_id, agent_id)
        try:
            selection = self.general_skill_selector.decide(
                message,
                general_skills,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            return None, GeneralSkillSelection(reason=f"Capability selection failed: {exc}")
        if not selection.use_general_skill or not selection.selected_slug:
            return None, selection
        skill = next(
            (item for item in general_skills if item.slug == selection.selected_slug), None
        )
        if not skill:
            return None, selection.model_copy(
                update={"use_general_skill": False, "selected_slug": None}
            )
        return skill, selection

    def _list_enabled_tools(self, tenant_id: str, agent_id: str | None = None) -> list[Tool]:
        return visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)

    def _tools_with_general_skills(
        self, tenant_id: str, tools: list[Tool], agent_id: str | None = None
    ) -> list[Any]:
        combined: list[Any] = list(tools)
        for skill in self._list_published_general_skills(tenant_id, agent_id):
            combined.append(
                SimpleNamespace(
                    enabled=True,
                    name=f"{GENERAL_SKILL_TOOL_PREFIX}{skill.slug}",
                    display_name=skill.name,
                    description=(
                        f"通用技能：{skill.description or skill.name}。"
                        "仅当当前子任务与该名称、描述和能力边界直接匹配时才能调用；"
                        "不得把它作为场景工具、已有工具结果、知识查询或追问用户的兜底替代。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "传给通用技能的自然语言任务或问题。",
                            }
                        },
                        "required": ["query"],
                    },
                    allowed_skills_json=[],
                )
            )
        return combined

    def _get_agent_profile(self, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
        if not agent_id:
            return None
        row = self.db.get(AgentProfile, agent_id)
        if not row or row.tenant_id != tenant_id or row.status != "active":
            return None
        return row

    def _agent_requires_resource_filter(self, tenant_id: str, agent_id: str | None) -> bool:
        agent = self._get_agent_profile(tenant_id, agent_id)
        return bool(agent and not agent.is_overall)

    def _agent_visible_knowledge_base_ids(self, tenant_id: str, agent_id: str | None) -> list[str]:
        return visible_knowledge_base_ids(self.db, tenant_id, agent_id)

    def _get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None:
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def _drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        available_skill_ids = {skill.skill_id for skill in skills}
        changed = False
        removed_skill_ids: set[str] = set()

        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            changed = True

        def frame_skill_id(frame: object) -> str:
            if not isinstance(frame, dict):
                return ""
            return str(frame.get("target_skill_id") or frame.get("skill_id") or "").strip()

        def keep_frame(frame: object) -> bool:
            skill_id = frame_skill_id(frame)
            if not skill_id:
                return True
            if skill_id in available_skill_ids:
                return True
            removed_skill_ids.add(skill_id)
            return False

        active_skill_id = str(chat_session.active_skill_id or "").strip()
        if active_skill_id and active_skill_id not in available_skill_ids:
            removed_skill_ids.add(active_skill_id)
            chat_session.active_skill_id = None
            chat_session.active_step_id = None
            chat_session.slots_json = {}
            chat_session.awaiting_input_json = None
            chat_session.resume_after_answer_json = None
            changed = True

        for attr in ("pending_tasks_json",):
            value = getattr(chat_session, attr) or []
            if not isinstance(value, list):
                continue
            kept = [frame for frame in value if keep_frame(frame)]
            if len(kept) != len(value):
                setattr(chat_session, attr, kept)
                changed = True

        awaiting = chat_session.awaiting_input_json
        if isinstance(awaiting, dict):
            awaiting_skill_id = str(awaiting.get("skill_id") or "").strip()
            if awaiting_skill_id and awaiting_skill_id not in available_skill_ids:
                removed_skill_ids.add(awaiting_skill_id)
                chat_session.awaiting_input_json = None
                changed = True

        if changed:
            chat_session.updated_at = utc_now()
            if hasattr(self, "events"):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_state_pruned",
                    {"removed_skill_ids": sorted(removed_skill_ids)},
                )
        return changed

    def _should_record_runtime_event_after_prune(
        self,
        router_decision: RouterDecision,
        chat_session: ChatSession,
        skills: list[Skill],
        state_pruned: bool,
    ) -> bool:
        if not state_pruned:
            return True
        if chat_session.active_skill_id:
            return True
        target_skill_id = str(router_decision.target_skill_id or "").strip()
        if not target_skill_id:
            return True
        return target_skill_id in {skill.skill_id for skill in skills}

    def _recent_messages(self, chat_session: ChatSession, limit: int = 8) -> list[dict[str, Any]]:
        if not hasattr(self, "db"):
            return []
        rows = list(
            self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == chat_session.tenant_id,
                    Message.session_id == chat_session.id,
                )
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).all()
        )
        rows.reverse()
        return [self._message_context_entry(row) for row in rows]

    def _conversation_context(
        self,
        chat_session: ChatSession,
        model_config: ModelConfig | None = None,
    ) -> dict[str, object]:
        if not hasattr(self, "db") or not hasattr(self.db, "exec"):
            return build_conversation_context([])
        rows = list(
            self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == chat_session.tenant_id,
                    Message.session_id == chat_session.id,
                )
                .order_by(Message.created_at.asc())
            ).all()
        )
        context = build_conversation_context(
            [self._message_context_entry(row) for row in rows],
            context_state=chat_session.context_state_json,
            summary_builder=self._context_summary_builder(model_config)
            if model_config
            else None,
        )
        next_state = context.get("context_state")
        if isinstance(next_state, dict) and next_state != (chat_session.context_state_json or {}):
            chat_session.context_state_json = next_state
            self.db.add(chat_session)
        return context

    @staticmethod
    def _context_compacted_now(context: dict[str, object] | None) -> bool:
        if not isinstance(context, dict):
            return False
        metadata = context.get("metadata")
        return isinstance(metadata, dict) and metadata.get("compacted_now") is True

    def _context_summary_builder(
        self, model_config: ModelConfig
    ) -> Callable[[str, str, int], str]:
        def summarize(label: str, source: str, token_budget: int) -> str:
            payload = stage_payload(
                phase="Context Compression",
                user_message=f"请压缩{label}",
                conversation_context={},
                memory_context=None,
                instructions=(
                    "把输入的历史对话压缩成一段可供后续对话继续使用的中文事实摘要。"
                    "保留用户身份与偏好、已确认事实、未完成任务、关键约束、工具或知识结论；"
                    "删除寒暄、重复内容、内部 ID、时间戳和推理过程，不新增原文没有的信息。"
                ),
                stage_data={"history_to_compress": source},
                output_contract=(
                    f"只输出一段纯文本摘要，控制在约 {token_budget} tokens 以内。"
                ),
            )
            with llm_operation("context.compact"):
                return LLMClient(model_config).generate_text(
                    unified_system_prompt(), payload
                ).strip()

        return summarize

    def _message_context_entry(self, row: Message) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": row.id,
            "role": row.role,
            "content": message_content_with_attachment_context(row.content, row.metadata_json),
            "created_at": row.created_at,
        }
        images = message_images_from_metadata(row.metadata_json)
        if images and row.role == "user":
            entry["images"] = images
        return entry

    def _assistant_message_metadata(
        self,
        step_result: StepAgentResult | None,
        chat_session: ChatSession,
        source_message: str | None = None,
    ) -> dict[str, Any]:
        knowledge_results = list(step_result.knowledge_results or []) if step_result else []
        citations = self._dedupe_knowledge_citations(
            knowledge_citations_from_results(knowledge_results)
        )
        if not citations:
            return {}
        first_query = next(
            (
                item.get("query")
                for item in knowledge_results
                if isinstance(item.get("query"), dict)
            ),
            None,
        )
        return {
            "knowledge_citations": citations,
            "knowledge_query": first_query or {},
        }

    def _dedupe_knowledge_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            identity = str(
                citation.get("title")
                or citation.get("section_path")
                or citation.get("summary")
                or citation.get("excerpt")
                or citation.get("source_path")
                or citation.get("concept_id")
                or citation.get("id")
                or ""
            )
            key = re.sub(r"\s+", " ", identity).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append({**citation, "label": f"[{len(result) + 1}]"})
            if len(result) >= 4:
                break
        return result

    def _append_message(
        self,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            tenant_id=tenant_id,
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        self.db.add(message)
        return message

    def _persist_cancelled_assistant_message(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        user_message_id: str,
        client_turn_id: str | None = None,
    ) -> Message | None:
        if not user_message_id:
            return None
        user_message = self.db.get(Message, user_message_id)
        if (
            not user_message
            or user_message.tenant_id != tenant_id
            or user_message.session_id != chat_session.id
            or user_message.role != "user"
        ):
            return None

        normalized_client_turn_id = (client_turn_id or "").strip()
        turn_ids = {user_message_id}
        if normalized_client_turn_id:
            turn_ids.add(normalized_client_turn_id)
        existing_messages = self.db.exec(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.session_id == chat_session.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at)
        ).all()
        for row in existing_messages:
            metadata = row.metadata_json or {}
            row_turn_ids = {
                str(metadata.get("turn_id") or "").strip(),
                str(metadata.get("user_message_id") or "").strip(),
                str(metadata.get("client_turn_id") or "").strip(),
            }
            if turn_ids & row_turn_ids:
                return None

        chat_session.updated_at = utc_now()
        chat_session.status = "active"
        chat_session.summary = f"最近回复：{CANCELLED_ASSISTANT_REPLY}"
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            CANCELLED_ASSISTANT_REPLY,
            metadata={
                "turn_id": user_message_id,
                "user_message_id": user_message_id,
                "client_turn_id": normalized_client_turn_id or None,
                "status": "cancelled",
            },
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            {
                "message_id": assistant_message.id,
                "assistant_message_id": assistant_message.id,
                "user_message_id": user_message_id,
                "turn_id": user_message_id,
                "client_turn_id": normalized_client_turn_id or None,
                "reply": CANCELLED_ASSISTANT_REPLY,
                "status": "cancelled",
            },
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )
        return assistant_message

    def _user_message_metadata(self, request: ChatTurnRequest) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if request.client_turn_id:
            metadata["client_turn_id"] = request.client_turn_id
        if request.interaction_mode == "scheduled_task":
            metadata["interaction_mode"] = "scheduled_task"
        if request.model_config_id:
            metadata["model_config_id"] = request.model_config_id
        if request.attachments:
            metadata["attachments"] = [item.model_dump(mode="json") for item in request.attachments]
        return metadata

    def _record_runtime_event(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        before_skill: str | None,
        before_step: str | None,
        decision: RouterDecision,
    ) -> None:
        event_type = "skill_step_changed"
        if decision.decision == "start_new_task":
            event_type = "skill_started"
        elif decision.decision == "switch_to_pending":
            event_type = "skill_resumed"
        elif decision.decision == "complete_task":
            event_type = "skill_exited"
        elif decision.decision == "handoff_human":
            event_type = "handoff_triggered"

        if (
            event_type == "skill_step_changed"
            and before_skill == chat_session.active_skill_id
            and before_step == chat_session.active_step_id
        ):
            return

        payload = {
            "decision": decision.decision,
            "from_skill_id": before_skill,
            "to_skill_id": chat_session.active_skill_id,
            "from_skill_version": self._skill_version(tenant_id, before_skill),
            "to_skill_version": self._skill_version(tenant_id, chat_session.active_skill_id),
            "from_step_id": before_step,
            "to_step_id": chat_session.active_step_id,
        }
        self.events.record(tenant_id, chat_session.id, event_type, payload)

    def _skill_version(self, tenant_id: str, skill_id: str | None) -> str | None:
        if not skill_id:
            return None
        row = self.db.exec(
            select(Skill.version).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
        ).first()
        return str(row) if row else None

    def _runtime_stream_context(
        self,
        decision: RouterDecision,
        before_skill: str | None,
        before_step: str | None,
        chat_session: ChatSession,
    ) -> dict[str, object]:
        return {
            "runtimeDecision": decision.decision,
            "fromSkillId": before_skill,
            "fromStepId": before_step,
            "toSkillId": chat_session.active_skill_id,
            "toStepId": chat_session.active_step_id,
        }

    def _skill_state_payload(
        self,
        chat_session: ChatSession,
        skills: list[Skill],
        runtime_context: dict[str, object] | None = None,
        *,
        user_message_id: str | None = None,
    ) -> dict[str, object]:
        skill_names = {skill.skill_id: skill.name for skill in skills}
        visible_skill_ids = set(skill_names)
        current_skills: list[dict[str, object]] = []
        active_skill_id = (
            chat_session.active_skill_id
            if chat_session.active_skill_id in visible_skill_ids
            else None
        )
        if active_skill_id:
            current_skills.append(
                {
                    "skillId": active_skill_id,
                    "name": skill_names.get(active_skill_id, active_skill_id),
                    "stepId": chat_session.active_step_id,
                    "state": "active",
                }
            )
        for task in chat_session.pending_tasks_json or []:
            if not isinstance(task, dict):
                continue
            skill_id = str(task.get("target_skill_id") or task.get("skill_id") or "").strip()
            if not skill_id or skill_id not in visible_skill_ids:
                continue
            current_skills.append(
                {
                    "skillId": skill_id,
                    "name": skill_names.get(skill_id, skill_id),
                    "stepId": task.get("target_step_id") or task.get("step_id"),
                    "state": task.get("status") or "pending",
                }
            )
        payload = {
            "activeSkillId": active_skill_id,
            "activeStepId": chat_session.active_step_id if active_skill_id else None,
            "currentSkills": current_skills,
            **(runtime_context or {}),
        }
        return self._turn_payload(payload, user_message_id)

    def _tool_activity_payload(
        self,
        tenant_id: str,
        tool_name: str,
        tool_result: ToolResult,
        tool_call: ToolCall | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, object]:
        tool = self.db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_name)
        ).first()
        payload: dict[str, object] = {
            "toolId": tool_name,
            "toolName": tool.display_name or tool.name if tool else tool_name,
            "rawToolName": tool_name,
            "content": tool_result.model_dump(mode="json"),
            "isError": not tool_result.success,
            "success": tool_result.success,
        }
        if tool_call:
            payload["toolCall"] = tool_call.model_dump(mode="json")
            payload["arguments"] = tool_call.arguments
        if tool_call_id:
            payload["toolCallId"] = tool_call_id
        return payload

    def _record_general_skill_run_events(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        run_response: GeneralSkillRunResponse,
        user_message_id: str | None = None,
        include_trace: bool = True,
    ) -> None:
        if include_trace:
            for item in run_response.execution_trace:
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "general_skill_trace",
                    self._turn_payload(
                        {
                            "skill_slug": run_response.skill_slug,
                            **item,
                        },
                        user_message_id,
                    ),
                )
        self.events.record(
            tenant_id,
            chat_session.id,
            "general_skill_run_finished",
            self._turn_payload(
                {
                    "skill_slug": run_response.skill_slug,
                    "success": bool(run_response.structured_result.get("success", True)),
                    "stdout_preview": run_response.stdout[:600],
                    "stderr_preview": run_response.stderr[:600],
                    "structured_result": run_response.structured_result,
                },
                user_message_id,
            ),
        )

    def _enqueue_memory_capture(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
    ) -> list[dict[str, object]]:
        try:
            job = enqueue_memory_capture(
                request,
                chat_session.id,
                step_result,
                tool_result,
                model_config.id,
            )
        except Exception as exc:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_error",
                {"message": str(exc)},
            )
            return []
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "async_job_enqueued",
            {"job_id": job.id, "job_name": job.name, "feature": "memory"},
        )
        self.db.commit()
        return [{"job_id": job.id, "job_name": job.name}]

    def _finish_with_error(
        self, chat_session: ChatSession, code: str, message: str
    ) -> ChatTurnResponse:
        reply = format_runtime_failure_reply(
            "系统配置错误",
            message,
            code,
            "请在管理端补齐配置后重试。",
        )
        self.events.record(
            chat_session.tenant_id,
            chat_session.id,
            "error_occurred",
            {"code": code, "message": message},
        )
        self._finalize_turn(chat_session, chat_session.tenant_id, reply)
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            session_state=public_session(chat_session),
        )

    def _finalize_turn(
        self,
        chat_session: ChatSession,
        tenant_id: str,
        reply: str,
        step_result: StepAgentResult | None = None,
        source_message: str | None = None,
        user_message_id: str | None = None,
    ) -> None:
        chat_session.updated_at = utc_now()
        if chat_session.status != "handoff":
            chat_session.status = "active"
        metadata = self._assistant_message_metadata(step_result, chat_session, source_message)
        reply = self._normalize_reply_citation_labels(reply, metadata.get("knowledge_citations"))
        reply = self._strip_trailing_citation_summary(reply)
        reply, compacted_citations = compact_knowledge_citation_labels(
            reply,
            metadata.get("knowledge_citations"),
        )
        metadata = dict(metadata)
        if compacted_citations:
            metadata["knowledge_citations"] = compacted_citations
        else:
            metadata.pop("knowledge_citations", None)
            metadata.pop("knowledge_query", None)
        if not chat_session.title and source_message:
            fallback_title = self._fallback_session_title_from_message(source_message)
            if fallback_title:
                chat_session.title = fallback_title
        chat_session.summary = f"最近回复：{reply[:120]}"
        assistant_metadata = dict(metadata or {})
        if user_message_id:
            assistant_metadata.setdefault("user_message_id", user_message_id)
            assistant_metadata.setdefault("turn_id", user_message_id)
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            reply,
            metadata=assistant_metadata,
        )
        event_payload: dict[str, Any] = {
            "message_id": assistant_message.id,
            "assistant_message_id": assistant_message.id,
            "reply": reply,
        }
        if user_message_id:
            event_payload["user_message_id"] = user_message_id
            event_payload["turn_id"] = user_message_id
        if assistant_metadata.get("knowledge_citations"):
            event_payload["knowledge_citations"] = assistant_metadata["knowledge_citations"]
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            event_payload,
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )

    def _mark_session_running(self, chat_session: ChatSession) -> None:
        if chat_session.status == "handoff":
            return
        chat_session.status = "running"
        chat_session.updated_at = utc_now()
        self.db.add(chat_session)

    @staticmethod
    def _fallback_session_title_from_message(message: str) -> str:
        title = re.sub(r"\s+", " ", message).strip().strip("。！？!?")
        if not title:
            return ""
        return title[:28]

    def _normalize_reply_citation_labels(self, reply: str, citations: object) -> str:
        if not isinstance(citations, list) or not citations:
            return reply
        max_label = len(citations)

        def replace(match: re.Match[str]) -> str:
            try:
                value = int(match.group(1))
            except ValueError:
                return match.group(0)
            if 1 <= value <= max_label:
                return match.group(0)
            return f"[{max_label if max_label > 1 else 1}]"

        return re.sub(r"\[(\d+)\]", replace, reply)

    def _strip_trailing_citation_summary(self, reply: str) -> str:
        return re.sub(
            r"(?:\n|\s){0,3}(?:参考资料|引用来源|资料来源)\s*[:：]\s*(?:\[\d+\]\s*)+$",
            "",
            reply.rstrip(),
        ).rstrip()
