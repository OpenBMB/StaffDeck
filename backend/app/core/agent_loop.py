from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from time import sleep
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    model_for_agent,
    visible_published_skills,
    visible_skill,
)
from app.agents.free_model import resolve_turn_default_model
from app.agents.usage import bump_agent_usage
from app.channels.service_outbox import stage_channel_delivery
from app.chat_pubsub import publish_relay_wake
from app.core.converse.identity import AgentIdentityPrompt
from app.core.base.cancellation import clear_chat_turn_cancelled
from app.core.base.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)
from app.core.base.conversation_projection import ConversationProjection
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
from app.core.converse.response import (
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.skill_runtime import SkillRuntime
from app.core.slash_commands import SlashCommandError
from app.core.turn_finalizer import TurnFinalizer
from app.core.base.ui_flags import knowledge_citations_hidden
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
    strip_citation_markers,
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
STREAM_CHUNK_SIZE = 8
# 真流式增量的落库节流：模型碎片可能来得远快于人的阅读节奏，逐片写库 + 推送
# 会把 relay 变成高频小包。合并到 60ms 一档再落库，既保留「随到随显」的观感，
# 又把写库次数压到 ~16 次/秒以内；合并只影响可见节奏，不拖慢本轮完成时间。
STREAM_DELTA_FLUSH_INTERVAL_SECONDS = 0.06
STREAM_DELTA_FLUSH_MAX_CHARS = 400
# 每一次落库都是一次 DB 往返（生产环境为远程 PG），长回答若始终按 60ms 一档
# 落库，写库耗时会被累加进本轮收尾。这里给整段生成一个写库次数上限，超过后
# 按已流逝时间反推间隔，把写库次数锁在常数级。
STREAM_DELTA_MAX_FLUSHES = 150
# 兜底打字机预算：没有真流式可用的轮次（直出回复、固定话术）按整轮最多多花
# 2.5 秒铺开，取代原先「一次性暴发」。
STREAM_TYPING_BUDGET_SECONDS = 2.5
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


class TurnStreamSink:
    """流式轮次的实时投递器：执行期阶段状态 + 生成期增量文本。

    改造前 ``handle_turn_stream`` 要先等 ``handle_turn`` 整轮跑完才能拿到
    ``response.reply`` 再切块下发，纯问答轮次因此要等「整轮耗时」才有第一个字。
    本类让执行期与生成期都能持续写入 ``AgentEvent`` relay 行并唤醒订阅该会话的
    SSE，前端看到的节奏变成「执行结束 + TTFT」。

    只在流式路径挂载：非流式 ``handle_turn`` 不构造它，行为与改造前一致。
    """

    def __init__(self, owner: AgentLoop) -> None:
        self._owner = owner
        self._tenant_id = ""
        self._session_id = ""
        self._buffer: list[str] = []
        self._streamed_parts: list[str] = []
        self._last_flush_at = 0.0
        self._started_at: float | None = None
        self.streamed_chars = 0

    @property
    def active(self) -> bool:
        return bool(self._tenant_id and self._session_id)

    @property
    def streamed(self) -> bool:
        return self.streamed_chars > 0

    def bind(self, tenant_id: str, session_id: str) -> None:
        self._tenant_id = str(tenant_id or "")
        self._session_id = str(session_id or "")
        self._buffer = []
        self._streamed_parts = []
        self._last_flush_at = 0.0
        self._started_at = None
        self.streamed_chars = 0

    def status(self, phase: str, text: str, **extra: object) -> None:
        if not self.active:
            return
        self._record("stream_status", {"phase": phase, "text": text, **extra})

    def delta(self, chunk: str) -> None:
        if not self.active or not chunk:
            return
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        self._buffer.append(chunk)
        self._streamed_parts.append(chunk)
        self.streamed_chars += len(chunk)
        pending = sum(len(item) for item in self._buffer)
        if (
            now - self._last_flush_at < self._flush_interval(now)
            and pending < STREAM_DELTA_FLUSH_MAX_CHARS
        ):
            return
        self.flush()

    def _flush_interval(self, now: float) -> float:
        """落库最小间隔：短回复固定 60ms，长回复按上限次数自动摊开。"""

        if self._started_at is None:
            return STREAM_DELTA_FLUSH_INTERVAL_SECONDS
        elapsed = max(0.0, now - self._started_at)
        return max(
            STREAM_DELTA_FLUSH_INTERVAL_SECONDS,
            elapsed / STREAM_DELTA_MAX_FLUSHES,
        )

    def flush(self) -> None:
        if not self.active or not self._buffer:
            return
        content = "".join(self._buffer)
        self._buffer = []
        self._last_flush_at = time.monotonic()
        self._record("stream_delta", {"content": content})

    def reset(self) -> None:
        """丢弃已下发的增量（生成中途失败降级时用，避免残留半截文本）。

        清空 ``streamed_chars`` 让调用方知道「流式内容已作废」，可以安全地
        按老路径重新分片下发。
        """
        if not self.active:
            return
        if not self.streamed_chars and not self._buffer:
            return
        self._buffer = []
        self._streamed_parts = []
        self.streamed_chars = 0
        self._last_flush_at = 0.0
        self._started_at = None
        self._record("stream_replace", {"content": ""})

    def replace(self, content: str) -> None:
        """用给定正文原地替换已下发内容（最终正文不可续写时用）。"""
        if not self.active:
            return
        self._buffer = []
        self._streamed_parts = [content] if content else []
        self._last_flush_at = time.monotonic()
        self.streamed_chars = len(content)
        self._record("stream_replace", {"content": content})

    def reconcile(self, content: str) -> None:
        """收尾对账：下发内容与最终正文不一致时用最终正文原地替换。

        预览通道（决策 JSON 的 ``finish.reply_fragment``）只负责「正文早出现」，
        正确性由这里兜底——引用编号重排、``finish`` 被拒后重来、降级话术、最终
        裁剪都会落到这条路径。两者一致时不产生任何事件。
        """

        if not self.active or not self.streamed_chars:
            return
        if "".join(self._streamed_parts) == content:
            return
        self.replace(content)

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        self._owner.events.record(
            self._tenant_id, self._session_id, event_type, dict(payload)
        )
        self._owner.db.commit()
        # relay 行已提交：唤醒监听该会话的 SSE 流（Redis 不可用时为 no-op，
        # SSE 走兜底轮询，正确性不受影响）。
        publish_relay_wake(self._session_id)


class AgentLoop:
    def __init__(
        self,
        db: Session,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.events = EventLog(db, event_sink=event_sink)
        self.runtime = SkillRuntime()
        self.response_generator = ResponseGenerator()
        self.memory = MemoryService(db)
        # 仅流式轮次挂载（见 _handle_turn_stream_v2）；非流式路径恒为 None。
        self.turn_stream: TurnStreamSink | None = None

    def _turn_payload(self, payload: dict[str, Any], user_message_id: str | None) -> dict[str, Any]:
        data = dict(payload)
        if user_message_id:
            data.setdefault("user_message_id", user_message_id)
            data.setdefault("turn_id", user_message_id)
        return data

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        engine = HarnessV2Engine(self, turn_stream=self.turn_stream)
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
        # 真流式：把 sink 挂到本轮 AgentLoop 上，harness_v2 在执行期（检索/调工具）
        # 与生成期（最终回复）都能把内容实时写给 SSE relay，而不是等整轮跑完。
        sink = TurnStreamSink(self)
        sink.bind(chat_session.tenant_id, chat_session.id)
        self.turn_stream = sink
        try:
            response = self.handle_turn(scoped_request)
        finally:
            self.turn_stream = None
        sink.flush()
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
        if sink.streamed:
            # 生成期已经真流式下发过正文，这里只做一次对账：最终正文与已下发
            # 内容不一致时原地替换。（当前预览通道已关闭，此分支为防御保留。）
            sink.reconcile(response.reply)
        else:
            # 2026-10-19：回到最初的分片下发，不做节奏控制。后端匀速回放（
            # 8 字/45ms）在前端看起来仍是一次性出现（传输/渲染又把分片合批了），
            # 但那一步会白白多花最多 STREAM_TYPING_BUDGET_SECONDS(2.5s)，故去掉
            # sleep/interval，只保留 8 字分片。
            for chunk in self.response_generator.chunk_text(
                response.reply, chunk_size=STREAM_CHUNK_SIZE
            ):
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
        if not skill:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        return bool(current_step and self._step_declares_human_handoff(current_step))

    def _maybe_route_to_handoff_node(
        self, chat_session: ChatSession, active_skill: Skill | None
    ) -> bool:
        """当 step_result.handoff=True 但当前 step 不声明 handoff 时,
        查找 SOP 中的 handoff 节点并路由到它。这使得后续的
        _create_human_handoff_request 能从 handoff 节点读取 assignee_user_id。

        返回 True 表示已路由到 handoff 节点。
        """
        if not active_skill or not chat_session.active_skill_id:
            return False
        current_step = self._current_skill_step(
            active_skill, chat_session.active_step_id
        )
        if current_step and self._step_declares_human_handoff(current_step):
            return False
        handoff_step_id = _find_handoff_node_id_in_skill(
            active_skill, chat_session.active_step_id
        )
        if not handoff_step_id:
            return False
        self._change_active_step(
            chat_session.tenant_id,
            chat_session,
            handoff_step_id,
            reason="handoff_node_routed_by_step_result",
        )
        return True

    def _step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        node_type = str(step.get("type") or "").strip()
        return node_type == "handoff" or "handoff_human" in self._step_actions(step)

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
        return GraphRules.step_actions(step)

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
        # Graph topology is authoritative for SOP completion. A non-terminal
        # node may allow an interim reply and all global slots may already be
        # filled, but an outgoing edge still means the workflow has work left.
        # Check this before the reply/tool completion shortcuts so transitioning
        # into an intermediate node cannot finish the entire SOP.
        if self._graph_flow_has_unfinished_work(skill, chat_session, step_result):
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

    def _is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        if not active_step_id:
            return False
        content = skill.content_json or {}
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        return GraphRules.terminal_position_from_step(
            content,
            active_step_id,
            slots,
            self._current_skill_step(skill, active_step_id),
            self._skill_slot_satisfied,
            self._step_actions,
        )

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
        return GraphRules.actions_allow_final_reply(actions)

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

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        return TurnFinalizer.finalize(
            tenant_id,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            current_step_allows_handoff=self._current_step_allows_human_handoff,
            route_to_handoff_node=self._maybe_route_to_handoff_node,
            create_handoff=self._create_human_handoff_request,
            record_event=self.events.record,
            should_complete=self._should_complete_skill,
            complete_skill=self._complete_active_skill,
        )

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
        - 偏好为具体渠道时:优先会话所属 binding(渠道匹配且 active);
          会话无 binding 或渠道不匹配时,在租户内找该渠道的任一 active 员工绑定。
        - 偏好为 None(默认)时:用会话所属 binding(渠道支持私聊通知即可达)。
        - 偏好为 "web" 时:仅网页收件箱,直接返回。

        无可用 binding(含日志说明)或 assignee 在该 binding scope 无非群聊身份时,
        由 notify_handoff_assignee 内部跳过,网页收件箱兜底。
        """
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
        source_skill_id = chat_session.active_skill_id
        source_step_id = chat_session.active_step_id
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

        active_skill_matches = bool(
            active_skill and active_skill.skill_id == chat_session.active_skill_id
        )
        invalid_next_step = False
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
                step_result.is_step_completed = False
                invalid_next_step = True

        self._sync_awaiting_input_from_step_result(
            chat_session,
            step_result,
            active_skill,
            source_skill_id=source_skill_id,
            source_step_id=source_step_id,
        )

        if not chat_session.active_skill_id:
            return
        if invalid_next_step:
            return
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
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

    def _sync_awaiting_input_from_step_result(
        self,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None,
        *,
        source_skill_id: str | None,
        source_step_id: str | None,
    ) -> None:
        if not active_skill or active_skill.skill_id != source_skill_id or not source_step_id:
            return

        step = self._current_skill_step(active_skill, source_step_id)
        if not step:
            return
        missing_fields = [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._skill_slot_satisfied(chat_session.slots_json or {}, str(field))
        ]
        is_waiting_reply = step_result.action in {"ask_user", "clarify"}
        if is_waiting_reply and missing_fields:
            previous = (
                chat_session.awaiting_input_json
                if isinstance(chat_session.awaiting_input_json, dict)
                else {}
            )
            awaiting_input = {
                "skill_id": source_skill_id,
                "step_id": source_step_id,
                "expected_fields": missing_fields,
                "question_summary": str(step_result.reply or "").strip() or None,
            }
            if previous.get("task_id"):
                awaiting_input["task_id"] = previous["task_id"]
            chat_session.awaiting_input_json = awaiting_input
            chat_session.last_agent_question = awaiting_input["question_summary"]
            return

        should_clear = bool(
            step_result.next_step_id
            or step_result.tool_call
            or step_result.is_step_completed
            or not missing_fields
        )
        awaiting = chat_session.awaiting_input_json
        if not should_clear or not isinstance(awaiting, dict):
            return
        if awaiting.get("skill_id") not in {None, source_skill_id}:
            return
        if awaiting.get("step_id") not in {None, source_step_id}:
            return
        task_id = awaiting.get("task_id")
        chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
        chat_session.last_agent_question = None

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
        return GraphRules.normalize_pending_steps(value)

    def _store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        slots = dict(chat_session.slots_json or {})
        normalized = GraphRules.normalize_pending_steps(pending_steps)
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
        sibling_steps = GraphRules.sibling_steps_from_edges(
            outgoing,
            selected_step_id,
            self._edge_condition,
        )
        if not sibling_steps:
            return
        pending_steps = self._graph_pending_steps(chat_session)
        for step_id in sibling_steps:
            if step_id not in pending_steps:
                pending_steps.append(step_id)
        self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def _edge_condition(self, edge: dict[str, Any]) -> str:
        return GraphRules.edge_condition(edge)

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
        return GraphRules.has_step(skill.content_json or {}, step_id)

    def _first_step_id(self, skill: Skill) -> str | None:
        content = skill.content_json or {}
        start_node_id = str(content.get("start_node_id") or "").strip()
        if start_node_id and self._skill_has_step(skill, start_node_id):
            return start_node_id
        steps = self._skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.steps_from_nodes(self._ordered_skill_nodes(skill))

    def _skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.nodes(skill.content_json or {})

    def _ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        content = skill.content_json or {}
        return GraphRules.ordered_nodes(
            content,
            nodes=self._skill_nodes(skill),
            outgoing=self._graph_outgoing_edges(skill),
        )

    def _graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        return GraphRules.outgoing_edges(skill.content_json or {})

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.default_next_step_from_parts(
            self._skill_nodes(skill),
            self._graph_outgoing_edges(skill).get(active_step_id, []),
        )

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
            self._bump_agent_usage(request.agent_id)
        elif not chat_session.agent_id and request.agent_id:
            chat_session.agent_id = request.agent_id
        return chat_session

    def _bump_agent_usage(self, agent_id: str | None) -> None:
        """数字员工「被使用」计数 +1(每新建一个会话算一次,站内与分享访客都计入)。

        公共实现见 `app.agents.usage.bump_agent_usage`（Public API 建会话也走它）。
        """
        bump_agent_usage(self.db, agent_id)

    def _current_skill_step(
        self, skill: Skill, active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.current_step_from_steps(self._skill_steps(skill), active_step_id)

    def _skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        return GraphRules.slot_satisfied(slots, field)

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
        return self._get_default_model(
            request.tenant_id, agent_id, role, user_id=request.user_id
        )

    def _get_default_model(
        self,
        tenant_id: str,
        agent_id: str | None = None,
        role: str = "default",
        user_id: str | None = None,
    ) -> ModelConfig | None:
        # 免费链路:员工被管理员标记 + 用户在该角色上没有自己的模型 → 用租户的全局免费模型
        # (配额在发送入口 chat_stream 已校验并消耗;这里只做解析,所有角色统一落到免费模型)。
        free_candidate = resolve_turn_default_model(self.db, tenant_id, agent_id, user_id, role)
        if free_candidate is not None:
            return resolve_model_config_for_runtime(self.db, tenant_id, free_candidate.id)
        return model_for_agent(self.db, tenant_id, agent_id, role, user_id=user_id)

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
        return visible_published_skills(self.db, tenant_id, agent_id)

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
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def _drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        skills_by_id = {skill.skill_id: skill for skill in skills}
        available_skill_ids = set(skills_by_id)
        changed = False
        removed_skill_ids: set[str] = set()
        repaired_steps: list[dict[str, str | None]] = []

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
        elif active_skill_id:
            active_skill = skills_by_id[active_skill_id]
            active_step_id = str(chat_session.active_step_id or "").strip()
            restored_step_id = self._first_step_id(active_skill)
            if (
                restored_step_id
                and active_step_id != restored_step_id
                and not self._skill_has_step(active_skill, active_step_id)
            ):
                chat_session.active_step_id = restored_step_id
                awaiting = (
                    chat_session.awaiting_input_json
                    if isinstance(chat_session.awaiting_input_json, dict)
                    else {}
                )
                task_id = awaiting.get("task_id")
                chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
                chat_session.last_agent_question = None
                repaired_steps.append(
                    {
                        "skill_id": active_skill_id,
                        "from_step_id": active_step_id,
                        "to_step_id": restored_step_id,
                    }
                )
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
                    {
                        "removed_skill_ids": sorted(removed_skill_ids),
                        "repaired_steps": repaired_steps,
                    },
                )
        return changed

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
        # 租户关掉「展示知识来源」后，正文里的 [1] 也一起清掉：只靠前端裁剪的话，
        # 老客户端 / 缓存里的旧包 / 分享页的第二端都会漏出悬空编号。
        if knowledge_citations_hidden(self.db, tenant_id):
            reply = strip_citation_markers(reply)
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
