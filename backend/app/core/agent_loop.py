from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import sleep
from typing import Any

from sqlmodel import Session, select

from app.core.reflection_agent import ReflectionAgent, ReflectionDecision
from app.core.response_generator import FALLBACK_REPLY, ResponseGenerator
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.db.models import ChatSession, Message, ModelConfig, PersonaConfig, Skill, Tool, UIConfig, new_id, utc_now
from app.llm import LLMError
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


class AgentLoop:
    def __init__(self, db: Session):
        self.db = db
        self.events = EventLog(db)
        self.router = Router()
        self.runtime = SkillRuntime()
        self.step_agent = StepAgent()
        self.reflection_agent = ReflectionAgent()
        self.response_generator = ResponseGenerator()
        self.tool_executor = ToolExecutor(db)
        self.memory = MemoryService(db)

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        try:
            prepared = self._prepare_turn(request)
            chat_session = prepared.chat_session
            router_decision = prepared.router_decision
            step_result = prepared.step_result
            tool_result = prepared.tool_result
            memory_context = prepared.memory_context
            reply = self.response_generator.generate(
                request.message,
                chat_session,
                prepared.active_skill,
                router_decision,
                step_result,
                tool_result,
                prepared.model_config,
                self._get_persona_prompt(request.tenant_id),
                memory_context,
            )
            self.runtime.finish_interrupt_response(chat_session)
            if router_decision.decision == "handoff_human" or step_result.handoff:
                chat_session.status = "handoff"
            elif self._should_complete_skill(prepared.active_skill, chat_session, step_result, tool_result):
                self._complete_active_skill(
                    request.tenant_id, chat_session, prepared.active_skill, "step_completed"
                )
            self._capture_memory(request, chat_session, reply, step_result, tool_result)

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
        self._finalize_turn(chat_session, request.tenant_id, reply)
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        reply = ""

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
            skills = self._list_published_skills(request.tenant_id)
            tools = self._list_enabled_tools(request.tenant_id)
            persona_prompt = self._get_persona_prompt(request.tenant_id)
            if not model_config:
                raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
            if not skills:
                raise AgentLoopPreconditionError("missing_published_skill", "没有已发布技能。")
            self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
            memory_context = [memory_read(row) for row in self.memory.recall(request.tenant_id, request.user_id, request.message)]
            if memory_context:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "memory_recalled",
                    {"memories": memory_context},
                )
            self.db.commit()
            self.db.refresh(chat_session)

            yield self._stream_status(chat_session, "routing", "正在判断用户意图")
            router_decision = self.router.decide(request.message, chat_session, skills, model_config)
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
                repair_stream_events,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            for event_name, payload in repair_stream_events:
                yield self._stream_event(event_name, chat_session, payload)

            if step_result.tool_call:
                yield self._stream_status(
                    chat_session,
                    "tool",
                    f"正在调用工具 {step_result.tool_call.name}",
                    {"tool_name": step_result.tool_call.name},
                )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "tool_call_started",
                    step_result.tool_call.model_dump(),
                )
                tool_result = self.tool_executor.execute(
                    request.tenant_id, step_result.tool_call, chat_session.active_skill_id
                )
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "tool_call_finished",
                    tool_result.model_dump(),
                )
                self._advance_after_successful_tool(
                    request.tenant_id, chat_session, active_skill, step_result, tool_result
                )
                yield self._stream_event(
                    "tool_result",
                    chat_session,
                    self._tool_activity_payload(
                        request.tenant_id, step_result.tool_call.name, tool_result
                    ),
                )
                self.db.commit()
                self.db.refresh(chat_session)

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
                reflection_stream_events,
            )
            for event_name, payload in reflection_stream_events:
                yield self._stream_event(event_name, chat_session, payload)

            yield self._stream_status(chat_session, "responding", "正在生成回复")
            chunks: list[str] = []
            for chunk in self.response_generator.generate_stream(
                request.message,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                model_config,
                persona_prompt,
                memory_context,
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

            self.runtime.finish_interrupt_response(chat_session)
            if router_decision.decision == "handoff_human" or step_result.handoff:
                chat_session.status = "handoff"
            elif self._should_complete_skill(active_skill, chat_session, step_result, tool_result):
                self._complete_active_skill(request.tenant_id, chat_session, active_skill, "step_completed")
            self._capture_memory(request, chat_session, reply, step_result, tool_result)

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
        self._finalize_turn(chat_session, request.tenant_id, reply)
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
        memory_context = [memory_read(row) for row in self.memory.recall(request.tenant_id, request.user_id, request.message)]
        if memory_context:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_recalled",
                {"memories": memory_context},
            )
        self.db.commit()
        self.db.refresh(chat_session)

        status("routing")
        router_decision = self.router.decide(request.message, chat_session, skills, model_config)
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
            request, chat_session, active_skill, tools, model_config, router_decision
        )

        tool_result: ToolResult | None = None
        self.db.commit()
        self.db.refresh(chat_session)
        if step_result.tool_call:
            status("tool", {"tool_name": step_result.tool_call.name})
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_started",
                step_result.tool_call.model_dump(),
            )
            tool_result = self.tool_executor.execute(
                request.tenant_id, step_result.tool_call, chat_session.active_skill_id
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_finished",
                tool_result.model_dump(),
            )
            self._advance_after_successful_tool(
                request.tenant_id, chat_session, active_skill, step_result, tool_result
            )
            self.db.commit()
            self.db.refresh(chat_session)

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
        )

        return PreparedTurn(
            chat_session=chat_session,
            model_config=model_config,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            memory_context=memory_context,
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
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        rounds = max(0, min(max_rounds, REFLECTION_MAX_ROUNDS_LIMIT))
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
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None, bool]:
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
        if stream_events is not None:
            stream_events.append(
                (
                    "status",
                    {
                        "phase": "tool",
                        "text": f"正在调用工具 {retry_tool_call.name}",
                        "tool_name": retry_tool_call.name,
                    },
                )
            )
        retry_tool_result = self._execute_tool_call(request, chat_session, retry_tool_call)
        self._advance_after_successful_tool(
            request.tenant_id, chat_session, active_skill, retry_step_result, retry_tool_result
        )
        self.db.commit()
        self.db.refresh(chat_session)
        if stream_events is not None:
            stream_events.append(
                (
                    "tool_result",
                    self._tool_activity_payload(
                        request.tenant_id, retry_tool_call.name, retry_tool_result
                    ),
                )
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
            stream_events,
        )
        self.db.commit()
        self.db.refresh(chat_session)

        tool_result: ToolResult | None = None
        if step_result.tool_call:
            if stream_events is not None:
                stream_events.append(
                    (
                        "status",
                        {
                            "phase": "tool",
                            "text": f"正在调用工具 {step_result.tool_call.name}",
                            "tool_name": step_result.tool_call.name,
                        },
                    )
                )
            tool_result = self._execute_tool_call(request, chat_session, step_result.tool_call)
            self._advance_after_successful_tool(
                request.tenant_id, chat_session, active_skill, step_result, tool_result
            )
            self.db.commit()
            self.db.refresh(chat_session)
            if stream_events is not None:
                stream_events.append(
                    (
                        "tool_result",
                        self._tool_activity_payload(
                            request.tenant_id, step_result.tool_call.name, tool_result
                        ),
                    )
                )
        return active_skill, router_decision, step_result, tool_result

    def _run_step_agent_with_context_repair(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> StepAgentResult:
        step_result = self._run_step_agent_once(
            request, chat_session, active_skill, tools, model_config
        )
        self._apply_step_result(request.tenant_id, chat_session, step_result)
        step_result = self._retry_slot_validation_if_needed(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            step_result,
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
                "satisfied_step_advanced",
            )
            self._apply_step_result(request.tenant_id, chat_session, step_result)
            self._advance_past_satisfied_collection_steps(
                request.tenant_id, chat_session, active_skill
            )

        if (
            not step_result.tool_call
            and not step_result.handoff
            and self._router_allows_schema_tool_repair(router_decision, chat_session)
        ):
            inferred_tool_call = self._tool_call_from_active_step(chat_session, active_skill, tools)
            if inferred_tool_call:
                step_result.tool_call = inferred_tool_call
                step_result.is_step_completed = True
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "step_agent_result_repaired",
                    {
                        "mode": "schema_tool_call",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "tool_call": inferred_tool_call.model_dump(),
                    },
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
            "slot_validation",
            {
                "reason": "slot_validation",
                "missing_expected_user_info": missing_fields,
                "previous_step_result": step_result.model_dump(mode="json"),
            },
        )
        if not self._step_result_has_progress(validation_result):
            return step_result
        if not validation_result.reply and step_result.reply:
            validation_result.reply = step_result.reply
        self._apply_step_result(request.tenant_id, chat_session, validation_result)
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
        repair_reason: str | None = None,
        repair_context: dict[str, object] | None = None,
    ) -> StepAgentResult:
        step_result = self.step_agent.run(
            request.message,
            chat_session,
            active_skill,
            tools,
            model_config,
            repair_context,
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
        self, tenant_id: str, chat_session: ChatSession, step_result: StepAgentResult
    ) -> None:
        if step_result.slot_updates:
            chat_session.slots_json = {**(chat_session.slots_json or {}), **step_result.slot_updates}
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        if step_result.next_step_id:
            previous_step = chat_session.active_step_id
            chat_session.active_step_id = step_result.next_step_id
            if previous_step != step_result.next_step_id:
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_step_changed",
                    {"from_step_id": previous_step, "to_step_id": step_result.next_step_id},
                )

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
            actions = [str(action) for action in current.get("allowed_actions", [])]
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
                    "from_step_id": previous_step,
                    "to_step_id": next_step_id,
                    "reason": "expected_info_satisfied",
                },
            )
            step_index += 1
        return changed

    def _step_can_act_without_more_user_input(self, actions: list[str]) -> bool:
        return (
            self._actions_allow_final_reply(actions)
            or "handoff_human" in actions
            or any(action.startswith("call_tool:") for action in actions)
        )

    def _tool_call_from_active_step(
        self, chat_session: ChatSession, active_skill: Skill | None, tools: list[Tool]
    ) -> ToolCall | None:
        if not active_skill:
            return None
        step = self._current_skill_step(active_skill, chat_session.active_step_id)
        if not step:
            return None
        tools_by_name = {tool.name: tool for tool in tools if tool.enabled}
        for action in [str(item) for item in step.get("allowed_actions", [])]:
            if not action.startswith("call_tool:"):
                continue
            tool_name = action.split(":", 1)[1].strip()
            tool = tools_by_name.get(tool_name)
            if not tool:
                continue
            if (
                tool.allowed_skills_json
                and active_skill.skill_id not in tool.allowed_skills_json
            ):
                continue
            arguments = self._build_tool_arguments_from_slots(tool, chat_session.slots_json or {})
            required = [str(field) for field in (tool.input_schema or {}).get("required", [])]
            if all(self._slot_has_value(arguments, field) for field in required):
                return ToolCall(name=tool.name, arguments=arguments)
        return None

    def _execute_tool_call(
        self, request: ChatTurnRequest, chat_session: ChatSession, tool_call: ToolCall
    ) -> ToolResult:
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_started",
            tool_call.model_dump(),
        )
        tool_result = self.tool_executor.execute(
            request.tenant_id, tool_call, chat_session.active_skill_id
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_finished",
            tool_result.model_dump(),
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
            {"from_step_id": previous_step, "to_step_id": next_step_id, "reason": "tool_completed"},
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
            actions = [str(action) for action in step.get("allowed_actions", [])]
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
        return arguments

    def _should_try_reflection(
        self,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if router_decision.decision in {"handoff_human", "clarify"}:
            return True
        if tool_result is not None:
            return True
        return step_result.is_step_completed

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
        actions = [str(action) for action in step.get("allowed_actions", [])]
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

        actions = [str(action) for action in steps[step_index].get("allowed_actions", [])]
        if not actions:
            return True
        terminal_actions = {"answer_user", "reply", "handoff_human", "continue_flow"}
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
        actions = [str(action) for action in step.get("allowed_actions", [])]
        if not self._actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field)
            for field in expected
        )

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
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

    def _list_published_skills(self, tenant_id: str) -> list[Skill]:
        return list(
            self.db.exec(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
            ).all()
        )

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
            "from_step_id": before_step,
            "to_step_id": chat_session.active_step_id,
            "skill_stack": chat_session.skill_stack_json,
        }
        event_type = "skill_step_changed"
        if decision.decision == "start_skill":
            event_type = "skill_started"
        elif decision.decision == "suspend_current_and_start_new_skill":
            event_type = "skill_suspended"
        elif decision.decision == "exit_current_skill":
            event_type = "skill_resumed" if chat_session.active_skill_id else "skill_exited"
        elif decision.decision == "handoff_human":
            event_type = "handoff_triggered"
        self.events.record(tenant_id, chat_session.id, event_type, payload)

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
        return {
            "activeSkillId": chat_session.active_skill_id,
            "activeStepId": chat_session.active_step_id,
            "currentSkills": current_skills,
            **(runtime_context or {}),
        }

    def _tool_activity_payload(self, tenant_id: str, tool_name: str, tool_result: ToolResult) -> dict[str, object]:
        tool = self.db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_name)
        ).first()
        return {
            "toolId": tool_name,
            "toolName": tool.display_name or tool.name if tool else tool_name,
            "rawToolName": tool_name,
            "content": tool_result.model_dump(mode="json"),
            "isError": not tool_result.success,
            "success": tool_result.success,
        }

    def _capture_memory(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        reply: str,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> list[dict[str, object]]:
        saved = [memory_read(row) for row in self.memory.capture_turn(request, chat_session, reply, step_result, tool_result)]
        if saved:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_saved",
                {"memories": saved},
            )
        return saved

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
        chat_session.last_agent_question = reply if "？" in reply or "?" in reply else chat_session.last_agent_question
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
