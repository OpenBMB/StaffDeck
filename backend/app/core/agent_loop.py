from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from time import sleep
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    model_for_agent,
)
from app.channels.service_outbox import stage_channel_delivery
from app.core.agent_identity_prompt import AgentIdentityPrompt
from app.core.cancellation import clear_chat_turn_cancelled
from app.core.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)
from app.core.conversation_projection import ConversationProjection
from app.config import get_settings
from app.core.graph_rules import GraphRules
from app.core.harness_agent import HarnessExecutionCancelled
from app.core.harness_session_lock import HarnessSessionBusy
from app.core.harness_turn_store import HarnessTurnConflict
from app.core.harness_v2_engine import (
    HarnessV2Engine,
    _with_recoverable_first_session,
    get_or_create_harness_session,
)
from app.core.human_handoff_service import HumanHandoffService
from app.core.response_generator import (
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.skill_runtime import SkillRuntime
from app.core.slash_commands import SlashCommandError
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChatSession,
    HarnessTurnRecord,
    HumanHandoffRequest,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    UIConfig,
    new_id,
    utc_now,
)
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    restore_truncated_atomic_references,
)
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    resolve_model_config_for_runtime,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService
from app.observability import EventLog
from app.observability.spans import llm_operation
from app.session.helpers import public_session
from app.session.message_visibility import visible_message_content, visible_message_rows
from app.session.origin import PILOTDECK_GROUP_CHAT_CHANNEL
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    RouterDecision,
    StepAgentResult,
)
from app.tools.tool_schema import ToolResult

logger = logging.getLogger(__name__)

STREAM_CHUNK_INTERVAL_SECONDS = 0.045
MAX_TOOL_ACTIONS_PER_TURN = 32
MAX_TOOL_ACTIONS_PER_TURN_LIMIT = 100
GRAPH_PENDING_STEPS_SLOT = "_graph_pending_steps"
CANCELLED_ASSISTANT_REPLY = "已停止生成"
ExecutionFinalizeState = Literal["continued", "completed", "handoff"]


def _knowledge_scope_ids(
    scope: dict[str, Any],
    plural_key: str,
    singular_key: str,
) -> list[str]:
    values = scope.get(plural_key)
    if not isinstance(values, list):
        singular = scope.get(singular_key)
        values = [singular] if singular else []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _find_handoff_node_id_in_skill(
    skill: Skill, active_step_id: str | None = None
) -> str | None:
    """查找 SOP 中从当前节点可达的 handoff 节点。

    使用 GraphRules.find_handoff_node_id 做基于 edges 的 BFS,
    优先返回从 active_step_id 可达的 handoff 节点,而非数组顺序的第一个。
    """
    content = skill.content_json or {}
    return GraphRules.find_handoff_node_id(content, active_step_id)


def _agent_identity_prompt(agent: AgentProfile) -> str:
    return AgentIdentityPrompt.render(
        agent,
        single_line=_single_line_text,
        metadata_formatter=_metadata_prompt_text,
    )


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
    return AgentIdentityPrompt.single_line(value)


class AgentLoopPreconditionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AgentLoop:
    def __init__(
        self,
        db: Session,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        stream_sink: Any | None = None,
    ) -> None:
        self.db = db
        self.events = EventLog(db, event_sink=event_sink)
        self.stream_sink = stream_sink
        self.stream_delivery_succeeded = False
        self.runtime = SkillRuntime()
        self.response_generator = ResponseGenerator()
        self.memory = MemoryService(db)

    def _sop_service(self):
        """Compatibility callers delegate into the module, never the reverse."""
        from staffdeck_harness.sop.lifecycle import SopRuntime

        return SopRuntime(
            getattr(self, "db", None), getattr(self, "events", None),
            create_handoff=self._create_human_handoff_request
        )

    def _turn_payload(self, payload: dict[str, Any], user_message_id: str | None) -> dict[str, Any]:
        data = dict(payload)
        if user_message_id:
            data.setdefault("user_message_id", user_message_id)
            data.setdefault("turn_id", user_message_id)
        return data

    def _label_engine_for(self, request: ChatTurnRequest, chat_session: ChatSession | None) -> None:
        """Decide up front which engine this turn will run on so the very first events are labelled right."""

        settings = get_settings()
        label = "harness_v2"
        if self._harness_v3_reachable(settings):
            try:
                from staffdeck_harness.bridge.engine_host import EngineHost

                agent_id = request.agent_id or (chat_session.agent_id if chat_session is not None else None)
                if EngineHost(settings).selects_harness_v3(request, agent_id, db=self.db):
                    label = "harness_v3"
            except Exception:  # pragma: no cover - labelling must never break a turn
                label = "harness_v2"
        if hasattr(self.events, "execution_engine"):
            self.events.execution_engine = None if label == "harness_v2" else label

    @staticmethod
    def _harness_v3_reachable(settings: Any) -> bool:
        """Is the Harness v3 package worth consulting for this turn?

        ``harness_v3_enabled`` is the deployment *default* engine; a per-staff choice may still
        pick Harness v3 while the default is v2 (canary). So the package is consulted whenever the
        engine is enabled globally *or* the admin console (per-staff choice) is on. A deployment
        with both off never imports the package, so it never loads Node/MCP dependencies.
        """

        return bool(getattr(settings, "harness_v3_enabled", False)) or bool(getattr(settings, "harness_admin_api_enabled", False))

    def _open_engine(self, request: ChatTurnRequest) -> HarnessV2Engine:
        # The Harness v3 engine lives in the parallel ``staffdeck_harness`` package and is
        # only imported when the deployment opts in, so a legacy deployment
        # never loads Node/MCP dependencies.
        settings = get_settings()
        from staffdeck_harness.modules.registry import peek_registry

        if not self._harness_v3_reachable(settings) and peek_registry() is None:
            return HarnessV2Engine(self)
        try:
            from staffdeck_harness.bridge.engine_host import EngineHost
        except ImportError:  # package not installed: legacy deployment
            return HarnessV2Engine(self)

        agent_id = request.agent_id
        if not agent_id and request.session_id and hasattr(self.db, "get"):
            row = self.db.get(ChatSession, request.session_id)
            agent_id = row.agent_id if row is not None else None
        return EngineHost(settings).open(self, request, agent_id)

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        engine = self._open_engine(request)
        chat_session: ChatSession | None = None
        user_message_id: str | None = None
        step_result = StepAgentResult(action="reply")
        try:
            return engine.run(request)
        except (HarnessTurnConflict, HarnessSessionBusy) as exc:
            chat_session = engine.session
            self.db.rollback()
            chat_session = chat_session or self._get_or_create_session(request)
            error_code = (
                "HARNESS_SESSION_BUSY"
                if isinstance(exc, HarnessSessionBusy)
                else "HARNESS_TURN_CONFLICT"
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "turn_rejected",
                {
                    "code": error_code,
                    "message": str(exc),
                    "client_turn_id": request.client_turn_id,
                },
            )
            self.db.commit()
            return ChatTurnResponse(
                reply=format_runtime_failure_reply(
                    "Harness 并发或重复请求已阻止",
                    exc,
                    error_code,
                    "请等待原请求完成，或为新请求使用新的 client_turn_id。",
                ),
                session_id=chat_session.id,
                runtime_error_code=error_code,
                step_result=step_result,
                session_state=public_session(chat_session),
            )
        except HarnessExecutionCancelled:
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_cancelled()
            chat_session = chat_session or self._get_or_create_session(request)
            if user_message_id:
                self._persist_cancelled_assistant_message(
                    request.tenant_id,
                    chat_session,
                    user_message_id,
                    request.client_turn_id,
                )
            self.db.commit()
            for turn_id in (user_message_id, request.client_turn_id):
                if turn_id:
                    clear_chat_turn_cancelled(chat_session.id, turn_id)
            return ChatTurnResponse(
                reply=CANCELLED_ASSISTANT_REPLY,
                session_id=chat_session.id,
                step_result=step_result,
                session_state=public_session(chat_session),
            )
        except (AgentLoopPreconditionError, SlashCommandError) as exc:
            chat_session = engine.session
            engine.mark_interrupted(exc.code, exc.message)
            chat_session = chat_session or self._get_or_create_session(request)
            return self._finish_with_error(chat_session, exc.code, exc.message)
        except LLMError as exc:
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_interrupted("LLM_ERROR", str(exc))
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
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_interrupted("HARNESS_V2_ERROR", str(exc))
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "HARNESS_V2_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "Harness v2 执行出错",
                exc,
                "HARNESS_V2_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )
        finally:
            terminal_record = getattr(engine, "turn_record", None)
            terminal_session = getattr(engine, "session", None)
            if (
                terminal_record is not None
                and terminal_session is not None
                and terminal_record.status in {"completed", "failed", "cancelled"}
            ):
                for turn_id in (engine.user_message_id, request.client_turn_id):
                    if turn_id:
                        clear_chat_turn_cancelled(terminal_session.id, turn_id)
            engine.close()

        reply = self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
            assistant_metadata_override=(
                {"message_visibility": request.message_visibility}
                if request.message_visibility != "visible"
                else None
            ),
        )
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            step_result=step_result,
            session_state=public_session(chat_session),
        )

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        yield from self._handle_turn_stream_v2(request)

    def _handle_turn_stream_v2(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        session_request = _with_recoverable_first_session(request)
        existing_session = (
            self.db.get(ChatSession, session_request.session_id)
            if session_request.session_id
            else None
        )
        chat_session = get_or_create_harness_session(
            self,
            session_request,
        )
        created_session = existing_session is None
        scoped_request = request.model_copy(update={"session_id": chat_session.id})
        initial_turn_id = str(request.client_turn_id or "").strip() or None
        self._label_engine_for(scoped_request, chat_session)
        if created_session:
            yield self._stream_event(
                "session_created",
                chat_session,
                {
                    "sessionId": chat_session.id,
                    "turn_id": initial_turn_id,
                    "client_turn_id": request.client_turn_id,
                    "execution_engine": "harness_v2",
                },
            )
        yield self._stream_event(
            "user_message_received",
            chat_session,
            self._turn_payload(
                {
                    "sessionId": chat_session.id,
                    "client_turn_id": request.client_turn_id,
                    "execution_engine": "harness_v2",
                },
                initial_turn_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "planning",
            "正在规划本轮任务",
            {"execution_engine": "harness_v2"},
            user_message_id=initial_turn_id,
        )
        response = self.handle_turn(scoped_request)
        chat_session = self.db.get(ChatSession, response.session_id)
        if chat_session is None:
            return
        user_message = None
        client_turn_id = str(request.client_turn_id or "").strip()
        if client_turn_id:
            receipt = self.db.exec(
                select(HarnessTurnRecord).where(
                    HarnessTurnRecord.tenant_id == request.tenant_id,
                    HarnessTurnRecord.session_id == response.session_id,
                    HarnessTurnRecord.client_turn_id == client_turn_id,
                )
            ).first()
            if receipt is not None and receipt.user_message_id:
                candidate = self.db.get(Message, receipt.user_message_id)
                if (
                    candidate is not None
                    and candidate.tenant_id == request.tenant_id
                    and candidate.session_id == response.session_id
                    and candidate.role == "user"
                ):
                    user_message = candidate
        if user_message is None and not client_turn_id:
            user_message = self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == request.tenant_id,
                    Message.session_id == response.session_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc())
            ).first()
        user_message_id = user_message.id if user_message else None
        if response.reply == CANCELLED_ASSISTANT_REPLY:
            yield self._stream_event(
                "stream_cancelled",
                chat_session,
                self._turn_payload(
                    {
                        "phase": "cancelled",
                        "text": CANCELLED_ASSISTANT_REPLY,
                        "client_turn_id": request.client_turn_id,
                        "execution_engine": "harness_v2",
                    },
                    user_message_id or initial_turn_id,
                ),
            )
            return
        resolved_turn_id = user_message_id or initial_turn_id
        if response.runtime_error_code:
            yield self._stream_event(
                "error",
                chat_session,
                self._turn_payload(
                    {
                        "code": response.runtime_error_code,
                        "message": response.reply,
                        "client_turn_id": request.client_turn_id,
                        "execution_engine": "harness_v2",
                    },
                    resolved_turn_id,
                ),
            )
            return
        for chunk in self.response_generator.chunk_text(response.reply):
            event = self._stream_event(
                "stream_delta",
                chat_session,
                self._turn_payload(
                    {
                        "content": chunk,
                        "execution_engine": "harness_v2",
                    },
                    resolved_turn_id,
                ),
            )
            self.db.commit()
            yield event
        end_event = self._stream_event(
            "stream_end",
            chat_session,
            self._turn_payload({"execution_engine": "harness_v2"}, resolved_turn_id),
        )
        self.db.commit()
        yield end_event
        yield self._stream_event(
            "complete",
            chat_session,
            self._turn_payload(
                {
                    **response.model_dump(mode="json"),
                    "execution_engine": "harness_v2",
                },
                resolved_turn_id,
            ),
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

    def _current_step_allows_human_handoff(
        self, skill: Skill | None, active_step_id: str | None
    ) -> bool:
        return self._sop_service().current_step_allows_human_handoff(skill, active_step_id)

    def _maybe_route_to_handoff_node(
        self, chat_session: ChatSession, active_skill: Skill | None
    ) -> bool:
        return self._sop_service().maybe_route_to_handoff_node(chat_session, active_skill)

    def _step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        return self._sop_service().step_declares_human_handoff(step)

    def _human_handoff_assignee_user_id(
        self, tenant_id: str, agent_id: str | None, fallback_user_id: str | None
    ) -> str | None:
        return HumanHandoffService(self.db, getattr(self, "events", None)).assignee_user_id(
            tenant_id,
            agent_id,
            fallback_user_id,
            tenant_admin_resolver=self._human_handoff_tenant_admin_user_id,
        )

    def _human_handoff_tenant_admin_user_id(self, tenant_id: str) -> str | None:
        return HumanHandoffService(self.db, getattr(self, "events", None)).tenant_admin_user_id(
            tenant_id
        )

    def _human_handoff_context_summary(self, chat_session: ChatSession) -> str:
        return HumanHandoffService(self.db, getattr(self, "events", None)).context_summary(
            chat_session
        )

    def _human_handoff_pending_question(
        self, current_step: dict[str, Any] | None, step_result: StepAgentResult
    ) -> str:
        return HumanHandoffService.pending_question(current_step, step_result)

    def _step_actions(self, step: dict[str, Any]) -> list[str]:
        return self._sop_service().step_actions(step)

    def _finish_stale_completed_skill(
        self, tenant_id: str, chat_session: ChatSession, skills: list[Skill]
    ) -> None:
        return self._sop_service().finish_stale_completed_skill(tenant_id, chat_session, skills)

    def _should_complete_skill(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        return self._sop_service().should_complete_skill(skill, chat_session, step_result, tool_result)

    def _is_terminal_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._sop_service().is_terminal_skill_state(skill, chat_session)

    def _is_answer_ready_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._sop_service().is_answer_ready_skill_state(skill, chat_session)

    def _graph_flow_has_unfinished_work(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult | None = None,
    ) -> bool:
        return self._sop_service().graph_flow_has_unfinished_work(skill, chat_session, step_result)

    def _is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        return self._sop_service().is_terminal_skill_position(skill, active_step_id, slots)

    def _current_step_can_finish_after_tool(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._sop_service().current_step_can_finish_after_tool(skill, chat_session)

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
        return self._sop_service().actions_allow_final_reply(actions)

    def _complete_active_skill(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill, reason: str
    ) -> None:
        return self._sop_service().complete_active_skill(tenant_id, chat_session, skill, reason)

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        return self._sop_service().finalize_execution_after_reply(tenant_id, chat_session, active_skill, router_decision, step_result, tool_result)

    def _create_human_handoff_request(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
    ) -> HumanHandoffRequest:
        # SOP 节点指定的处理人:从当前 step 的 assignee_user_id 字段读取
        # (handoff 类型节点或 allowed_actions 含 handoff_human 的节点可配置)。
        # assignee_notify_channel 指定投递渠道:None=默认;"web"=仅网页端;绑定渠道=按渠道转接。
        step_assignee_user_id: str | None = None
        step_notify_channel: str | None = None
        current_step = (
            self._current_skill_step(active_skill, chat_session.active_step_id)
            if active_skill
            else None
        )
        if isinstance(current_step, dict):
            step_assignee_user_id = (
                str(current_step.get("assignee_user_id") or "").strip() or None
            )
            step_notify_channel = (
                str(current_step.get("assignee_notify_channel") or "").strip() or None
            )
        # 当前渠道默认处理人:从会话所属 binding 的 config_json 读取。
        binding_default_assignee_user_id, binding_default_notify_channel = (
            self._binding_default_handoff_assignee(tenant_id, chat_session)
        )
        handoff = HumanHandoffService(self.db, self.events).create(
            tenant_id,
            chat_session,
            step_result,
            current_step_resolver=lambda: current_step,
            assignee_resolver=self._human_handoff_assignee_user_id,
            context_summary=self._human_handoff_context_summary,
            pending_question=self._human_handoff_pending_question,
            step_assignee_user_id=step_assignee_user_id,
            binding_default_assignee_user_id=binding_default_assignee_user_id,
            step_notify_channel=step_notify_channel,
            binding_default_notify_channel=binding_default_notify_channel,
        )
        # 给 assignee 发渠道私聊通知。失败仅记日志,不影响 handoff 主流程
        # (网页收件箱仍可兜底)。
        self._maybe_notify_handoff_assignee(tenant_id, chat_session, handoff)
        return handoff

    def _binding_default_handoff_assignee(
        self,
        tenant_id: str,
        chat_session: ChatSession,
    ) -> tuple[str | None, str | None]:
        """会话所属渠道绑定配置的默认人工处理人及其通知渠道。

        从 ChatSession.channel_binding_id 反查 binding(而非 agent 挂载列表取首个),
        读取 config_json.default_handoff_assignee_user_id 与
        default_handoff_assignee_channel。无 binding 或未配置返回 (None, None)。
        """
        if not chat_session.channel_binding_id:
            return None, None
        binding = self.db.get(ChannelBinding, chat_session.channel_binding_id)
        if not binding or binding.tenant_id != tenant_id:
            return None, None
        config = binding.config_json if isinstance(binding.config_json, dict) else {}
        value = str(config.get("default_handoff_assignee_user_id") or "").strip()
        if not value:
            return None, None
        channel = str(config.get("default_handoff_assignee_channel") or "").strip()
        return value, (channel or None)

    def _maybe_notify_handoff_assignee(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        handoff: HumanHandoffRequest,
    ) -> None:
        """按通知渠道偏好解析投递 binding,给 assignee 登记渠道私聊通知。

        绑定解析规则:
        - 偏好为具体渠道(如 feishu)时:优先会话所属 binding(渠道匹配且 active);
          会话无 binding 或渠道不匹配时,在租户内找该渠道的任一 active 员工绑定。
        - 偏好为 None(默认)时:用会话所属 binding(渠道支持私聊通知即可达)。
        - 偏好为 "web" 时:仅网页收件箱,直接返回。

        无可用 binding(含日志说明)或 assignee 在该 binding scope 无非群聊身份时,
        由 notify_handoff_assignee 内部跳过,网页收件箱兜底。
        """
        from staffdeck_harness.modules.registry import peek_registry

        if peek_registry() is not None:
            from staffdeck_harness.handoff.core import for_session

            core = for_session(self.db, chat_session)
            core.notify(handoff, pending_question=handoff.pending_question or "", context_summary=handoff.context_summary or "")
            return
        from app.channels.service_outbox import (
            HANDOFF_NOTIFY_CHANNELS,
            notify_handoff_assignee,
            resolve_handoff_notify_binding,
        )

        metadata = handoff.metadata_json if isinstance(handoff.metadata_json, dict) else {}
        notify_channel = str(metadata.get("assignee_notify_channel") or "").strip()
        if notify_channel == "web":
            return
        binding: ChannelBinding | None = None
        if notify_channel:
            # 指定渠道:优先会话所属 binding,渠道不匹配时回退租户内该渠道任一 binding。
            if chat_session.channel_binding_id:
                session_binding = self.db.get(ChannelBinding, chat_session.channel_binding_id)
                if (
                    session_binding
                    and session_binding.tenant_id == tenant_id
                    and session_binding.channel == notify_channel
                    and session_binding.status == "active"
                ):
                    binding = session_binding
            binding = binding or resolve_handoff_notify_binding(self.db, tenant_id, notify_channel)
            if binding is None:
                logger.warning(
                    "handoff 通知跳过:租户无可用的 %s 绑定 handoff=%s", notify_channel, handoff.id
                )
                return
        else:
            # 默认投递:用会话所属 binding,渠道支持私聊通知即可达。
            if not chat_session.channel_binding_id:
                return
            session_binding = self.db.get(ChannelBinding, chat_session.channel_binding_id)
            if (
                not session_binding
                or session_binding.tenant_id != tenant_id
                or session_binding.status != "active"
            ):
                return
            if session_binding.channel not in HANDOFF_NOTIFY_CHANNELS:
                return
            binding = session_binding
        notify_handoff_assignee(
            self.db,
            binding,
            handoff,
            handoff.pending_question or "",
            handoff.context_summary or "",
        )

    def _apply_step_result(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None = None,
    ) -> None:
        return self._sop_service().apply_step_result(tenant_id, chat_session, step_result, active_skill)

    def _sync_awaiting_input_from_step_result(
        self,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None,
        *,
        source_skill_id: str | None,
        source_step_id: str | None,
    ) -> None:
        return self._sop_service().sync_awaiting_input_from_step_result(chat_session, step_result, active_skill, source_skill_id=source_skill_id, source_step_id=source_step_id)

    def _change_active_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        next_step_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        return self._sop_service().change_active_step(tenant_id, chat_session, next_step_id, reason=reason)

    def _graph_pending_steps(self, chat_session: ChatSession) -> list[str]:
        return self._sop_service().graph_pending_steps(chat_session)

    def _store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        return self._sop_service().store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def _queue_graph_sibling_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        source_step_id: str | None,
        selected_step_id: str,
    ) -> None:
        return self._sop_service().queue_graph_sibling_steps(tenant_id, chat_session, active_skill, source_step_id, selected_step_id)

    def _edge_condition(self, edge: dict[str, Any]) -> str:
        return self._sop_service().edge_condition(edge)

    def _activate_next_pending_graph_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        *,
        reason: str,
    ) -> bool:
        return self._sop_service().activate_next_pending_graph_step(tenant_id, chat_session, active_skill, reason=reason)

    def _skill_has_step(self, skill: Skill, step_id: str | None) -> bool:
        return self._sop_service().skill_has_step(skill, step_id)

    def _first_step_id(self, skill: Skill) -> str | None:
        return self._sop_service().first_step_id(skill)

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return self._sop_service().skill_steps(skill)

    def _skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        return self._sop_service().skill_nodes(skill)

    def _ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        return self._sop_service().ordered_skill_nodes(skill)

    def _graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        return self._sop_service().graph_outgoing_edges(skill)

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        return self._sop_service().default_next_step(skill, active_step_id)

    def _get_or_create_session(self, request: ChatTurnRequest) -> ChatSession:
        session_id = request.session_id or new_id("session")
        chat_session = self.db.get(ChatSession, session_id)
        if not chat_session:
            chat_session = ChatSession(
                id=session_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                channel=(
                    request.channel
                    if request.channel in {PILOTDECK_GROUP_CHAT_CHANNEL, "skill_test"}
                    else None
                ),
            )
            self.db.add(chat_session)
            self.db.flush()
        elif not chat_session.agent_id and request.agent_id:
            chat_session.agent_id = request.agent_id
        return chat_session

    def _current_skill_step(
        self, skill: Skill, active_step_id: str | None
    ) -> dict[str, Any] | None:
        return self._sop_service().current_skill_step(skill, active_step_id)

    def _skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        return self._sop_service().skill_slot_satisfied(slots, field)

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

    def _get_agent_loop_max_actions(
        self, tenant_id: str, agent_id: str | None = None
    ) -> int:
        if not hasattr(self.db, "get"):
            return MAX_TOOL_ACTIONS_PER_TURN
        agent = self.db.get(AgentProfile, agent_id) if agent_id else None
        if agent is not None and (
            agent.tenant_id != tenant_id or agent.status != "active"
        ):
            agent = None
        if agent is not None:
            value = agent.harness_max_actions
            return max(1, min(int(value), MAX_TOOL_ACTIONS_PER_TURN_LIMIT))
        row = self.db.get(UIConfig, tenant_id)
        value = row.agent_loop_max_actions if row else MAX_TOOL_ACTIONS_PER_TURN
        return max(1, min(int(value), MAX_TOOL_ACTIONS_PER_TURN_LIMIT))

    def _get_conversation_context_settings(
        self,
        tenant_id: str,
    ) -> ConversationContextSettings:
        if not hasattr(self.db, "get"):
            return ConversationContextSettings()
        row = self.db.get(UIConfig, tenant_id)
        if row is None:
            return ConversationContextSettings()
        return ConversationContextSettings(
            token_budget=getattr(row, "context_token_budget", 32_000),
            compaction_trigger_ratio=getattr(
                row,
                "context_compaction_trigger_ratio",
                0.70,
            ),
            recent_round_limit=getattr(row, "context_recent_round_limit", 6),
            long_summary_token_budget=getattr(
                row,
                "context_long_summary_token_budget",
                4_000,
            ),
            medium_summary_token_budget=getattr(
                row,
                "context_medium_summary_token_budget",
                4_000,
            ),
            allowed_roles=frozenset(
                getattr(row, "context_allowed_roles", None)
                or {"user", "assistant"}
            ),
            long_summary_prefix=getattr(
                row,
                "context_long_summary_prefix",
                "历史的信息可以被总结为：",
            ),
            medium_summary_prefix=getattr(
                row,
                "context_medium_summary_prefix",
                "近期的历史信息总结为：",
            ),
        ).normalized()

    def _list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
        return self._sop_service().list_published_skills(tenant_id, agent_id)

    def _get_agent_profile(self, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
        if not agent_id:
            return None
        row = self.db.get(AgentProfile, agent_id)
        if not row or row.tenant_id != tenant_id or row.status != "active":
            return None
        return row

    def _get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None:
        return self._sop_service().get_active_skill(tenant_id, skill_id, agent_id)

    def _drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        return self._sop_service().drop_unavailable_skill_state(tenant_id, chat_session, skills)

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
        visible_rows = visible_message_rows(rows)
        context = build_conversation_context(
            [
                ConversationProjection.message_context_entry(
                    row,
                    content=visible_message_content(row),
                )
                for row in visible_rows
            ],
            settings=self._get_conversation_context_settings(chat_session.tenant_id),
            context_state=chat_session.context_state_json,
            summary_builder=self._context_summary_builder(model_config) if model_config else None,
        )
        next_state = context.get("context_state")
        if isinstance(next_state, dict) and next_state != (chat_session.context_state_json or {}):
            chat_session.context_state_json = next_state
            self.db.add(chat_session)
        return context

    def _context_summary_builder(self, model_config: ModelConfig) -> Callable[[str, str, int], str]:
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
                output_contract=(f"只输出一段纯文本摘要，控制在约 {token_budget} tokens 以内。"),
            )
            with llm_operation("context.compact"):
                return (
                    LLMClient(model_config).generate_text(unified_system_prompt(), payload).strip()
                )

        return summarize

    def _message_context_entry(self, row: Message) -> dict[str, Any]:
        return ConversationProjection.message_context_entry(row)

    def _assistant_message_metadata(
        self,
        step_result: StepAgentResult | None,
        chat_session: ChatSession,
        source_message: str | None = None,
    ) -> dict[str, Any]:
        return ConversationProjection.assistant_message_metadata(
            step_result, citation_deduper=self._dedupe_knowledge_citations
        )

    def _dedupe_knowledge_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return ConversationProjection.dedupe_knowledge_citations(citations)

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
        user_visibility = str(
            (user_message.metadata_json or {}).get("message_visibility") or "visible"
        )
        cancelled_metadata = {
            "turn_id": user_message_id,
            "user_message_id": user_message_id,
            "client_turn_id": normalized_client_turn_id or None,
            "status": "cancelled",
        }
        if user_visibility != "visible":
            cancelled_metadata["message_visibility"] = user_visibility
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            CANCELLED_ASSISTANT_REPLY,
            metadata=cancelled_metadata,
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
        return ConversationProjection.user_message_metadata(request)

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
        reply = self._finalize_turn(chat_session, chat_session.tenant_id, reply)
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
        assistant_metadata_override: dict[str, Any] | None = None,
    ) -> str:
        chat_session.updated_at = utc_now()
        if chat_session.status != "handoff":
            chat_session.status = "active"
        metadata = self._assistant_message_metadata(step_result, chat_session, source_message)
        if assistant_metadata_override:
            metadata = {**metadata, **dict(assistant_metadata_override)}
        reply = restore_truncated_atomic_references(reply, metadata.get("knowledge_citations"))
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
        if not self.stream_delivery_succeeded:
            stage_channel_delivery(self.db, chat_session, assistant_message)
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
        if assistant_metadata.get("message_visibility"):
            event_payload["message_visibility"] = assistant_metadata["message_visibility"]
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
        return reply

    def _mark_session_running(self, chat_session: ChatSession) -> None:
        if chat_session.status == "handoff":
            return
        chat_session.status = "running"
        chat_session.updated_at = utc_now()
        self.db.add(chat_session)

    @staticmethod
    def _fallback_session_title_from_message(message: str) -> str:
        return ConversationProjection.fallback_session_title(message)

    def _normalize_reply_citation_labels(self, reply: str, citations: object) -> str:
        return ConversationProjection.normalize_reply_citation_labels(reply, citations)

    def _strip_trailing_citation_summary(self, reply: str) -> str:
        return ConversationProjection.strip_trailing_citation_summary(reply)
