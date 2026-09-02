"""DshTaskAgent: runs one TaskRequirement on DSH instead of the in-process action loop.

This is the engine seam. ``HarnessV2Engine._run_frame`` calls
``self.task_agent.run(requirement, model_config, invoke_tool, ...)`` and gets a
``TaskExecutionResult`` back. ``DshTaskAgent`` has the same signature and the
same return contract, so *everything else* — turn claim, planner, TaskFrame
store, leases, SOP CAS, handoff, memory capture, response generation — is the
legacy code path unchanged. Only the step execution swaps:

    legacy: HarnessTaskAgent  → LLM JSON action protocol → invoke_tool(...)
    dsh:    DshTaskAgent      → DSH AgentLoop (Node)      → mcp__staffdeck__* → CapabilityHost

The ``invoke_tool`` callable handed in by the engine is the legacy
``HarnessCapabilityInvoker.invoke``. We do **not** use it for dispatch (the
CapabilityHost owns PEP + ledger), but we keep its manifest semantics by
building the ActivationSlot from the same ``CompositionSnapshot``.

Turn shape on DSH:

1. pre_step hooks build the step prompt (persona + memory + SOP ExecutionSlice
   + TaskRequirement) — one user message to DSH.
2. DSH runs its loop; every tool call is an MCP call into ``CapabilityHost``.
3. The model ends by calling ``finish_task`` (captured on the ActivationSlot).
   If it stops without calling it, ``turn_stopping`` hooks may steer once; if
   it still does not, the assistant text becomes ``reply_fragment`` and the
   status is inferred (``awaiting_user`` when required slots are missing,
   otherwise ``completed``).
4. ``post_tool`` hooks collected receipts/citations; artifacts come from the
   sandbox facade's workspace discovery.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlmodel import Session

from app.core.cancellation import is_chat_turn_cancelled
from app.core.harness_agent import HarnessExecutionCancelled
from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.db.models import ModelConfig
from app.security.encryption import decrypt_secret
from staffdeck_dsh.bridge.capability_mcp import ActivationRegistry, CapabilityMcpServer
from staffdeck_dsh.bridge.worker import DshProcess, DshWorkerConfig
from staffdeck_dsh.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_dsh.composition.compiler import CompositionSnapshot
from staffdeck_dsh.contracts.hooks import HookContext
from staffdeck_dsh.contracts.invocation import InvocationContext
from staffdeck_dsh.contracts.security import SecurityContext
from staffdeck_dsh.events.relay import SessionEventRelay
from staffdeck_dsh.interactions.pipeline_host import InteractionPipelineHost, PipelineState
from staffdeck_dsh.security.profile import Guard

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, dict[str, Any]], None]


@dataclass
class DshTurnContext:
    """Everything the engine seam needs beyond the legacy ``run`` arguments."""

    db: Session
    snapshot: CompositionSnapshot
    security_context: SecurityContext
    guard: Guard
    tenant_id: str
    agent_id: str
    user_id: str
    session_id: str
    turn_id: str
    channel: str
    run_id: str
    task_frame_id: str
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    session_slots: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    attachments_text: str = ""
    client_turn_id: str | None = None
    # The engine assigns run_id after start_run(), i.e. after this context is built.
    run_id_provider: Callable[[], str] | None = None

    @property
    def current_run_id(self) -> str:
        if self.run_id_provider is not None:
            value = self.run_id_provider()
            if value:
                return value
        return self.run_id


@dataclass
class DshRuntime:
    """Process-wide singletons: the capability MCP server and the DSH worker config."""

    worker_config: DshWorkerConfig
    registry: ActivationRegistry = field(default_factory=ActivationRegistry)
    mcp: CapabilityMcpServer | None = None

    def start(self) -> None:
        if self.mcp is None:
            self.mcp = CapabilityMcpServer(self.registry)
            self.mcp.start()

    def stop(self) -> None:
        if self.mcp is not None:
            self.mcp.stop()
            self.mcp = None

    @property
    def mcp_url(self) -> str:
        assert self.mcp is not None, "DshRuntime not started"
        return self.mcp.url


def _model_route(model_config: ModelConfig) -> tuple[str, str | None, str | None]:
    """(model name, base_url, api_key) for the DSH deepseek-official adapter.

    DSH's adapter speaks OpenAI-compatible chat completions with DeepSeek
    extensions, which is exactly what the StaffDeck gateways serve, so any
    ``openai_compatible`` ModelConfig routes through it unchanged.
    """

    base = (model_config.base_url or "").rstrip("/")
    key = decrypt_secret(model_config.api_key_encrypted) if model_config.api_key_encrypted else None
    return model_config.model, base or None, key


def _step_prompt(requirement: TaskRequirement, state: PipelineState, decision_contexts: list[dict[str, Any]], attachments_text: str) -> str:
    parts: list[str] = []
    for c in decision_contexts:
        text = str(c.get("text") or "").strip()
        if text:
            parts.append(text)
    task = {
        "goal": requirement.goal,
        "requirements": requirement.requirements,
        "completion_criteria": requirement.completion_criteria,
        "required_slots": requirement.required_slots,
        "known_slots": requirement.known_slots,
        "allowed_transitions": requirement.allowed_transitions,
        "required_capability_names": requirement.required_capability_names,
        "out_of_scope": requirement.out_of_scope_task_intents,
        "prior_task_results": requirement.prior_task_results[-3:],
    }
    parts.append("# 本步骤任务\n" + json.dumps(task, ensure_ascii=False, indent=1))
    if attachments_text:
        parts.append("# 附件\n" + attachments_text)
    if requirement.source_user_message:
        parts.append("# 用户原话\n" + requirement.source_user_message)
    parts.append(
        "# 执行规则\n"
        "- 只用 mcp__staffdeck__* 工具获取信息或执行动作；不要臆造工具结果。\n"
        "- 知识检索结果带 [N] 标签，引用时保留标签。\n"
        "- 完成、需要用户补充信息、需要转人工或失败时，必须调用 mcp__staffdeck__finish_task 提交结果，然后停止。\n"
        "- reply_fragment 是直接给用户看的回复，用用户的语言写。"
    )
    return "\n\n".join(parts)


class DshTaskAgent:
    """Same call contract as ``app.core.harness_agent.HarnessTaskAgent.run``."""

    def __init__(self, runtime: DshRuntime, turn: DshTurnContext, *, pipeline: InteractionPipelineHost | None = None, trace_sink: TraceSink | None = None):
        self.runtime = runtime
        self.turn = turn
        if pipeline is None:
            from staffdeck_dsh.modules.registry import get_registry

            pipeline = InteractionPipelineHost(turn.snapshot.hooks, handlers=get_registry().hook_handlers(), trace=trace_sink)
        self.pipeline = pipeline
        self.trace_sink = trace_sink
        self._process: DshProcess | None = None
        self._activation_token: str | None = None
        self._host: CapabilityHost | None = None

    # -- engine seam ---------------------------------------------------------------

    def run(
        self,
        requirement: TaskRequirement,
        model_config: ModelConfig,
        invoke_tool: Callable[..., Any],   # legacy invoker; unused for dispatch, kept for signature parity
        *,
        max_actions: int = 6,
        trace_sink: TraceSink | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        image_payloads: list[Any] | None = None,
        step_deadline_monotonic: float | None = None,
        step_timeout_seconds: int | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        trace = trace_sink or self.trace_sink or (lambda *_: None)
        t = self.turn
        # The engine's ``is_cancelled`` closes over its ORM session and is only
        # safe on the engine thread. MCP callbacks arrive on worker threads, so
        # the host gets an id-based check backed by its own DB session; the
        # engine-thread check stays for the turn loop itself.
        cancelled = is_cancelled or (lambda: False)
        bind = t.db.get_bind()

        def cancelled_threadsafe() -> bool:
            with Session(bind) as fresh:
                for kind, tid in (("message", t.turn_id), ("client", t.client_turn_id)):
                    if tid and is_chat_turn_cancelled(t.session_id, tid, db=fresh, identity_kind=kind):  # type: ignore[arg-type]
                        return True
            return False
        step_id = str((requirement.sop_context or {}).get("step", {}).get("node_id") or (requirement.sop_context or {}).get("step", {}).get("step_id") or "") or None
        sop_id = str((requirement.sop_context or {}).get("skill_id") or "") or None

        slot = ActivationSlot(
            snapshot=t.snapshot,
            generation=t.generation,
            turn_id=t.turn_id,
            active_sop_id=sop_id,
            active_node_id=step_id,
            deadline_monotonic=step_deadline_monotonic,
            allowed_next_steps=frozenset(str(x.get("next_node_id")) for x in requirement.allowed_transitions if isinstance(x, dict) and x.get("next_node_id")),
        )
        fence = LifecycleFence(expected_generation=t.generation, is_cancelled=cancelled_threadsafe)
        run_id = t.current_run_id
        # Own session for the host: it is driven from MCP worker threads.
        host_db = Session(bind)
        host = CapabilityHost(
            db=host_db, guard=t.guard, security_context=t.security_context, slot=slot, fence=fence,
            model_config=model_config, trace=self._threadsafe_trace(trace), run_id=run_id,
        )
        self._host = host
        state = PipelineState(snapshot=t.snapshot, memory_context=list(t.memory_context), session_slots=dict(t.session_slots), active_sop_id=sop_id, active_node_id=step_id)

        def inv_ctx(trace_id: str) -> InvocationContext:
            return InvocationContext(
                tenant_id=t.tenant_id, agent_id=t.agent_id, user_id=t.user_id or "anonymous", session_id=t.session_id,
                turn_id=t.turn_id, channel=t.channel, task_frame_id=t.task_frame_id, step_id=step_id, run_id=run_id,
                snapshot_id=t.snapshot.snapshot_id, trace_id=trace_id,
            )

        activation = self.runtime.registry.register(host, inv_ctx)
        self._activation_token = activation.token
        started = time.monotonic()
        actions = 0
        try:
            # 1. pre_step
            ctx1 = HookContext(point="pre_step", tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload={"requirement": requirement.model_dump(mode="json")}, generation=t.generation)
            pre = self.pipeline.run("pre_step", ctx1, state)
            if pre.kind == "deny":
                return self._failed(requirement, "PRE_STEP_DENIED", pre.reason or "pre-step denied", actions=0)
            prompt = _step_prompt(requirement, state, list(pre.contexts), t.attachments_text)

            # 2. DSH process bound to this activation
            model, base_url, api_key = _model_route(model_config)
            cfg = DshWorkerConfig(
                dsh_root=self.runtime.worker_config.dsh_root, dsh_home=self.runtime.worker_config.dsh_home / t.tenant_id,
                node_bin=self.runtime.worker_config.node_bin, model=model, model_base_url=base_url, model_api_key=api_key,
                permission_mode=self.runtime.worker_config.permission_mode,
                initialize_timeout_seconds=self.runtime.worker_config.initialize_timeout_seconds,
                request_timeout_seconds=(step_timeout_seconds or self.runtime.worker_config.request_timeout_seconds),
            )
            workspace = host._workspace_root(inv_ctx("ws"))
            proc = DshProcess(cfg, activation_token=activation.token, mcp_url=self.runtime.mcp_url, cwd=workspace)
            self._process = proc
            proc.start()
            trace("dsh_process_started", {"model": model, "base_url": base_url, "workspace": str(workspace), "boot_ms": int((time.monotonic() - started) * 1000)})

            # 3. run turn (+ optional single steer)
            session_id = f"sd-{t.session_id}-{t.task_frame_id}"
            events, final_text, finish_reason = self._run_dsh_turn(proc, session_id, prompt, cancelled, trace)
            actions = sum(1 for e in events if e.get("type") == "tool/call")
            if slot.finish is None:
                ctx_stop = HookContext(point="turn_stopping", tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload={"final_text": final_text, "finish_reason": finish_reason}, generation=t.generation)
                stop = self.pipeline.run("turn_stopping", ctx_stop, state)
                if stop.kind == "steer" and stop.steer_message and not cancelled():
                    trace("dsh_turn_steered", {"reason": stop.reason, "message": stop.steer_message[:200]})
                    events2, final_text2, finish_reason = self._run_dsh_turn(proc, session_id, stop.steer_message + "\n\n完成后调用 mcp__staffdeck__finish_task。", cancelled, trace)
                    events.extend(events2)
                    actions += sum(1 for e in events2 if e.get("type") == "tool/call")
                    if final_text2.strip():
                        final_text = final_text2
                if stop.handoff and slot.finish is None:
                    slot.finish = {"status": "handoff", "reply_fragment": final_text, "slot_updates": {}, "next_step_id": None, "task_summary": "SOP 步骤请求转人工", "structured_result": None}

            # 4. assemble result
            artifacts = host.discover_artifacts(inv_ctx("artifacts"))
            return self._result(requirement, slot, state, final_text, finish_reason, actions, artifacts, events)
        except HarnessExecutionCancelled:
            raise
        except Exception as exc:
            if cancelled():
                raise HarnessExecutionCancelled("DSH turn cancelled") from exc
            logger.exception("DSH task agent failed")
            trace("dsh_turn_failed", {"error": str(exc)})
            return self._failed(requirement, "DSH_ENGINE_ERROR", str(exc), actions=actions)
        finally:
            slot.closed = True
            self.runtime.registry.release(activation.token)
            try:
                host_db.close()
            except Exception:  # pragma: no cover
                pass
            self._flush_trace(trace)
            if self._process is not None:
                try:
                    self._process.close()
                except Exception:  # pragma: no cover
                    pass
                self._process = None

    # -- helpers ------------------------------------------------------------------

    def _threadsafe_trace(self, trace: TraceSink) -> TraceSink:
        """Buffer trace events raised on MCP worker threads; ``_flush_trace`` replays them on the engine thread."""

        import threading

        buf: list[tuple[str, dict[str, Any]]] = []
        lock = threading.Lock()
        main = threading.get_ident()
        self._trace_buffer = (buf, lock)

        def sink(event: str, payload: dict[str, Any]) -> None:
            if threading.get_ident() == main:
                trace(event, payload)
                return
            with lock:
                buf.append((event, payload))

        return sink

    def _flush_trace(self, trace: TraceSink) -> None:
        pending = getattr(self, "_trace_buffer", None)
        if not pending:
            return
        buf, lock = pending
        with lock:
            items, buf[:] = list(buf), []
        for event, payload in items:
            try:
                trace(event, payload)
            except Exception:  # pragma: no cover
                logger.exception("trace flush failed for %s", event)

    def _run_dsh_turn(self, proc: DshProcess, session_id: str, text: str, cancelled: Callable[[], bool], trace: TraceSink) -> tuple[list[dict[str, Any]], str, str | None]:
        client = proc.client
        events: list[dict[str, Any]] = []
        relay = SessionEventRelay(self.turn.tenant_id, self.turn.session_id, trace)
        with client.subscribe_session_notifications(session_id) as sub:
            message_id = client.session_prompt(session_id, [{"type": "text", "text": text}], notification_subscription=sub)
            received = False
            while True:
                if cancelled():
                    raise HarnessExecutionCancelled("cancelled while DSH turn running")
                n = sub.next()
                payload = n.payload or {}
                if n.method == "session.event" and payload.get("sessionId") == session_id:
                    ev = payload.get("event")
                    if isinstance(ev, dict):
                        if not received:
                            if ev.get("type") == "agent/inbox/spliced" and any(isinstance(m, dict) and m.get("id") == message_id for m in ((ev.get("data") or {}).get("inserted") or [])):
                                received = True
                            continue
                        events.append(ev)
                        relay(ev)
                if n.method == "session.status" and payload.get("sessionId") == session_id and payload.get("status") == "idle" and received:
                    break
        from deepseek_harness.api import final_response, finish_reason as _finish_reason  # official SDK helpers
        return events, final_response(events), _finish_reason(events)

    def _result(self, requirement: TaskRequirement, slot: ActivationSlot, state: PipelineState, final_text: str, finish_reason: str | None, actions: int, artifacts: list[dict[str, Any]], events: list[dict[str, Any]]) -> TaskExecutionResult:
        fin = slot.finish
        if fin is None:
            merged = {**requirement.known_slots}
            missing = [f for f in requirement.required_slots if merged.get(f) in (None, "", [], {})]
            status = "awaiting_user" if missing else ("completed" if finish_reason in (None, "completed", "end_turn", "stop") else "failed")
            fin = {"status": status, "reply_fragment": final_text.strip(), "slot_updates": {}, "next_step_id": None, "task_summary": "DSH turn ended without finish_task", "structured_result": None}
        reply = str(fin.get("reply_fragment") or "").strip() or final_text.strip()
        # Mirror legacy _finish_result's handoff-node rule.
        step = (requirement.sop_context or {}).get("step") or {}
        if str(step.get("type") or "") == "handoff" and not fin.get("next_step_id"):
            fin["status"] = "handoff"
        return TaskExecutionResult(
            task_frame_id=requirement.task_frame_id,
            status=fin["status"],
            reply_fragment=reply,
            slot_updates=dict(fin.get("slot_updates") or {}),
            next_step_id=fin.get("next_step_id"),
            citations=self._citations(events, state),
            evidence_results=self._evidence(events),
            capability_results=[{"receipt": r} for r in state.receipts],
            artifacts=list(artifacts),
            task_summary=str(fin.get("task_summary") or ""),
            action_count=max(1, actions),
            structured_result=fin.get("structured_result"),
            loop_checkpoint={"version": 1, "engine": "dsh", "task_frame_id": requirement.task_frame_id, "snapshot_id": slot.snapshot.snapshot_id, "artifacts": list(artifacts)[-20:]},
        )

    @staticmethod
    def _tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pair DSH ``tool/call`` and ``tool/result`` events and decode the StaffDeck proxy payload.

        DSH shape (0.1.2): ``tool/call.data = {callId, name, arguments}``;
        ``tool/result.data.message.content[] = {type: "tool-result", toolCallId, content: [{type: "text", text}], isError}``.
        The proxy always returns one JSON text block.
        """

        names: dict[str, str] = {}
        for ev in events:
            if ev.get("type") == "tool/call":
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                if data.get("callId"):
                    names[str(data["callId"])] = str(data.get("name") or "")
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.get("type") != "tool/result":
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool-result":
                    continue
                call_id = str(block.get("toolCallId") or "")
                payload: Any = None
                for inner in block.get("content") or []:
                    if isinstance(inner, dict) and inner.get("type") == "text":
                        try:
                            payload = json.loads(inner.get("text") or "")
                        except Exception:
                            payload = None
                        break
                if isinstance(payload, dict):
                    out.append({"name": names.get(call_id, ""), "call_id": call_id, "is_error": bool(block.get("isError")), **payload})
        return out

    def _citations(self, events: list[dict[str, Any]], state: PipelineState) -> list[dict[str, Any]]:
        cites: list[dict[str, Any]] = []
        for r in self._tool_results(events):
            for c in r.get("citations") or []:
                if isinstance(c, dict):
                    cites.append(c)
        return cites or [c for c in state.citations if isinstance(c, dict)]

    def _evidence(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(r["data"]) for r in self._tool_results(events) if r.get("success") and str(r.get("name") or "").endswith("knowledge_search") and isinstance(r.get("data"), dict)]

    @staticmethod
    def _failed(requirement: TaskRequirement, code: str, message: str, *, actions: int) -> TaskExecutionResult:
        return TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="failed", reply_fragment=message, task_summary=f"DSH: {code}", action_count=max(1, actions), error={"code": code, "message": message})
