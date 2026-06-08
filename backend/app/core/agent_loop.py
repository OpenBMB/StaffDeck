from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import sleep
from types import SimpleNamespace
from typing import Any

from sqlmodel import Session, select

from app.core.conversation_context import build_conversation_context
from app.core.reflection_agent import ReflectionAgent, ReflectionDecision, action_needs_reflection
from app.core.response_generator import FALLBACK_REPLY, ResponseGenerator
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.db.models import ChatSession, GeneralSkill, Message, ModelConfig, PersonaConfig, Skill, Tool, UIConfig, new_id, utc_now
from app.general_skills import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import GeneralSkillRunResponse, GeneralSkillSelection
from app.llm import LLMError
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService, memory_read
from app.observability import EventLog
from app.session.helpers import public_session
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, RouterDecision, StepAgentResult
from app.tools import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


StatusCallback = Callable[[str, dict[str, object]], None]
STREAM_CHUNK_INTERVAL_SECONDS = 0.045
DEFAULT_REFLECTION_MAX_ROUNDS = 1
REFLECTION_MAX_ROUNDS_LIMIT = 5
MAX_TOOL_ACTIONS_PER_TURN = 6
ROUTER_CONTEXT_MESSAGES = 8
TOOL_CALL_HISTORY_SLOT = "_tool_call_history"
TOOL_RESULTS_SLOT = "_tool_results"


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

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        memory_model_config: ModelConfig | None = None
        try:
            general_response = self._try_handle_general_skill_turn(request)
            if general_response:
                return general_response
            prepared = self._prepare_turn(request)
            chat_session = prepared.chat_session
            memory_model_config = prepared.model_config
            router_decision = prepared.router_decision
            step_result = prepared.step_result
            tool_result = prepared.tool_result
            memory_context = prepared.memory_context
            conversation_context = prepared.conversation_context
            reply = self._generate_reply_segment(
                request.message,
                chat_session,
                prepared.active_skill,
                router_decision,
                step_result,
                tool_result,
                prepared.model_config,
                self._get_persona_prompt(request.tenant_id),
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
        self._finalize_turn(chat_session, request.tenant_id, reply)
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

    def _try_handle_general_skill_turn(self, request: ChatTurnRequest) -> ChatTurnResponse | None:
        model_config = self._get_default_model(request.tenant_id)
        if not model_config:
            return None
        selected = self._select_general_skill(request.message, model_config)
        if not selected:
            return None
        skill, selection = selected
        chat_session = self._get_or_create_session(request)
        self._append_message(request.tenant_id, chat_session.id, "user", request.message)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {"message": request.message, "channel": request.channel, "user_id": request.user_id},
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
            },
        )
        run_response = self.general_skill_runner.run(skill, request.message, model_config, request.user_id)
        self._record_general_skill_run_events(request.tenant_id, chat_session, run_response)
        self._finalize_turn(chat_session, request.tenant_id, run_response.reply)
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            run_response.reply,
            StepAgentResult(),
            None,
            model_config,
            self._recent_messages(chat_session),
        )
        return ChatTurnResponse(
            reply=run_response.reply,
            session_id=chat_session.id,
            step_result=StepAgentResult(),
            session_state=public_session(chat_session),
        )

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
            self._append_message(request.tenant_id, chat_session.id, "user", request.message)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "user_message_received",
                {"message": request.message, "channel": request.channel, "user_id": request.user_id},
            )

            model_config = self._get_default_model(request.tenant_id)
            if not model_config:
                raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
            memory_model_config = model_config
            selected_general_skill = self._select_general_skill(request.message, model_config)
            if selected_general_skill:
                skill, selection = selected_general_skill
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "general_skill_selected",
                    {
                        "skill_slug": skill.slug,
                        "skill_name": skill.name,
                        "confidence": selection.confidence,
                        "reason": selection.reason,
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
                reply = run_response.reply
                for chunk in self.response_generator.chunk_text(reply):
                    yield self._stream_event("stream_delta", chat_session, {"content": chunk})
                    self._pace_stream()
                yield self._stream_event("stream_end", chat_session, {})
                self._finalize_turn(chat_session, request.tenant_id, reply)
                self.db.commit()
                self.db.refresh(chat_session)
                self._enqueue_memory_capture(
                    request,
                    chat_session,
                    reply,
                    StepAgentResult(),
                    None,
                    model_config,
                    self._recent_messages(chat_session),
                )
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    step_result=StepAgentResult(),
                    session_state=public_session(chat_session),
                )
                yield self._stream_event("complete", chat_session, result.model_dump(mode="json"))
                return
            skills = self._list_published_skills(request.tenant_id)
            tools = self._list_enabled_tools(request.tenant_id)
            persona_prompt = self._get_persona_prompt(request.tenant_id)
            if not skills:
                raise AgentLoopPreconditionError("missing_published_skill", "没有已发布技能。")
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
                request.message, chat_session, skills, model_config, router_context
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

            active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id)
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
        self._finalize_turn(chat_session, request.tenant_id, reply)
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
        self._append_message(request.tenant_id, chat_session.id, "user", request.message)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {"message": request.message, "channel": request.channel, "user_id": request.user_id},
        )

        model_config = self._get_default_model(request.tenant_id)
        skills = self._list_published_skills(request.tenant_id)
        tools = self._list_enabled_tools(request.tenant_id)
        if not model_config:
            raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
        if not skills:
            raise AgentLoopPreconditionError("missing_published_skill", "没有已发布技能。")
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
            request.message, chat_session, skills, model_config, router_context
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

        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id)
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
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        conversation_context = conversation_context or self._conversation_context(chat_session)
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
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None, bool]:
        conversation_context = conversation_context or self._conversation_context(chat_session)
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

        retry_tool_call = self._tool_call_from_reflection(reflection, chat_session, tools)
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
            reflection, chat_session, skills, router_decision
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

        active_skill = self._get_active_skill(request.tenant_id, chat_session.active_skill_id)
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
            tool_result = self._execute_tool_call(request, chat_session, tool_call, tool_call_id)
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

        advanced = self._advance_past_satisfied_collection_steps(
            request.tenant_id, chat_session, active_skill
        )
        if advanced and not step_result.tool_call and not step_result.handoff:
            if stream_events is not None:
                stream_events.append(
                    (
                        "status",
                        {
                            "phase": "stepping",
                            "text": "正在思考",
                            "active_skill_id": chat_session.active_skill_id,
                            "active_step_id": chat_session.active_step_id,
                            "repair_reason": "satisfied_step_advanced",
                        },
                    )
                )
            step_result = self._run_step_agent_once(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                router_decision,
                repair_reason="satisfied_step_advanced",
                memory_context=memory_context,
                conversation_context=conversation_context,
            )
            self._apply_step_result(request.tenant_id, chat_session, step_result, active_skill)
            self._advance_past_satisfied_collection_steps(
                request.tenant_id, chat_session, active_skill
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
        if not self._step_result_has_progress(validation_result):
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
        return router_decision.decision in {"continue_current_skill", "jump_within_current_skill"}

    def _step_result_has_progress(self, step_result: StepAgentResult) -> bool:
        return bool(step_result.slot_updates or step_result.tool_call or step_result.handoff)

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
            tools,
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
        return any(step.get("step_id") == step_id for step in self._skill_steps(skill))

    def _advance_past_satisfied_collection_steps(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill | None
    ) -> bool:
        if not skill or not chat_session.active_step_id:
            return False
        steps = self._skill_steps(skill)
        step_index = next(
            (
                index
                for index, step in enumerate(steps)
                if step.get("step_id") == chat_session.active_step_id
            ),
            -1,
        )
        if step_index < 0:
            return False

        changed = False
        slots = chat_session.slots_json or {}
        while step_index < len(steps) - 1:
            current = steps[step_index]
            expected = [str(field) for field in current.get("expected_user_info", [])]
            if any(not self._skill_slot_satisfied(slots, field) for field in expected):
                break
            actions = self._step_actions(current)
            if self._step_can_act_without_more_user_input(actions):
                break
            next_step = steps[step_index + 1]
            next_step_id = str(next_step.get("step_id") or "")
            if not next_step_id:
                break
            previous_step = chat_session.active_step_id
            chat_session.active_step_id = next_step_id
            changed = True
            self.events.record(
                tenant_id,
                chat_session.id,
                "skill_step_changed",
                {
                    "from_skill_id": chat_session.active_skill_id,
                    "to_skill_id": chat_session.active_skill_id,
                    "from_step_id": previous_step,
                    "to_step_id": next_step_id,
                    "reason": "expected_info_satisfied",
                },
            )
            step_index += 1
        return changed

    def _step_can_act_without_more_user_input(self, actions: list[str]) -> bool:
        actions = [_normalize_action(action) for action in actions]
        return (
            self._actions_allow_final_reply(actions)
            or "handoff_human" in actions
            or any(action.startswith("call_tool:") for action in actions)
        )

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
        steps = [step for step in (skill.content_json or {}).get("steps", []) if isinstance(step, dict)]
        start_index = next(
            (index for index, step in enumerate(steps) if step.get("step_id") == active_step_id),
            -1,
        )
        if start_index < 0:
            return None
        for step in steps[start_index + 1 :]:
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
    ) -> RouterDecision | None:
        if not reflection.target_skill_id:
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
        self, reflection: ReflectionDecision, chat_session: ChatSession, tools: list[Tool]
    ) -> ToolCall | None:
        if not reflection.target_tool_name:
            return None
        tool = next(
            (item for item in tools if item.enabled and item.name == reflection.target_tool_name),
            None,
        )
        if not tool:
            return None
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
        steps = self._skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return [step for step in (skill.content_json or {}).get("steps", []) if isinstance(step, dict)]

    def _get_or_create_session(self, request: ChatTurnRequest) -> ChatSession:
        session_id = request.session_id or new_id("session")
        chat_session = self.db.get(ChatSession, session_id)
        if not chat_session:
            chat_session = ChatSession(id=session_id, tenant_id=request.tenant_id, user_id=request.user_id)
            self.db.add(chat_session)
            self.db.flush()
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
        steps = [step for step in content.get("steps", []) if isinstance(step, dict)]
        if not steps:
            return False
        step_index = next(
            (
                index
                for index, step in enumerate(steps)
                if step.get("step_id") == active_step_id
            ),
            -1,
        )
        if step_index < 0 or step_index != len(steps) - 1:
            return False

        expected = [str(field) for field in steps[step_index].get("expected_user_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in expected):
            return False

        required = [str(field) for field in content.get("required_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in required):
            return False

        actions = self._step_actions(steps[step_index])
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
        for step in (skill.content_json or {}).get("steps", []):
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

    def _get_default_model(self, tenant_id: str) -> ModelConfig | None:
        return self.db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.is_default == True,  # noqa: E712
                ModelConfig.enabled == True,  # noqa: E712
            )
        ).first()

    def _get_persona_prompt(self, tenant_id: str) -> str | None:
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

    def _list_published_skills(self, tenant_id: str) -> list[Skill]:
        return list(
            self.db.exec(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
            ).all()
        )

    def _list_published_general_skills(self, tenant_id: str) -> list[GeneralSkill]:
        return list(
            self.db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id,
                    GeneralSkill.status == "published",
                )
            ).all()
        )

    def _select_general_skill(
        self,
        message: str,
        model_config: ModelConfig,
    ) -> tuple[GeneralSkill, GeneralSkillSelection] | None:
        general_skills = self._list_published_general_skills(model_config.tenant_id)
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

    def _get_active_skill(self, tenant_id: str, skill_id: str | None) -> Skill | None:
        if not skill_id:
            return None
        return self.db.exec(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
        ).first()

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

    def _append_message(self, tenant_id: str, session_id: str, role: str, content: str) -> None:
        self.db.add(Message(tenant_id=tenant_id, session_id=session_id, role=role, content=content))

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

    def _finalize_turn(self, chat_session: ChatSession, tenant_id: str, reply: str) -> None:
        chat_session.updated_at = utc_now()
        chat_session.summary = f"最近回复：{reply[:120]}"
        self._append_message(tenant_id, chat_session.id, "assistant", reply)
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            {"reply": reply},
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )
