from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import sleep
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import model_for_agent, visible_knowledge_base_ids, visible_published_skills, visible_skill
from app.core.conversation_context import build_conversation_context
from app.core.reflection_agent import ReflectionAgent, ReflectionDecision, action_needs_reflection
from app.core.response_generator import FALLBACK_REPLY, ResponseGenerator
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    Tool,
    UIConfig,
    new_id,
    utc_now,
)
from app.general_skills import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import GeneralSkillRunResponse, GeneralSkillSelection
from app.knowledge import KnowledgeService
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.llm import LLMError
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService, memory_read
from app.observability import EventLog
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
ROUTER_CONTEXT_MESSAGES = 8
TOOL_CALL_HISTORY_SLOT = "_tool_call_history"
TOOL_RESULTS_SLOT = "_tool_results"
GENERAL_SKILL_TOOL_PREFIX = "general_skill."
PROFILE_NAME_PREFIX = "用户姓名/称呼："
TASK_CONFIRMATION_MARKERS = ("确认下单", "确认购买", "请确认", "是否确认")
TASK_HANDOFF_MARKERS = ("接下来", "然后", "下一步", "随后", "之后")
TASK_PRODUCT_PATTERN = re.compile(r"(?<![A-Za-z0-9])([Aa]\d+|SKU-\d+)(?![A-Za-z0-9])")
KNOWN_PRODUCT_ID_ALIASES = {
    "a1": "A1",
    "a3": "A3",
    "sku001": "SKU-001",
    "sku-001": "SKU-001",
    "sku002": "SKU-002",
    "sku-002": "SKU-002",
    "sku003": "SKU-003",
    "sku-003": "SKU-003",
}


def _normalize_action(action: object) -> str:
    text = str(action or "").strip().strip("`'\"").strip()
    if not text:
        return ""
    if text.startswith("call_tool:"):
        tool_name = text.split(":", 1)[1].strip().strip("`'\"").strip()
        return f"call_tool:{tool_name}" if tool_name else ""
    aliases = {
        "ask": "ask_user",
        "ask_for_info": "ask_user",
        "ask_user_info": "ask_user",
        "reply": "answer_user",
        "respond": "answer_user",
        "transfer_to_human": "handoff_human",
        "human_handoff": "handoff_human",
        "manual_handoff": "handoff_human",
        "manual_service": "handoff_human",
        "escalate_to_human": "handoff_human",
    }
    return aliases.get(text, text)


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
        if key != "preferred_name" and not content.startswith(PROFILE_NAME_PREFIX):
            continue
        name = content.split("：", 1)[1] if "：" in content else content
        name = re.split(r"[。；;，,\n]", name, maxsplit=1)[0].strip()
        name = re.sub(r"^(用户)?(姓名|称呼)?[:：为是叫\s]+", "", name).strip()
        if name:
            return name[:40]
    return ""


def _known_product_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[\s_]+", "-", text.lower())
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    return KNOWN_PRODUCT_ID_ALIASES.get(normalized) or KNOWN_PRODUCT_ID_ALIASES.get(compact, "")


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


@dataclass
class TaskScheduleContinuation:
    reply: str
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

    def _hydrate_router_decision_from_context(
        self,
        chat_session: ChatSession,
        router_decision: RouterDecision,
        skills: list[Skill],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        skills_by_id = {skill.skill_id: skill for skill in skills}
        hydrated: dict[str, Any] = {}

        target_skill = skills_by_id.get(router_decision.target_skill_id or chat_session.active_skill_id or "")
        base_slots = dict(chat_session.slots_json or {})
        base_slots.update(dict(router_decision.slot_hints or {}))
        patch = self._slot_hydration_patch(target_skill, base_slots, memory_context)
        if patch:
            router_decision.slot_hints = {**dict(router_decision.slot_hints or {}), **patch}
            hydrated["primary"] = patch
        remaining_awaiting = self._trim_satisfied_awaiting_fields(router_decision, {**base_slots, **patch})
        if remaining_awaiting is not None:
            hydrated["awaiting_input_expected_fields"] = remaining_awaiting

        task_patches: list[dict[str, Any]] = []
        for task in [*router_decision.pending_tasks, *router_decision.created_tasks]:
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
        if "product_id" in expected_fields and not _slot_has_value(slots, "product_id"):
            product_id = _known_product_id(slots.get("product_name") or slots.get("product_id"))
            if product_id:
                patch["product_id"] = product_id
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
        try:
            prepared = self._prepare_turn(request)
            if prepared.general_response:
                return prepared.general_response
            chat_session = prepared.chat_session
            memory_model_config = prepared.model_config
            router_decision = prepared.router_decision
            step_result = prepared.step_result
            tool_result = prepared.tool_result
            memory_context = prepared.memory_context
            conversation_context = prepared.conversation_context
            if prepared.reply_override is not None:
                reply = prepared.reply_override
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
                completed = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    prepared.active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if completed:
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
            reply = FALLBACK_REPLY
        except Exception as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "AGENT_LOOP_ERROR", "message": str(exc)},
            )
            reply = FALLBACK_REPLY

        if not chat_session:
            chat_session = self._get_or_create_session(request)
        memory_recent_messages = self._recent_messages(chat_session) if memory_model_config else []
        self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
        self.db.commit()
        self.db.refresh(chat_session)
        if memory_model_config:
            self._enqueue_memory_capture(
                request,
                chat_session,
                reply,
                step_result,
                tool_result,
                memory_model_config,
                memory_recent_messages,
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
    ) -> ChatTurnResponse | None:
        if not self._scene_router_deferred_to_general(router_decision):
            return None
        selected = self._select_general_skill(request.message, model_config, chat_session.agent_id)
        if not selected:
            return None
        skill, selection = selected
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "confidence": selection.confidence,
                "reason": selection.reason,
                "scene_router_decision": router_decision.model_dump(mode="json"),
            },
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "confidence": selection.confidence,
                "reason": selection.reason,
                "scene_router_decision": router_decision.model_dump(mode="json"),
            },
        )
        run_response = self.general_skill_runner.run(skill, request.message, model_config, request.user_id)
        self._record_general_skill_run_events(request.tenant_id, chat_session, run_response)
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
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
            conversation_context or self._conversation_context(chat_session),
        )
        self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            reply,
            step_result,
            tool_result,
            model_config,
            self._recent_messages(chat_session),
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
        success = bool(run_response.structured_result.get("success", True)) and not run_response.stderr.strip()
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
            error=None if success else ToolError(code="GENERAL_SKILL_FAILED", message=run_response.stderr or run_response.reply),
        )
        step_result = StepAgentResult(
            reply=run_response.reply,
            is_step_completed=success,
            tool_call=None,
        )
        return step_result, tool_result

    def _scene_router_deferred_to_general(self, router_decision: RouterDecision) -> bool:
        if router_decision.selected_task_id:
            return False
        if router_decision.pending_tasks or router_decision.created_tasks or router_decision.task_updates:
            return False
        return router_decision.decision in {
            "answer_only",
            "clarify",
            "answer_related_question_then_resume",
            "answer_chitchat_then_resume",
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
            "exit_current_skill",
            "handoff",
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
    ) -> Iterator[dict[str, object]]:
        skill, selection = selected_general_skill
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "confidence": selection.confidence,
                "reason": selection.reason,
                "scene_router_decision": router_decision.model_dump(mode="json")
                if router_decision
                else None,
            },
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "confidence": selection.confidence,
                "reason": selection.reason,
                "scene_router_decision": router_decision.model_dump(mode="json")
                if router_decision
                else None,
            },
        )
        yield self._stream_status(
            chat_session,
            "general_skill_intent",
            "正在判断意图",
            {"skill_slug": skill.slug, "skill_name": skill.name, "reason": selection.reason},
        )
        yield self._stream_event(
            "general_skill_trace",
            chat_session,
            {
                "phase": "intent_checked",
                "message": "判断意图",
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "reason": selection.reason,
                "confidence": selection.confidence,
            },
        )
        yield self._stream_status(
            chat_session,
            "general_skill_routing",
            "正在选择通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
        )
        yield self._stream_event(
            "general_skill_state",
            chat_session,
            {"skillSlug": skill.slug, "skillName": skill.name, "state": "selected"},
        )
        yield self._stream_status(
            chat_session,
            "general_skill_running",
            "正在运行通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
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
        model_snapshot = SimpleNamespace(
            api_key_encrypted=model_config.api_key_encrypted,
            base_url=model_config.base_url,
            model=model_config.model,
            temperature=model_config.temperature,
            max_output_tokens=model_config.max_output_tokens,
        )

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
                )
                general_skill_events.put(("complete", response))
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                general_skill_events.put(("error", exc))
            finally:
                general_skill_events.put(None)

        threading.Thread(target=general_skill_worker, daemon=True).start()
        run_response: GeneralSkillRunResponse | None = None
        while True:
            queued = general_skill_events.get()
            if queued is None:
                break
            event_name, payload = queued
            if event_name == "trace":
                yield self._stream_event("general_skill_trace", chat_session, payload)
            elif event_name == "complete":
                run_response = payload
            elif event_name == "error":
                raise payload

        if run_response is None:
            raise LLMError("General skill stream ended without a result")
        self._record_general_skill_run_events(request.tenant_id, chat_session, run_response)
        yield self._stream_status(chat_session, "responding", "正在生成回复")
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
        reply = ""
        for chunk in self._generate_reply_stream_segment(
            request.message,
            chat_session,
            active_skill,
            router_decision or RouterDecision(decision="answer_only", user_intent="通用技能执行结果回复"),
            step_result,
            tool_result,
            model_config,
            persona_prompt
            if persona_prompt is not None
            else self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
            memory_context or [],
            conversation_context or self._conversation_context(chat_session),
        ):
            reply += chunk
            yield self._stream_event("stream_delta", chat_session, {"content": chunk})
            self._pace_stream()
        if not reply.strip():
            reply = run_response.reply
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                self._pace_stream()
        yield self._stream_event("stream_end", chat_session, {})
        self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            reply,
            step_result,
            tool_result,
            model_config,
            self._recent_messages(chat_session),
        )
        result = ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )
        yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))

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
        replace_existing_reply: bool = False,
    ) -> Iterator[dict[str, object]]:
        if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
            return None
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        replaced_current_reply = False
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        for schedule_round in range(max_actions):
            if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
                break
            yield self._stream_status(
                chat_session,
                "routing",
                "正在规划后续任务",
                {"schedule_round": schedule_round + 1},
            )
            schedule = self.router.schedule_tasks_after_completion(
                request.message,
                chat_session,
                skills,
                model_config,
                conversation_context,
                memory_context,
                completed_reply="\n\n".join([completed_reply, *replies]).strip(),
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "task_scheduler_decision_created",
                {**schedule.model_dump(mode="json"), "round": schedule_round + 1},
            )
            yield self._stream_event(
                "task_schedule",
                chat_session,
                {**schedule.model_dump(mode="json"), "round": schedule_round + 1},
            )
            if schedule.action != "run_tasks" or not schedule.selected_task_ids:
                break

            for task_id in schedule.selected_task_ids:
                if executed_actions >= max_actions:
                    break
                router_decision = self._router_decision_from_task_frame(
                    chat_session,
                    task_id,
                    schedule.reason,
                )
                if not router_decision:
                    continue

                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                self.runtime.apply_decision(chat_session, router_decision)
                self._record_runtime_event(
                    request.tenant_id, chat_session, before_skill, before_step, router_decision
                )
                self.db.commit()
                self.db.refresh(chat_session)

                active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
                yield self._stream_event(
                    "skill_state",
                    chat_session,
                    self._skill_state_payload(
                        chat_session,
                        skills,
                        self._runtime_stream_context(router_decision, before_skill, before_step, chat_session),
                    ),
                )
                yield self._stream_status(
                    chat_session,
                    "stepping",
                    "正在思考",
                    {"active_skill_id": chat_session.active_skill_id, "active_step_id": chat_session.active_step_id},
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
                self.db.commit()
                self.db.refresh(chat_session)
                for event_name, payload in repair_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

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
                    )
                    for event_name, payload in tool_stream_events:
                        yield self._stream_event(event_name, chat_session, payload)

                reflection_stream_events: list[tuple[str, dict[str, object]]] = []
                reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
                if reflection_max_rounds > 0 and self._should_try_reflection(router_decision, step_result, tool_result):
                    yield self._stream_status(
                        chat_session,
                        "reflecting",
                        "正在反思",
                        {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
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
                )
                for event_name, payload in reflection_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

                yield self._stream_status(chat_session, "responding", "正在生成回复")
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
                ):
                    chunks.append(chunk)
                    if replace_existing_reply and not replaced_current_reply:
                        yield self._stream_event("stream_replace", chat_session, {"content": ""})
                        replaced_current_reply = True
                    yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                    self._pace_stream()
                segment = "".join(chunks).strip() or FALLBACK_REPLY
                if not chunks:
                    for chunk in self.response_generator.chunk_text(segment):
                        chunks.append(chunk)
                        if replace_existing_reply and not replaced_current_reply:
                            yield self._stream_event("stream_replace", chat_session, {"content": ""})
                            replaced_current_reply = True
                        yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                        self._pace_stream()
                replies, replaced_reply = self._merge_scheduled_reply_segment(replies, segment)
                if replaced_reply:
                    yield self._stream_event(
                        "stream_replace",
                        chat_session,
                        {"content": "\n\n".join(replies).strip()},
                    )
                executed_actions += 1
                completed = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if completed and active_skill:
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                if not completed:
                    if self._should_attempt_scheduled_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        schedule_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    return self._scheduled_continuation(
                        replies, active_skill, router_decision, step_result, tool_result
                    )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {"pending_tasks": chat_session.pending_tasks_json or [], "round": schedule_round + 1},
                )
            if executed_actions >= max_actions:
                break

        return self._scheduled_continuation(replies, active_skill, router_decision, step_result, tool_result)

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        reply = ""
        memory_model_config: ModelConfig | None = None

        try:
            chat_session = self._get_or_create_session(request)
            yield self._stream_event(
                "session_created",
                chat_session,
                {"newSessionId": chat_session.id, "sessionId": chat_session.id},
            )
            yield self._stream_status(chat_session, "received", "已收到消息")
            self._append_message(
                request.tenant_id,
                chat_session.id,
                "user",
                request.message,
                metadata=self._user_message_metadata(request),
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "user_message_received",
                {"message": request.message, "channel": request.channel, "user_id": request.user_id},
            )

            model_config = self._get_request_model(request, chat_session.agent_id)
            if not model_config:
                raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
            memory_model_config = model_config
            skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
            tools = self._tools_with_general_skills(
                request.tenant_id,
                self._list_enabled_tools(request.tenant_id),
                chat_session.agent_id,
            )
            persona_prompt = self._get_persona_prompt(request.tenant_id, chat_session.agent_id)
            if not skills:
                selected_general_skill = self._select_general_skill(request.message, model_config, chat_session.agent_id)
                if selected_general_skill:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        selected_general_skill,
                        None,
                        [],
                        self._conversation_context(chat_session),
                        persona_prompt,
                    )
                    return
                router_decision = RouterDecision(
                    decision="answer_only",
                    reason="No published scene skills are available; answer as chat.",
                )
                knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    stream_events=knowledge_stream_events,
                )
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)
                yield self._stream_status(chat_session, "responding", "正在生成回复")
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
                    self._conversation_context(chat_session),
                ):
                    reply += chunk
                    yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                    self._pace_stream()
                yield self._stream_event("stream_end", chat_session, {})
                self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))
                return
            self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
            memory_context = [
                memory_read(row)
                for row in self.memory.recall(
                    request.tenant_id,
                    request.user_id,
                    request.message,
                    model_config=model_config,
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
            conversation_context = self._conversation_context(chat_session)
            router_context = self._conversation_context(chat_session, max_messages=ROUTER_CONTEXT_MESSAGES)

            yield self._stream_status(chat_session, "routing", "正在判断用户意图")
            router_decision = self.router.decide(
                request.message, chat_session, skills, model_config, router_context, memory_context
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
                router_decision.model_dump(),
            )
            if self._scene_router_deferred_to_general(router_decision):
                selected_general_skill = self._select_general_skill(request.message, model_config, chat_session.agent_id)
                if selected_general_skill:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        selected_general_skill,
                        router_decision,
                        memory_context,
                        conversation_context,
                        persona_prompt,
                    )
                    return

            initial_schedule_decision = self._initial_scheduler_queue_decision(
                request,
                chat_session,
                router_decision,
            )
            if initial_schedule_decision:
                self.runtime.apply_decision(chat_session, initial_schedule_decision)
                self._release_active_task_to_scheduler(chat_session, initial_schedule_decision)
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "task_frames_seeded_for_scheduler",
                    initial_schedule_decision.model_dump(mode="json"),
                )
                self.db.commit()
                self.db.refresh(chat_session)
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
                )
                if continuation:
                    reply = continuation.reply
                    active_skill = continuation.active_skill
                    router_decision = continuation.router_decision
                    step_result = continuation.step_result
                    tool_result = continuation.tool_result
                else:
                    router_decision = initial_schedule_decision
                    yield self._stream_status(chat_session, "responding", "正在生成回复")
                    chunks: list[str] = []
                    for chunk in self._generate_reply_stream_segment(
                        request.message,
                        chat_session,
                        None,
                        router_decision,
                        StepAgentResult(),
                        None,
                        model_config,
                        persona_prompt,
                        memory_context,
                        conversation_context,
                    ):
                        chunks.append(chunk)
                        yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                        self._pace_stream()
                    reply = "".join(chunks).strip() or FALLBACK_REPLY
                    if not chunks:
                        for chunk in self.response_generator.chunk_text(reply):
                            yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                            self._pace_stream()
                yield self._stream_event("stream_end", chat_session, {})
                memory_recent_messages = self._recent_messages(chat_session) if memory_model_config else []
                self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                if memory_model_config:
                    self._enqueue_memory_capture(
                        request,
                        chat_session,
                        reply,
                        step_result,
                        tool_result,
                        memory_model_config,
                        memory_recent_messages,
                    )
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    tool_result=tool_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))
                return

            before_skill = chat_session.active_skill_id
            before_step = chat_session.active_step_id
            self.runtime.apply_decision(chat_session, router_decision)
            self._record_runtime_event(request.tenant_id, chat_session, before_skill, before_step, router_decision)
            self.db.commit()
            self.db.refresh(chat_session)

            active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
            if not self._should_run_step_agent(router_decision, active_skill):
                knowledge_stream_events = []
                auto_step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    stream_events=knowledge_stream_events,
                )
                if auto_step_result.knowledge_results:
                    step_result = auto_step_result
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)
                yield self._stream_status(chat_session, "responding", "正在生成回复")
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
                    yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                    self._pace_stream()
                yield self._stream_event("stream_end", chat_session, {})
                self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
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
                yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))
                return
            yield self._stream_event(
                "skill_state",
                chat_session,
                self._skill_state_payload(
                    chat_session,
                    skills,
                    self._runtime_stream_context(router_decision, before_skill, before_step, chat_session),
                ),
            )
            yield self._stream_status(
                chat_session,
                "stepping",
                "正在思考",
                {"active_skill_id": chat_session.active_skill_id, "active_step_id": chat_session.active_step_id},
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
            self.db.commit()
            self.db.refresh(chat_session)
            for event_name, payload in repair_stream_events:
                yield self._stream_event(event_name, chat_session, payload)

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
                    yield self._stream_event(event_name, chat_session, payload)
            elif not step_result.knowledge_results and self._should_auto_query_knowledge(request.message, router_decision):
                knowledge_stream_events = []
                auto_step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    stream_events=knowledge_stream_events,
                )
                if auto_step_result.knowledge_results:
                    step_result.knowledge_query = auto_step_result.knowledge_query
                    step_result.knowledge_results = auto_step_result.knowledge_results
                self.db.commit()
                self.db.refresh(chat_session)
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

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
                )
                for event_name, payload in tool_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

            reflection_stream_events: list[tuple[str, dict[str, object]]] = []
            reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
            if reflection_max_rounds > 0 and self._should_try_reflection(router_decision, step_result, tool_result):
                yield self._stream_status(
                    chat_session,
                    "reflecting",
                    "正在反思",
                    {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
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
            )
            for event_name, payload in reflection_stream_events:
                yield self._stream_event(event_name, chat_session, payload)

            yield self._stream_status(chat_session, "responding", "正在生成回复")
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
            ):
                chunks.append(chunk)
                yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                self._pace_stream()
            reply = "".join(chunks).strip() or FALLBACK_REPLY
            if not chunks:
                for chunk in self.response_generator.chunk_text(reply):
                    chunks.append(chunk)
                    yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                    self._pace_stream()
            completed = self._finalize_execution_after_reply(
                request.tenant_id,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
            )
            if completed:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {"pending_tasks": chat_session.pending_tasks_json or []},
                )
            yield self._stream_event("stream_end", chat_session, {})

        except AgentLoopPreconditionError as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": exc.code, "message": exc.message},
            )
            yield self._stream_status(chat_session, "error", exc.message, {"code": exc.code})
            reply = FALLBACK_REPLY
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                self._pace_stream()
            yield self._stream_event("stream_end", chat_session, {})
        except LLMError as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "LLM_ERROR", "message": str(exc)},
            )
            yield self._stream_status(chat_session, "error", "模型调用失败", {"code": "LLM_ERROR"})
            reply = FALLBACK_REPLY
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                self._pace_stream()
            yield self._stream_event("stream_end", chat_session, {})
        except Exception as exc:
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "AGENT_LOOP_ERROR", "message": str(exc)},
            )
            yield self._stream_status(chat_session, "error", "Agent Loop 出错", {"code": "AGENT_LOOP_ERROR"})
            reply = FALLBACK_REPLY
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                self._pace_stream()
            yield self._stream_event("stream_end", chat_session, {})

        if not chat_session:
            chat_session = self._get_or_create_session(request)
        memory_recent_messages = self._recent_messages(chat_session) if memory_model_config else []
        self._finalize_turn(chat_session, request.tenant_id, reply, step_result, request.message)
        self.db.commit()
        self.db.refresh(chat_session)
        if memory_model_config:
            self._enqueue_memory_capture(
                request,
                chat_session,
                reply,
                step_result,
                tool_result,
                memory_model_config,
                memory_recent_messages,
            )
        result = ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )
        yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))

    def _stream_status(
        self,
        chat_session: ChatSession,
        phase: str,
        text: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._stream_event(
            "status",
            chat_session,
            {"phase": phase, "text": text, **(extra or {})},
        )

    def _stream_event(
        self,
        kind: str,
        chat_session: ChatSession,
        payload: dict[str, object],
    ) -> dict[str, object]:
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
        status("received", {"session_id": chat_session.id})
        self._append_message(
            request.tenant_id,
            chat_session.id,
            "user",
            request.message,
            metadata=self._user_message_metadata(request),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {"message": request.message, "channel": request.channel, "user_id": request.user_id},
        )

        model_config = self._get_request_model(request, chat_session.agent_id)
        skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
        tools = self._tools_with_general_skills(
            request.tenant_id,
            self._list_enabled_tools(request.tenant_id),
            chat_session.agent_id,
        )
        if not model_config:
            raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
        if not skills:
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
                self._conversation_context(chat_session),
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
                    conversation_context=self._conversation_context(chat_session),
                    general_response=general_response,
                )
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
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
                conversation_context=self._conversation_context(chat_session),
            )
        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        memory_context = [
            memory_read(row)
            for row in self.memory.recall(
                request.tenant_id,
                request.user_id,
                request.message,
                model_config=model_config,
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
        conversation_context = self._conversation_context(chat_session)
        router_context = self._conversation_context(chat_session, max_messages=ROUTER_CONTEXT_MESSAGES)

        status("routing")
        router_decision = self.router.decide(
            request.message, chat_session, skills, model_config, router_context, memory_context
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
            router_decision.model_dump(),
        )
        general_response = self._try_handle_general_skill_after_scene_router(
            request,
            chat_session,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
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
            )

        initial_schedule_decision = self._initial_scheduler_queue_decision(
            request,
            chat_session,
            router_decision,
        )
        if initial_schedule_decision:
            self.runtime.apply_decision(chat_session, initial_schedule_decision)
            self._release_active_task_to_scheduler(chat_session, initial_schedule_decision)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "task_frames_seeded_for_scheduler",
                initial_schedule_decision.model_dump(mode="json"),
            )
            self.db.commit()
            self.db.refresh(chat_session)
            continuation = self._try_continue_pending_after_completion(
                request,
                chat_session,
                model_config,
                skills,
                tools,
                self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                memory_context,
                conversation_context,
                "",
            )
            if continuation:
                return PreparedTurn(
                    chat_session=chat_session,
                    model_config=model_config,
                    active_skill=continuation.active_skill,
                    router_decision=continuation.router_decision,
                    step_result=continuation.step_result,
                    tool_result=continuation.tool_result,
                    memory_context=memory_context,
                    conversation_context=conversation_context,
                    reply_override=continuation.reply,
                )
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=None,
                router_decision=initial_schedule_decision,
                step_result=StepAgentResult(),
                tool_result=None,
                memory_context=memory_context,
                conversation_context=conversation_context,
            )

        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.apply_decision(chat_session, router_decision)
        self._record_runtime_event(request.tenant_id, chat_session, before_skill, before_step, router_decision)
        self.db.commit()
        self.db.refresh(chat_session)

        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
        if not self._should_run_step_agent(router_decision, active_skill):
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
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
            )
        status(
            "stepping",
            {"active_skill_id": chat_session.active_skill_id, "active_step_id": chat_session.active_step_id},
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
        elif not step_result.knowledge_results and self._should_auto_query_knowledge(request.message, router_decision):
            auto_step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
                status_callback=status,
            )
            if auto_step_result.knowledge_results:
                step_result.knowledge_query = auto_step_result.knowledge_query
                step_result.knowledge_results = auto_step_result.knowledge_results
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
        )

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if router_decision.decision == "handoff_human" or step_result.handoff:
            chat_session.status = "handoff"
            return False
        if self._should_complete_skill(active_skill, chat_session, step_result, tool_result):
            self._complete_active_skill(tenant_id, chat_session, active_skill, "step_completed")
            return True
        return False

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
        )

    def _initial_scheduler_queue_decision(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        router_decision: RouterDecision,
    ) -> RouterDecision | None:
        tasks: list[PendingTask] = [*router_decision.pending_tasks, *router_decision.created_tasks]
        existing_selected_count = 0
        if router_decision.decision == "switch_to_pending" and router_decision.selected_task_id:
            existing_selected_count = 1 if self._find_task_frame(chat_session, router_decision.selected_task_id) else 0
        primary_task = self._primary_task_from_router_decision(request, chat_session, router_decision)
        active_task = self._active_task_for_scheduler(request, chat_session, router_decision, primary_task)
        if active_task:
            tasks = [active_task, *tasks]
        if primary_task:
            tasks = [primary_task, *tasks]
        if len(tasks) + existing_selected_count < 2:
            return None
        return RouterDecision(
            decision="create_pending",
            confidence=router_decision.confidence,
            user_intent=router_decision.user_intent,
            reason=(
                "本轮包含多个由 Router 明确识别的任务，先写入 task frame，"
                "再交给 scheduler 决定执行顺序。"
            ),
            source_message=router_decision.source_message or request.message,
            pending_tasks=tasks,
            task_updates=router_decision.task_updates,
        )

    def _active_task_for_scheduler(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        router_decision: RouterDecision,
        primary_task: PendingTask | None,
    ) -> PendingTask | None:
        if not chat_session.active_skill_id or not primary_task:
            return None
        if primary_task.target_skill_id == chat_session.active_skill_id:
            return None
        if router_decision.decision not in {"start_skill", "start_new_task"}:
            return None
        active_task_id = None
        if isinstance(chat_session.awaiting_input_json, dict):
            raw_task_id = chat_session.awaiting_input_json.get("task_id")
            active_task_id = str(raw_task_id) if raw_task_id else None
        if active_task_id:
            for update in router_decision.task_updates:
                if update.task_id == active_task_id and (
                    update.remove or update.status in {"removed", "completed", "cancelled"}
                ):
                    return None
        return PendingTask(
            task_id=active_task_id or new_id("task"),
            status="pending",
            decision="continue_active",
            target_skill_id=chat_session.active_skill_id,
            target_step_id=chat_session.active_step_id,
            confidence=router_decision.confidence,
            user_intent="继续当前未完成任务，并从本轮用户消息中提取与该任务相关的信息。",
            reason=(
                "Router 在已有 active task 时选择了另一个新任务；"
                "保留当前 active task 给 scheduler，由 scheduler 决定本轮是否以及何时继续。"
            ),
            source_message=request.message,
            slot_hints=dict(chat_session.slots_json or {}),
        )

    def _release_active_task_to_scheduler(
        self,
        chat_session: ChatSession,
        scheduler_queue_decision: RouterDecision,
    ) -> None:
        active_skill_id = chat_session.active_skill_id
        active_step_id = chat_session.active_step_id
        if not active_skill_id:
            return
        for task in scheduler_queue_decision.pending_tasks:
            if task.target_skill_id != active_skill_id:
                continue
            if task.target_step_id and active_step_id and task.target_step_id != active_step_id:
                continue
            chat_session.active_skill_id = None
            chat_session.active_step_id = None
            chat_session.slots_json = {}
            chat_session.awaiting_input_json = None
            return

    def _primary_task_from_router_decision(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        router_decision: RouterDecision,
    ) -> PendingTask | None:
        if router_decision.decision in {
            "answer_only",
            "clarify",
            "create_pending",
            "update_pending",
            "complete_task",
            "exit_current_skill",
            "handoff",
            "handoff_human",
            "switch_to_pending",
        }:
            return None
        skill_id = router_decision.target_skill_id or chat_session.active_skill_id
        if not skill_id:
            return None
        step_id = router_decision.target_step_id or chat_session.active_step_id
        slots: dict[str, Any] = {}
        if skill_id == chat_session.active_skill_id and isinstance(chat_session.slots_json, dict):
            slots.update(chat_session.slots_json)
        slots.update(dict(router_decision.slot_hints or {}))
        return PendingTask(
            task_id=router_decision.selected_task_id or new_id("task"),
            status="pending",
            decision="start_skill",
            target_skill_id=skill_id,
            target_step_id=step_id,
            confidence=router_decision.confidence,
            user_intent=router_decision.user_intent,
            reason=router_decision.reason,
            source_message=router_decision.source_message or request.message,
            slot_hints=slots,
        )

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
    ) -> TaskScheduleContinuation | None:
        if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
            return None
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        for schedule_round in range(max_actions):
            if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
                break
            schedule = self.router.schedule_tasks_after_completion(
                request.message,
                chat_session,
                skills,
                model_config,
                conversation_context,
                memory_context,
                completed_reply="\n\n".join([completed_reply, *replies]).strip(),
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "task_scheduler_decision_created",
                {**schedule.model_dump(mode="json"), "round": schedule_round + 1},
            )
            if schedule.action != "run_tasks" or not schedule.selected_task_ids:
                break

            for task_id in schedule.selected_task_ids:
                if executed_actions >= max_actions:
                    break
                router_decision = self._router_decision_from_task_frame(
                    chat_session,
                    task_id,
                    schedule.reason,
                )
                if not router_decision:
                    continue

                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                self.runtime.apply_decision(chat_session, router_decision)
                self._record_runtime_event(
                    request.tenant_id, chat_session, before_skill, before_step, router_decision
                )
                self.db.commit()
                self.db.refresh(chat_session)

                active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
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
                )
                segment = self._generate_reply_segment(
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
                )
                if segment:
                    replies, _replaced_reply = self._merge_scheduled_reply_segment(replies, segment)
                executed_actions += 1
                completed = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if completed and active_skill:
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                if not completed:
                    if self._should_attempt_scheduled_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        schedule_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    return self._scheduled_continuation(
                        replies, active_skill, router_decision, step_result, tool_result
                    )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {"pending_tasks": chat_session.pending_tasks_json or [], "round": schedule_round + 1},
                )
            if executed_actions >= max_actions:
                break

        return self._scheduled_continuation(replies, active_skill, router_decision, step_result, tool_result)

    def _scheduled_continuation(
        self,
        replies: list[str],
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> TaskScheduleContinuation | None:
        if not replies:
            return None
        return TaskScheduleContinuation(
            reply="\n\n".join(replies).strip(),
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
        )

    def _should_attempt_scheduled_task_followup(
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
        if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
            return False

        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        self.db.commit()
        self.db.refresh(chat_session)

        if chat_session.awaiting_input_json or chat_session.active_skill_id:
            return False
        if not (chat_session.pending_tasks_json or chat_session.skill_stack_json):
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

    def _merge_scheduled_reply_segment(self, replies: list[str], segment: str) -> tuple[list[str], bool]:
        clean_segment = str(segment or "").strip()
        if not clean_segment:
            return replies, False
        if not replies:
            return [clean_segment], False

        previous = replies[-1].strip()
        merged = self._merge_overlapping_task_confirmation(previous, clean_segment)
        if merged:
            return [*replies[:-1], merged], True
        return [*replies, clean_segment], False

    def _merge_overlapping_task_confirmation(self, previous: str, current: str) -> str | None:
        if not previous or not current:
            return None
        if not self._has_task_confirmation(previous) or not self._has_task_confirmation(current):
            return None
        previous_products = self._task_reply_products(previous)
        current_products = self._task_reply_products(current)
        if not current_products:
            return None
        if previous_products and current_products and previous_products.isdisjoint(current_products):
            return None

        split_index = self._task_handoff_index(previous)
        if split_index is None:
            return None
        prefix = previous[:split_index].strip()
        if not prefix:
            return current.strip()
        return f"{prefix}\n\n{current.strip()}"

    def _has_task_confirmation(self, text: str) -> bool:
        return any(marker in text for marker in TASK_CONFIRMATION_MARKERS)

    def _task_handoff_index(self, text: str) -> int | None:
        indexes = [text.find(marker) for marker in TASK_HANDOFF_MARKERS if marker in text]
        indexes = [index for index in indexes if index >= 0]
        return min(indexes) if indexes else None

    def _task_reply_products(self, text: str) -> set[str]:
        return {match.group(1).upper() for match in TASK_PRODUCT_PATTERN.finditer(text or "")}

    def _router_decision_from_task_frame(
        self,
        chat_session: ChatSession,
        task_id: str,
        scheduler_reason: str | None = None,
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
            reason=scheduler_reason or frame.get("reason"),
            source_message=frame.get("source_message"),
            slot_hints=slot_hints,
        )

    def _find_task_frame(self, chat_session: ChatSession, task_id: str) -> dict[str, Any] | None:
        for frames in (chat_session.pending_tasks_json or [], chat_session.skill_stack_json or []):
            for frame in frames:
                if isinstance(frame, dict) and str(frame.get("task_id") or "") == str(task_id):
                    return frame
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
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        conversation_context = conversation_context or self._conversation_context(chat_session)
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
            )
            if not retried:
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
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None, bool]:
        conversation_context = conversation_context or self._conversation_context(chat_session)
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
        self._record_runtime_event(request.tenant_id, chat_session, before_skill, before_step, router_decision)
        self.db.commit()
        self.db.refresh(chat_session)

        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id, chat_session.agent_id)
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
    ) -> tuple[StepAgentResult, ToolResult | None]:
        tool_result: ToolResult | None = None
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
                    payload = self._tool_loop_decision_payload(iteration + 1, "respond_after_duplicate")
                    self.events.record(request.tenant_id, chat_session.id, "agent_loop_completed", payload)
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
            )
            self._apply_step_result(request.tenant_id, chat_session, continuation_result, active_skill)
            self.db.commit()
            self.db.refresh(chat_session)
            step_result = continuation_result
            if step_result.tool_call:
                payload = self._tool_loop_decision_payload(
                    iteration + 1,
                    "model_tool_call",
                    step_result.tool_call,
                )
                self.events.record(request.tenant_id, chat_session.id, "agent_loop_continued", payload)
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
        if self._agent_requires_resource_filter(request.tenant_id, chat_session.agent_id) and not knowledge_base_ids:
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
            expanded_query = self._expanded_knowledge_query(original_message)
            if expanded_query and expanded_query not in search_query:
                search_query = f"{search_query}\n{expanded_query}"
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
            "selected_buckets": [item.model_dump(mode="json") for item in search_response.selected_buckets],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }
        self._record_knowledge_results(chat_session, knowledge_items)
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
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> StepAgentResult:
        if not self._should_auto_query_knowledge(request.message, router_decision):
            return StepAgentResult()

        query = KnowledgeQuery(
            query=request.message,
            reason="用户要求基于业务资料或规则回答",
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
        if not knowledge_items:
            return StepAgentResult()
        self._record_knowledge_results(chat_session, knowledge_items)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "knowledge_query_finished",
            {**knowledge_items, "auto": True},
        )
        if stream_events is not None:
            for trace in knowledge_items.get("trace") or []:
                stream_events.append(("status", {"phase": "knowledge", **trace}))
            stream_events.append(("knowledge_result", knowledge_items))
        return StepAgentResult(knowledge_query=query, knowledge_results=[knowledge_items])

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
                query=self._expanded_knowledge_query(message),
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
            "selected_buckets": [item.model_dump(mode="json") for item in search_response.selected_buckets],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }

    def _expanded_knowledge_query(self, message: str) -> str:
        text = (message or "").strip()
        if not text:
            return text
        lowered = text.lower()
        expansions: list[str] = [text]
        if any(term in lowered for term in ("不想要", "不要了", "取消", "退", "退款", "撤销")):
            expansions.append("订单取消 刚创建订单取消 取消刚创建的订单 售后退款 订单处理")
        if any(term in lowered for term in ("刚买", "刚下单", "下单", "订单", "支付")):
            expansions.append("订单创建 支付确认 取消处理 客服处理边界")
        if any(term in lowered for term in ("会员", "账号", "地址", "称呼", "隐私")):
            expansions.append("用户身份 称呼 隐私保护 会员账号 历史地址 处理原则")
        if any(term in lowered for term in ("客服", "客户", "用户", "服务人员")):
            expansions.append("服务人员 应先确认真实诉求 当前已知事实 可执行动作 需要补充的信息")
        unique: list[str] = []
        for item in expansions:
            if item not in unique:
                unique.append(item)
        return "\n".join(unique)

    def _should_auto_query_knowledge(self, message: str, router_decision: RouterDecision | None = None) -> bool:
        text = (message or "").strip().lower()
        if not text:
            return False
        explicit_terms = (
            "业务资料",
            "知识库",
            "知识",
            "引用",
            "基于",
            "根据",
            "规则",
            "政策",
            "资料",
            "文档",
            "怎么处理",
            "如何处理",
            "应该怎么",
            "怎么办",
            "怎么回",
            "怎么回复",
            "该怎么",
            "如何回复",
        )
        if any(term in text for term in explicit_terms):
            return True
        question_terms = (
            "怎么",
            "如何",
            "应该",
            "应当",
            "能否",
            "能不能",
            "可以吗",
            "行不行",
            "要不要",
            "问什么",
            "回复",
        )
        knowledge_terms = (
            "规范",
            "标准",
            "要求",
            "制度",
            "手册",
            "口径",
            "流程说明",
        )
        if any(term in text for term in question_terms) and any(term in text for term in knowledge_terms):
            return True
        intent = (router_decision.user_intent or "").lower() if router_decision else ""
        return any(term in intent for term in ("知识", "资料", "规则", "政策"))

    def _record_knowledge_results(self, chat_session: ChatSession, item: dict[str, Any]) -> None:
        history = list(chat_session.knowledge_context_json or [])
        history.append(item)
        chat_session.knowledge_context_json = history[-12:]

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
        conversation_context = conversation_context or self._conversation_context(chat_session)
        step_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context=memory_context,
            conversation_context=conversation_context,
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
        )
        if not self._step_result_has_progress(validation_result) and not self._step_result_has_reply_repair(
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
            "start_skill",
            "continue_active",
            "continue_current_skill",
            "jump_within_current_skill",
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

    def _missing_expected_fields(
        self, skill: Skill | None, chat_session: ChatSession
    ) -> list[str]:
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
            "start_skill",
            "start_new_task",
            "continue_active",
            "continue_current_skill",
            "jump_within_current_skill",
            "suspend_current_and_start_new_skill",
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
    ) -> StepAgentResult:
        conversation_context = conversation_context or self._conversation_context(chat_session)
        recent_messages = [
            message
            for message in conversation_context.get("messages", [])
            if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        ]
        step_result = self.step_agent.run(
            request.message,
            chat_session,
            active_skill,
            self._step_agent_tools(
                active_skill,
                tools,
                request.message,
                model_config,
                chat_session.agent_id,
            ),
            model_config,
            router_decision,
            repair_context,
            recent_messages,
            memory_context,
            conversation_context,
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
    ) -> list[Tool]:
        if active_skill is None:
            return []
        active_skill_id = active_skill.skill_id
        scoped_tools: list[Tool] = []
        general_skill_tools: list[Tool] = []
        for tool in tools:
            if not getattr(tool, "enabled", False):
                continue
            tool_name = str(getattr(tool, "name", "") or "")
            if tool_name.startswith(GENERAL_SKILL_TOOL_PREFIX):
                general_skill_tools.append(tool)
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
            selection = self.general_skill_selector.decide(message, candidates, model_config)
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
            chat_session.slots_json = {**(chat_session.slots_json or {}), **step_result.slot_updates}
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        if step_result.next_step_id and chat_session.active_skill_id:
            if active_skill and active_skill.skill_id == chat_session.active_skill_id:
                if not self._skill_has_step(active_skill, step_result.next_step_id):
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
            previous_step = chat_session.active_step_id
            chat_session.active_step_id = step_result.next_step_id
            if previous_step != step_result.next_step_id:
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_step_changed",
                    {
                        "from_skill_id": chat_session.active_skill_id,
                        "to_skill_id": chat_session.active_skill_id,
                        "from_step_id": previous_step,
                        "to_step_id": step_result.next_step_id,
                    },
                )

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

    def _execute_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_call_id: str | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> ToolResult:
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
            )
        else:
            tool_result = self.tool_executor.execute(
                request.tenant_id, tool_call, chat_session.active_skill_id
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
        structured = response.structured_result if isinstance(response.structured_result, dict) else {}
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
        self.events.record(request.tenant_id, chat_session.id, "general_skill_run_finished", finished_payload)
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
    ) -> ToolResult | None:
        if not query:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "通用技能调用缺少自然语言任务。",
                },
                error=ToolError(code="GENERAL_SKILL_MISMATCH", message="通用技能调用缺少自然语言任务。"),
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
                error=ToolError(code="GENERAL_SKILL_NOT_FOUND", message="当前员工没有可用通用技能。"),
            )
        try:
            selection = self.general_skill_selector.decide(query, candidates, model_config)
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
        self.events.record(request.tenant_id, chat_session.id, "general_skill_guard_rejected", payload)
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=payload,
            error=ToolError(
                code="GENERAL_SKILL_MISMATCH",
                message="通用技能与当前子任务不匹配，已取消调用。",
            ),
        )

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
            or (
                step_result.next_step_id
                and step_result.next_step_id != chat_session.active_step_id
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
            "continue_current_skill"
            if chat_session.active_skill_id == target_skill.skill_id
            else "start_skill"
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
        used_signatures = {self._tool_history_signature(item) for item in self._tool_call_history(slots)}
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
        nodes_by_id = {str(node.get("node_id") or ""): node for node in nodes if node.get("node_id")}
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

    def _next_steps_from_graph(self, skill: Skill, active_step_id: str | None) -> list[dict[str, Any]]:
        if not active_step_id:
            return []
        nodes_by_id = {str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)}
        outgoing = self._graph_outgoing_edges(skill).get(active_step_id, [])
        return [
            nodes_by_id[target_id]
            for target_id in (str(edge.get("next_node_id") or "") for edge in outgoing)
            if target_id in nodes_by_id
        ]

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        nodes_by_id = {str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)}
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
        skills_by_id = {skill.skill_id: skill for skill in skills}
        self._drop_stale_completed_stack_frames(tenant_id, chat_session, skills_by_id)
        active_skill = next(
            (skill for skill in skills if skill.skill_id == chat_session.active_skill_id), None
        )
        if active_skill and self._is_terminal_skill_state(active_skill, chat_session):
            self._complete_active_skill(tenant_id, chat_session, active_skill, "stale_terminal_state")

    def _drop_stale_completed_stack_frames(
        self, tenant_id: str, chat_session: ChatSession, skills_by_id: dict[str, Skill]
    ) -> None:
        kept_frames: list[dict[str, Any]] = []
        changed = False
        for frame in chat_session.skill_stack_json or []:
            skill_id = str(frame.get("skill_id") or "")
            skill = skills_by_id.get(skill_id)
            if skill and self._is_terminal_skill_frame(skill, frame):
                changed = True
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_completed",
                    {
                        "skill_id": skill_id,
                        "step_id": frame.get("step_id"),
                        "reason": "stale_suspended_terminal_state",
                        "resumed_skill_id": chat_session.active_skill_id,
                        "resumed_step_id": chat_session.active_step_id,
                    },
                )
                continue
            kept_frames.append(frame)
        if changed:
            chat_session.skill_stack_json = kept_frames
            chat_session.updated_at = utc_now()

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
        if tool_result and tool_result.success and self._current_step_can_finish_after_tool(
            skill, chat_session
        ):
            return True
        if self._is_answer_ready_skill_state(skill, chat_session):
            return True
        if not step_result.next_step_id and not step_result.tool_call:
            return True
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
            "reply",
            "handoff_human",
            "continue_flow",
            "ask_user",
            "ask_clarification",
        }
        return all(action in terminal_actions or action.startswith("call_tool:") for action in actions)

    def _current_skill_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        for step in self._skill_steps(skill):
            if isinstance(step, dict) and step.get("step_id") == active_step_id:
                return step
        return None

    def _current_step_can_finish_after_tool(
        self, skill: Skill, chat_session: ChatSession
    ) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field)
            for field in expected
        )

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
        actions = [_normalize_action(action) for action in actions]
        return any(action in {"answer_user", "reply"} for action in actions)

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
            return row
        return self._get_default_model(request.tenant_id, agent_id, role)

    def _get_default_model(self, tenant_id: str, agent_id: str | None = None, role: str = "default") -> ModelConfig | None:
        return model_for_agent(self.db, tenant_id, agent_id, role)

    def _get_persona_prompt(self, tenant_id: str, agent_id: str | None = None) -> str | None:
        agent = self._get_agent_profile(tenant_id, agent_id)
        if agent and not agent.is_overall:
            return agent.persona_prompt
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

    def _list_published_general_skills(self, tenant_id: str, agent_id: str | None = None) -> list[GeneralSkill]:
        statement = select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id,
            GeneralSkill.status == "published",
        )
        if self._agent_requires_resource_filter(tenant_id, agent_id):
            resource_ids = self._agent_resource_ids(tenant_id, agent_id, "general_skill")
            if not resource_ids:
                return []
            statement = statement.where(GeneralSkill.id.in_(resource_ids))  # type: ignore[attr-defined]
        return list(self.db.exec(statement).all())

    def _select_general_skill(
        self,
        message: str,
        model_config: ModelConfig,
        agent_id: str | None = None,
    ) -> tuple[GeneralSkill, GeneralSkillSelection] | None:
        general_skills = self._list_published_general_skills(model_config.tenant_id, agent_id)
        if not general_skills:
            return None
        try:
            selection = self.general_skill_selector.decide(message, general_skills, model_config)
        except LLMError:
            return None
        if not selection.use_general_skill or not selection.selected_slug:
            return None
        skill = next((item for item in general_skills if item.slug == selection.selected_slug), None)
        if not skill:
            return None
        return skill, selection

    def _list_enabled_tools(self, tenant_id: str) -> list[Tool]:
        return list(
            self.db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.enabled == True)  # noqa: E712
            ).all()
        )

    def _tools_with_general_skills(self, tenant_id: str, tools: list[Tool], agent_id: str | None = None) -> list[Any]:
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

    def _agent_resource_ids(self, tenant_id: str, agent_id: str | None, resource_type: str) -> list[str]:
        if not agent_id:
            return []
        return [
            row.resource_id
            for row in self.db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == tenant_id,
                    AgentResourceBinding.agent_id == agent_id,
                    AgentResourceBinding.resource_type == resource_type,
                    AgentResourceBinding.status == "active",
                )
            ).all()
        ]

    def _agent_visible_knowledge_base_ids(self, tenant_id: str, agent_id: str | None) -> list[str]:
        return visible_knowledge_base_ids(self.db, tenant_id, agent_id)

    def _get_active_skill(self, tenant_id: str, skill_id: str | None, agent_id: str | None = None) -> Skill | None:
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def _recent_messages(self, chat_session: ChatSession, limit: int = 8) -> list[dict[str, str]]:
        if not hasattr(self, "db"):
            return []
        rows = list(
            self.db.exec(
                select(Message)
                .where(Message.tenant_id == chat_session.tenant_id, Message.session_id == chat_session.id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).all()
        )
        rows.reverse()
        return [{"role": row.role, "content": row.content} for row in rows]

    def _conversation_context(
        self, chat_session: ChatSession, max_messages: int | None = None
    ) -> dict[str, object]:
        if not hasattr(self, "db") or not hasattr(self.db, "exec"):
            return build_conversation_context([])
        rows = list(
            self.db.exec(
                select(Message)
                .where(Message.tenant_id == chat_session.tenant_id, Message.session_id == chat_session.id)
                .order_by(Message.created_at.asc())
            ).all()
        )
        if max_messages is not None and max_messages > 0:
            rows = rows[-max_messages:]
        return build_conversation_context([{"role": row.role, "content": row.content} for row in rows])

    def _assistant_message_metadata(
        self,
        step_result: StepAgentResult | None,
        chat_session: ChatSession,
        source_message: str | None = None,
    ) -> dict[str, Any]:
        knowledge_results = list(step_result.knowledge_results or []) if step_result else []
        if not knowledge_results and source_message:
            for item in reversed(chat_session.knowledge_context_json or []):
                if item.get("source_message") == source_message:
                    knowledge_results = [item]
                    break
        citations = self._dedupe_knowledge_citations(knowledge_citations_from_results(knowledge_results))
        if not citations:
            return {}
        first_query = next(
            (item.get("query") for item in knowledge_results if isinstance(item.get("query"), dict)),
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
            identity = re.sub(r"/\s*evidence\s*\d+", "", identity, flags=re.IGNORECASE)
            identity = re.split(r"在第\s*\d+\s*章第\s*\d+\s*节", identity)[0]
            identity = identity.split("。")[0]
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
    ) -> None:
        self.db.add(
            Message(
                tenant_id=tenant_id,
                session_id=session_id,
                role=role,
                content=content,
                metadata_json=metadata or {},
            )
        )

    def _user_message_metadata(self, request: ChatTurnRequest) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if request.interaction_mode == "scheduled_task":
            metadata["interaction_mode"] = "scheduled_task"
        if request.model_config_id:
            metadata["model_config_id"] = request.model_config_id
        return metadata

    def _record_runtime_event(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        before_skill: str | None,
        before_step: str | None,
        decision: RouterDecision,
    ) -> None:
        payload = {
            "decision": decision.decision,
            "from_skill_id": before_skill,
            "to_skill_id": chat_session.active_skill_id,
            "from_skill_version": self._skill_version(tenant_id, before_skill),
            "to_skill_version": self._skill_version(tenant_id, chat_session.active_skill_id),
            "from_step_id": before_step,
            "to_step_id": chat_session.active_step_id,
            "skill_stack": chat_session.skill_stack_json,
        }
        event_type = "skill_step_changed"
        if decision.decision in {"start_skill", "start_new_task"}:
            event_type = "skill_started"
        elif decision.decision in {"switch_to_pending"}:
            event_type = "skill_resumed"
        elif decision.decision == "suspend_current_and_start_new_skill" or (
            decision.decision
            in {"answer_related_question_then_resume", "answer_chitchat_then_resume"}
            and before_skill
            and chat_session.active_skill_id
            and before_skill != chat_session.active_skill_id
        ):
            event_type = "skill_suspended"
        elif decision.decision == "exit_current_skill":
            event_type = "skill_resumed" if chat_session.active_skill_id else "skill_exited"
        elif decision.decision == "handoff_human":
            event_type = "handoff_triggered"
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
    ) -> dict[str, object]:
        skill_names = {skill.skill_id: skill.name for skill in skills}
        current_skills: list[dict[str, object]] = []
        for frame in chat_session.skill_stack_json or []:
            skill_id = frame.get("skill_id")
            if not skill_id:
                continue
            current_skills.append(
                {
                    "skillId": skill_id,
                    "name": skill_names.get(str(skill_id), str(skill_id)),
                    "stepId": frame.get("step_id"),
                    "state": "suspended",
                }
            )
        if chat_session.active_skill_id:
            current_skills.append(
                {
                    "skillId": chat_session.active_skill_id,
                    "name": skill_names.get(chat_session.active_skill_id, chat_session.active_skill_id),
                    "stepId": chat_session.active_step_id,
                    "state": "active",
                }
            )
        for task in chat_session.pending_tasks_json or []:
            if not isinstance(task, dict):
                continue
            skill_id = task.get("target_skill_id") or task.get("skill_id")
            if not skill_id:
                continue
            current_skills.append(
                {
                    "skillId": skill_id,
                    "name": skill_names.get(str(skill_id), str(skill_id)),
                    "stepId": task.get("target_step_id") or task.get("step_id"),
                    "state": task.get("status") or "pending",
                }
            )
        return {
            "activeSkillId": chat_session.active_skill_id,
            "activeStepId": chat_session.active_step_id,
            "currentSkills": current_skills,
            **(runtime_context or {}),
        }

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
    ) -> None:
        for item in run_response.execution_trace:
            self.events.record(
                tenant_id,
                chat_session.id,
                "general_skill_trace",
                {
                    "skill_slug": run_response.skill_slug,
                    **item,
                },
            )
        self.events.record(
            tenant_id,
            chat_session.id,
            "general_skill_run_finished",
            {
                "skill_slug": run_response.skill_slug,
                "success": bool(run_response.structured_result.get("success", True)),
                "stdout_preview": run_response.stdout[:600],
                "stderr_preview": run_response.stderr[:600],
                "structured_result": run_response.structured_result,
            },
        )

    def _enqueue_memory_capture(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        reply: str,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        recent_messages: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        try:
            job = enqueue_memory_capture(
                request,
                chat_session.id,
                reply,
                step_result,
                tool_result,
                model_config.id,
                recent_messages,
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
        self.events.record(
            chat_session.tenant_id,
            chat_session.id,
            "error_occurred",
            {"code": code, "message": message},
        )
        self._finalize_turn(chat_session, chat_session.tenant_id, FALLBACK_REPLY)
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=FALLBACK_REPLY,
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
    ) -> None:
        chat_session.updated_at = utc_now()
        metadata = self._assistant_message_metadata(step_result, chat_session, source_message)
        reply = self._normalize_overlapping_task_confirmations(reply)
        reply = self._normalize_reply_citation_labels(reply, metadata.get("knowledge_citations"))
        reply = self._strip_trailing_citation_summary(reply)
        metadata = self._metadata_with_reply_citations(metadata, reply)
        chat_session.summary = f"最近回复：{reply[:120]}"
        self._append_message(tenant_id, chat_session.id, "assistant", reply, metadata=metadata)
        event_payload: dict[str, Any] = {"reply": reply}
        if metadata.get("knowledge_citations"):
            event_payload["knowledge_citations"] = metadata["knowledge_citations"]
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

    def _normalize_overlapping_task_confirmations(self, reply: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", reply or "") if part.strip()]
        if len(paragraphs) < 2:
            return reply
        merged: list[str] = []
        replaced = False
        for paragraph in paragraphs:
            if not merged:
                merged.append(paragraph)
                continue
            candidate = self._merge_overlapping_task_confirmation("\n\n".join(merged), paragraph)
            if candidate:
                merged = [candidate]
                replaced = True
            else:
                merged.append(paragraph)
        if not replaced:
            return reply
        return "\n\n".join(merged).strip()

    def _metadata_with_reply_citations(self, metadata: dict[str, Any], reply: str) -> dict[str, Any]:
        citations = metadata.get("knowledge_citations")
        if not isinstance(citations, list) or not citations:
            return metadata
        used_labels = self._reply_citation_labels(reply, len(citations))
        if not used_labels:
            next_metadata = dict(metadata)
            next_metadata.pop("knowledge_citations", None)
            next_metadata.pop("knowledge_query", None)
            return next_metadata
        next_citations: list[dict[str, Any]] = []
        for label in sorted(used_labels):
            index = label - 1
            if index < 0 or index >= len(citations):
                continue
            citation = citations[index]
            if isinstance(citation, dict):
                next_citations.append({**citation, "label": f"[{label}]"})
        next_metadata = dict(metadata)
        next_metadata["knowledge_citations"] = next_citations
        return next_metadata

    def _reply_citation_labels(self, reply: str, max_label: int) -> set[int]:
        labels: set[int] = set()
        for match in re.finditer(r"\[(\d+)\]", reply):
            try:
                label = int(match.group(1))
            except ValueError:
                continue
            if 1 <= label <= max_label:
                labels.add(label)
        return labels
