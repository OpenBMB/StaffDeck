"""HarnessV3TaskAgent: runs one TaskRequirement on the Harness v3 engine instead of the in-process action loop.

This is the engine seam. ``HarnessV2Engine._run_frame`` calls
``self.task_agent.run(requirement, model_config, invoke_tool, ...)`` and gets a
``TaskExecutionResult`` back. ``HarnessV3TaskAgent`` has the same signature and the
same return contract, so *everything else* — turn claim, planner, TaskFrame
store, leases, SOP CAS, handoff, memory capture, response generation — is the
legacy code path unchanged. Only the step execution swaps:

    legacy: HarnessTaskAgent  → LLM JSON action protocol → invoke_tool(...)
    v3:     HarnessV3TaskAgent      → Harness v3 AgentLoop (Node)      → mcp__staffdeck__* → CapabilityHost

The ``invoke_tool`` callable handed in by the engine is the legacy
``HarnessCapabilityInvoker.invoke``. We do **not** use it for dispatch (the
CapabilityHost owns PEP + ledger), but we keep its manifest semantics by
building the ActivationSlot from the same ``CompositionSnapshot``.

Turn shape on Harness v3:

1. pre_step hooks build the step prompt (persona + memory + SOP ExecutionSlice
   + TaskRequirement) — one user message to the engine.
2. The Harness v3 agent loop runs; every tool call is an MCP call into ``CapabilityHost``.
3. The model ends by calling ``finish_task`` (captured on the ActivationSlot).
   If it stops without calling it, ``turn_stopping`` hooks may steer once; if
   it still does not, the assistant text becomes ``reply_fragment`` and the
   status is inferred (``awaiting_user`` when required slots are missing,
   otherwise ``completed``).
4. ``post_tool`` hooks collected receipts/citations; artifacts come from the
   sandbox facade's workspace discovery.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

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
from staffdeck_harness.bridge.capability_mcp import ActivationRegistry, CapabilityMcpServer
from staffdeck_harness.bridge.worker import HarnessV3Process, HarnessV3WorkerConfig
from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
from staffdeck_harness.composition.compiler import CompositionSnapshot
from staffdeck_harness.contracts.hooks import HookContext, HookDecision
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.events.relay import SessionEventRelay
from staffdeck_harness.interactions.pipeline_host import InteractionPipelineHost, PipelineState
from staffdeck_harness.security.profile import Guard

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, dict[str, Any]], None]


@dataclass
class HarnessV3TurnContext:
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
    # Validated vision payloads (data URLs) for this turn, threaded from the legacy _run_frame path.
    image_payloads: list[Any] = field(default_factory=list)
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
class HarnessV3Runtime:
    """Process-wide singletons: the capability MCP server and the Harness v3 worker config."""

    worker_config: HarnessV3WorkerConfig
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
        assert self.mcp is not None, "HarnessV3Runtime not started"
        return self.mcp.url

    @property
    def model_base_url(self) -> str:
        assert self.mcp is not None, "HarnessV3Runtime not started"
        return self.mcp.model_base_url


_EFFORT_ALIASES = {"minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "max", "max": "max", "off": "off", "none": "off"}


def _model_thinking(model_config: ModelConfig) -> tuple[str, str]:
    """(thinking, reasoning_effort) for the Harness v3 ``llm-deepseek`` row, read from the same ModelConfig
    fields the legacy LLM client honours (``extra_body.thinking.type``, ``extra_body.reasoning_effort``).

    A model configured with thinking disabled must not be sent any effort (the engine would default to
    ``high`` and gateways reject it); an unset config leaves both at the provider default.
    """

    from app.llm.client import _merge_extra_body, _normalize_extra_body, _thinking_mode_from_extra_body

    # Same precedence as app.llm.client.LLMClient: legacy extra_body (or the ORM column) merged with
    # the protocol options of the resolved snapshot.
    legacy = getattr(model_config, "legacy_extra_body", None) or getattr(model_config, "extra_body_json", None)
    body = _merge_extra_body(_normalize_extra_body(legacy), getattr(model_config, "protocol_options", None))
    mode = _thinking_mode_from_extra_body(body)  # "enabled" | "disabled" | ""
    thinking_block = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    effort_raw = body.get("reasoning_effort") or thinking_block.get("effort")
    effort = _EFFORT_ALIASES.get(str(effort_raw or "").lower(), "")
    if mode == "disabled":
        return "disabled", "off"
    if mode == "enabled":
        return "enabled", effort
    return "", effort


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
    elif requirement.attachments:
        # Fall back to the safe descriptors (never the data URLs) when the engine seam did not
        # precompute a summary — filename/kind/size/sandbox path are enough for the model.
        summary: dict[str, Any] = {}
        for d in requirement.attachments:
            if not isinstance(d, dict):
                continue
            key = str(d.get("attachment_id") or d.get("filename") or d.get("id") or "")
            summary[key] = {k: d.get(k) for k in ("filename", "kind", "content_type", "size", "workspace_relative_path", "sandbox_path", "preview", "note") if d.get(k) not in (None, "")}
        if summary:
            parts.append("# 附件\n" + json.dumps(summary, ensure_ascii=False, indent=1))
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


def _data_url_to_image_block(data_url: str) -> dict[str, Any] | None:
    """Convert an ``data:image/<mime>;base64,<payload>`` data URL to the SDK's image content block.

    The SDK prompt accepts ``SdkEncodedImageBlock {type: "image", data, mimeType}`` and the server
    converts it to a durable reference before enqueue; text passes through unchanged.
    """

    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep or not payload:
        return None
    mime = header[5:].split(";", 1)[0] or "image/png"
    encoded = payload.split(";base64,")[-1] if ";base64," in header else payload
    if not encoded:
        return None
    return {"type": "image", "data": encoded, "mimeType": mime}


def _image_content_blocks(image_payloads: list[Any]) -> list[dict[str, Any]]:
    """Extract SDK image content blocks from validated vision payloads (objects or dicts)."""

    blocks: list[dict[str, Any]] = []
    for p in image_payloads or []:
        if p is None:
            continue
        data_url = getattr(p, "data_url", None) or (p.get("data_url") if isinstance(p, dict) else None)
        if not data_url:
            continue
        blk = _data_url_to_image_block(str(data_url))
        if blk is not None:
            blocks.append(blk)
    return blocks


class HarnessV3TaskAgent:
    """Same call contract as ``app.core.harness_agent.HarnessTaskAgent.run``."""

    def __init__(self, runtime: HarnessV3Runtime, turn: HarnessV3TurnContext, *, pipeline: InteractionPipelineHost | None = None, trace_sink: TraceSink | None = None):
        self.runtime = runtime
        self.turn = turn
        if pipeline is None:
            from staffdeck_harness.modules.registry import peek_registry

            reg = peek_registry()
            pipeline = InteractionPipelineHost(turn.snapshot.hooks, handlers=reg.hook_handlers() if reg is not None else None, trace=trace_sink)
        self.pipeline = pipeline
        self.trace_sink = trace_sink
        self._process: HarnessV3Process | None = None
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
            session_id=self.turn.session_id,
            active_sop_id=sop_id,
            active_node_id=step_id,
            deadline_monotonic=step_deadline_monotonic,
            allowed_next_steps=frozenset(str(x.get("next_node_id")) for x in requirement.allowed_transitions if isinstance(x, dict) and x.get("next_node_id")),
        )
        fence = LifecycleFence(expected_generation=t.generation, is_cancelled=cancelled_threadsafe)
        run_id = t.current_run_id
        # Own session for the host: it is driven from MCP worker threads.
        host_db = Session(bind)

        def hooks(point: str, inv: ModuleInvocation, result: ModuleResult | None) -> HookDecision:
            """Bridge CapabilityHost's per-call hook point to the InteractionPipelineHost.

            pre_tool receives the invocation before it touches the ledger; post_tool receives the
            finished result so ``ledger.record`` / ``citations.collect`` can gather it. State is the
            turn's PipelineState; a lock guards the few mutations a concurrent MCP call could make.
            """
            payload: dict[str, Any] = {
                "name": str((inv.metadata or {}).get("proxy_name") or inv.operation),
                "operation": inv.operation,
                "arguments": dict(inv.arguments),
                "binding_id": inv.binding_id,
            }
            if result is not None:
                payload.update({"success": result.success, "error": result.error, "receipt": None, "citations": list(result.citations or ())})
            ctx = HookContext(point=point, tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload=payload, generation=t.generation)
            with hook_lock:
                return self.pipeline.run(point, ctx, state)

        host = CapabilityHost(
            db=host_db, guard=t.guard, security_context=t.security_context, slot=slot, fence=fence,
            model_config=model_config, trace=self._threadsafe_trace(trace), run_id=run_id,
        )
        hook_lock = threading.Lock()
        host.hooks = hooks
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
            content_blocks = self._content_blocks(prompt, image_payloads or [] or t.image_payloads)

            # 2. Harness v3 process bound to this activation. Model traffic goes through the bridge's
            #    gateway (StaffDeck's own model module runs the ModelConfig); the activation token
            #    doubles as the subprocess's API key, so provider credentials never leave StaffDeck.
            model = str(getattr(model_config, "model", "") or "")
            thinking, effort = _model_thinking(model_config)
            cfg = HarnessV3WorkerConfig(
                harness_v3_root=self.runtime.worker_config.harness_v3_root, harness_v3_home=self.runtime.worker_config.harness_v3_home / t.tenant_id,
                node_bin=self.runtime.worker_config.node_bin, model=model, model_base_url=self.runtime.model_base_url, model_api_key=activation.token,
                thinking=thinking, reasoning_effort=effort,
                permission_mode=self.runtime.worker_config.permission_mode,
                initialize_timeout_seconds=self.runtime.worker_config.initialize_timeout_seconds,
                request_timeout_seconds=(step_timeout_seconds or self.runtime.worker_config.request_timeout_seconds),
            )
            workspace = host._workspace_root(inv_ctx("ws"))
            proc = HarnessV3Process(cfg, activation_token=activation.token, mcp_url=self.runtime.mcp_url, cwd=workspace)
            self._process = proc
            proc.start()
            trace("harness_v3_process_started", {"model": model, "model_config_id": getattr(model_config, "id", None), "base_url": str(getattr(model_config, "base_url", "") or ""), "via": "staffdeck-model-gateway", "thinking": thinking or "provider_default", "reasoning_effort": effort or "provider_default", "workspace": str(workspace), "boot_ms": int((time.monotonic() - started) * 1000)})

            # 3. run turn (+ optional single steer)
            session_id = f"sd-{t.session_id}-{t.task_frame_id}"
            events, final_text, finish_reason = self._run_engine_turn(proc, session_id, content_blocks, cancelled, trace, finished=lambda: slot.finish is not None)
            actions = sum(1 for e in events if e.get("type") == "tool/call")
            if slot.finish is None:
                ctx_stop = HookContext(point="turn_stopping", tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload={"final_text": final_text, "finish_reason": finish_reason}, generation=t.generation)
                stop = self.pipeline.run("turn_stopping", ctx_stop, state)
                if stop.kind == "steer" and stop.steer_message and not cancelled():
                    trace("harness_v3_turn_steered", {"reason": stop.reason, "message": stop.steer_message[:200]})
                    events2, final_text2, finish_reason = self._run_engine_turn(proc, session_id, [{"type": "text", "text": stop.steer_message + "\n\n完成后调用 mcp__staffdeck__finish_task。"}], cancelled, trace, finished=lambda: slot.finish is not None)
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
                raise HarnessExecutionCancelled("Harness v3 turn cancelled") from exc
            logger.exception("Harness v3 task agent failed")
            trace("harness_v3_turn_failed", {"error": str(exc)})
            return self._failed(requirement, "HARNESS_V3_ENGINE_ERROR", str(exc), actions=actions)
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
            # Persisted later by the engine thread; keep the real time so logs stay in order.
            stamped = {**payload, "occurred_at": datetime.now(timezone.utc).isoformat()}
            with lock:
                buf.append((event, stamped))

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

    def _content_blocks(self, prompt: str, image_payloads: list[Any]) -> list[dict[str, Any]]:
        """The prompt's content blocks: the text plus any validated image attachments."""

        blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        blocks.extend(_image_content_blocks(image_payloads))
        return blocks

    def _run_engine_turn(self, proc: HarnessV3Process, session_id: str, content_blocks: list[dict[str, Any]], cancelled: Callable[[], bool], trace: TraceSink, *, finished: Callable[[], bool] | None = None) -> tuple[list[dict[str, Any]], str, str | None]:
        client = proc.client
        events: list[dict[str, Any]] = []
        relay = SessionEventRelay(self.turn.tenant_id, self.turn.session_id, trace)
        with client.subscribe_session_notifications(session_id) as sub:
            message_id = client.session_prompt(session_id, content_blocks, notification_subscription=sub)
            received = False
            while True:
                if cancelled():
                    raise HarnessExecutionCancelled("cancelled while Harness v3 turn running")
                # The Harness v3 (0.1.2) protocol has no cancel method and ``NotificationSubscription.next``
                # blocks indefinitely, so we poll with a small timeout: it lets us honour an
                # up-to-now-cancelled turn and break as soon as ``finish_task`` closed the slot
                # (a finished step is a closed step; the engine must not keep generating).
                n = self._next_poll(sub)
                if n is None:
                    if received and finished is not None and finished():
                        break
                    continue
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
                        if received and finished is not None and finished():
                            break
                if n.method == "session.status" and payload.get("sessionId") == session_id and payload.get("status") == "idle" and received:
                    break
        from deepseek_harness.api import final_response, finish_reason as _finish_reason  # official SDK helpers
        return events, final_response(events), _finish_reason(events)

    @staticmethod
    def _next_poll(sub: Any, *, timeout: float = 0.25) -> Any:
        """``NotificationSubscription.next()`` with a small timeout.

        Falls back to the blocking ``next()`` if the queue attribute the SDK uses is not present
        (e.g. a newer SDK changes its internals) — the feature degrades to no early-exit, never a
        crash.
        """

        q = getattr(sub, "_notifications", None)
        if q is None:
            return sub.next()
        try:
            return q.get(timeout=timeout)
        except Exception:
            return None

    def _result(self, requirement: TaskRequirement, slot: ActivationSlot, state: PipelineState, final_text: str, finish_reason: str | None, actions: int, artifacts: list[dict[str, Any]], events: list[dict[str, Any]]) -> TaskExecutionResult:
        fin = slot.finish
        if fin is None:
            merged = {**requirement.known_slots}
            missing = [f for f in requirement.required_slots if merged.get(f) in (None, "", [], {})]
            status = "awaiting_user" if missing else ("completed" if finish_reason in (None, "completed", "end_turn", "stop") else "failed")
            fin = {"status": status, "reply_fragment": final_text.strip(), "slot_updates": {}, "next_step_id": None, "task_summary": "Harness v3 turn ended without finish_task", "structured_result": None}
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
            loop_checkpoint={"version": 1, "engine": "harness_v3", "task_frame_id": requirement.task_frame_id, "snapshot_id": slot.snapshot.snapshot_id, "artifacts": list(artifacts)[-20:]},
        )

    @staticmethod
    def _tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pair Harness v3 ``tool/call`` and ``tool/result`` events and decode the StaffDeck proxy payload.

        Harness v3 event shape (0.1.2): ``tool/call.data = {callId, name, arguments}``;
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
        host = getattr(self, "_host", None)
        if host is not None and host.citations:
            return [dict(c) for c in host.citations]
        cites: list[dict[str, Any]] = []
        for r in self._tool_results(events):
            for c in r.get("citations") or []:
                if isinstance(c, dict):
                    cites.append(c)
        return cites or [c for c in state.citations if isinstance(c, dict)]

    def _evidence(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        host = getattr(self, "_host", None)
        if host is not None and host.evidence:
            return [dict(e) for e in host.evidence]
        return [dict(r["data"]) for r in self._tool_results(events) if r.get("success") and str(r.get("name") or "").endswith("knowledge_search") and isinstance(r.get("data"), dict)]

    @staticmethod
    def _failed(requirement: TaskRequirement, code: str, message: str, *, actions: int) -> TaskExecutionResult:
        return TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="failed", reply_fragment=message, task_summary=f"Harness v3: {code}", action_count=max(1, actions), error={"code": code, "message": message})
