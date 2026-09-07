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
2. The Harness v3 agent loop runs with the logical general/SOP loop's context;
   business calls use ``CapabilityHost``, fixed controls use ``StepCompletionPort``.
3. Ordinary conversation returns native final output; SOP steps submit structured
   results through ``submit_step_result``. ``turn_stopping`` hooks supervise both
   paths, and v2's result normalizer retains the existing state semantics.
4. ``post_tool`` hooks collected receipts/citations; artifacts come from the
   sandbox facade's workspace discovery. Public history is checkpointed across
   user turns; cold workers reconstruct it without replaying old permissions.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from sqlmodel import Session
from pydantic import ValidationError

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
    module_registry: Any = None
    # Live streaming: forward the engine's assistant text deltas to ``stream_sink`` as they arrive.
    # Off when a supervision hook may refuse/rewrite the final text (the coordinator decides).
    live_stream: bool = False
    stream_sink: Any = None

    @property
    def current_run_id(self) -> str:
        if self.run_id_provider is not None:
            value = self.run_id_provider()
            if value:
                return value
        return self.run_id


@dataclass
class HarnessV3Runtime:
    """Process-wide singletons: the capability MCP server, the worker config and the warm process pool."""

    worker_config: HarnessV3WorkerConfig
    registry: ActivationRegistry = field(default_factory=ActivationRegistry)
    mcp: CapabilityMcpServer | None = None
    pool: Any = None

    def start(self) -> None:
        if self.mcp is None:
            self.mcp = CapabilityMcpServer(self.registry)
            self.mcp.start()
        if self.pool is None:
            from staffdeck_harness.bridge.process_pool import ProcessPool

            self.pool = ProcessPool()

    def stop(self) -> None:
        if self.pool is not None:
            self.pool.close_all(on_close=self.registry.release)
            self.pool = None
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

    # -- per-turn process checkout ---------------------------------------------------------

    def acquire_process(self, cfg: HarnessV3WorkerConfig, tenant_id: str, *, cwd: Path) -> Any:
        """A warm (or fresh) engine process for one turn. Its activation token is already registered
        (bound to an idle placeholder) so the engine can connect its MCP client at boot."""

        assert self.pool is not None, "HarnessV3Runtime not started"
        cfg = replace(cfg, model_api_key="")  # set below: the token is the API key
        holder: dict[str, str] = {}

        def register(token: str) -> None:
            holder["token"] = token
            self.registry.register(IdlePhaseHost(), None, token=token)

        inner = self.pool._factory

        def factory(c: HarnessV3WorkerConfig, **kw: Any) -> Any:
            # The token is the process's model-gateway API key; stamp it into the config the
            # (possibly test-injected) factory receives.
            return inner(replace(c, model_api_key=kw["activation_token"]), **kw)

        return self.pool.acquire(cfg, tenant_id, mcp_url=self.mcp_url, cwd=cwd, register=register, factory=factory, on_close=self.registry.release)

    def release_process(self, pooled: Any) -> None:
        if self.pool is None:
            pooled.process.close()
            self.registry.release(pooled.token)
            return
        # Park the activation on the idle placeholder while the process waits in the pool.
        try:
            self.registry.rebind(pooled.token, IdlePhaseHost(), None)
        except KeyError:
            pass
        self.pool.release(pooled, on_close=self.registry.release)


class IdlePhaseHost:
    """What a pooled process's token points at between phases: tools listed but refused, no model."""

    idle = True
    model_config = None
    trace = None

    def tool_schemas(self) -> list[dict[str, Any]]:
        from staffdeck_harness.bridge.control import all_tool_schemas

        return all_tool_schemas()

    def invoke_proxy(self, proxy_name: str, arguments: Any, ctx: Any) -> tuple[ModuleResult, None]:
        return ModuleResult.fail("ACTIVATION_FENCED", "当前没有正在执行的任务步骤，不能调用能力"), None


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


def _step_prompt(requirement: TaskRequirement, state: PipelineState, decision_contexts: list[dict[str, Any]], attachments_text: str, *, system_tools: set[str] | None = None) -> str:
    from staffdeck_harness.bridge.tool_guide import system_tool_guide

    parts: list[str] = [system_tool_guide(requirement.kind, system_tools)]
    for c in decision_contexts:
        text = str(c.get("text") or "").strip()
        if text:
            parts.append(text)
    task = {
        "current_time": requirement.current_time,
        "goal": requirement.goal,
        "requirements": requirement.requirements,
        "completion_criteria": requirement.completion_criteria,
        "required_slots": requirement.required_slots,
        "expected_slots": requirement.expected_slots,
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
        + ("- 这是 SOP 步骤：使用运行控制接口 mcp__staffdeck__submit_step_result 提交结构化结果，然后停止。reply_fragment 是给用户的回复。\n"
           if requirement.kind == "sop" else
           "- 这是普通对话：直接输出给用户的最终回复；需要补充信息时直接提问，不调用结束或步骤提交接口。\n")
    )
    if requirement.kind == "conversation":
        parts.append('如需表达等待用户、转人工、失败或槽位更新，可直接在最终输出返回 v2 结果报文：'
                     '{"action":"finish","status":"awaiting_user|handoff|failed|completed",'
                     '"reply_fragment":"给用户的回复","slot_updates":{}}。这不是工具调用。')
    if requirement.current_user_message:
        parts.append("# 本次用户输入\n" + requirement.current_user_message)
    return "\n\n".join(parts)


def _attachment_notice(image_payloads: list[Any]) -> str:
    """Images never enter the engine subprocess.

    The engine's DeepSeek chat adapter rejects image content unless the model is declared
    image-capable in *its* catalog and a Files API is reachable — neither is true behind the
    bridge's gateway. Turns that carry images are routed to Harness v2 by ``EngineHost`` before
    they get here; this notice only covers the defensive case where one still arrives.
    """

    n = sum(1 for p in image_payloads or [] if p is not None)
    if not n:
        return ""
    return f"（本轮有 {n} 张图片附件，Harness v3 引擎不读取图片内容；如需看图请使用 Harness v2 引擎。）"


class HarnessV3TaskAgent:
    """Same call contract as ``app.core.harness_agent.HarnessTaskAgent.run``."""

    def __init__(self, runtime: HarnessV3Runtime, turn: HarnessV3TurnContext, *, pipeline: InteractionPipelineHost | None = None, trace_sink: TraceSink | None = None, pooled: Any = None):
        self.runtime = runtime
        self.turn = turn
        # A turn-scoped pooled process (see HarnessV3Engine): when given, frames reuse it and the
        # agent only *rebinds* the activation; when absent (unit tests, legacy callers) the agent
        # checks one out itself and returns it in ``finally``.
        self._pooled = pooled
        self._owns_process = pooled is None
        if pipeline is None:
            from staffdeck_harness.modules.registry import peek_registry

            reg = peek_registry()
            pipeline = InteractionPipelineHost(turn.snapshot.hooks, handlers=reg.hook_handlers() if reg is not None else None, trace=trace_sink)
        self.pipeline = pipeline
        self.trace_sink = trace_sink
        self._process: HarnessV3Process | None = None
        self._activation_token: str | None = None
        self._host: CapabilityHost | None = None
        self._custom_pipeline = pipeline is not None and turn.module_registry is None

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
        base_trace = trace_sink or self.trace_sink or (lambda *_: None)
        t = self.turn
        from staffdeck_harness.runtime.execution_context import ExecutionContext
        from staffdeck_harness.bridge.control import ExecutionHost, ExecutionBudgetExceeded

        trace = self._client_stream_trace(base_trace, requirement)

        context = ExecutionContext.restore(requirement, checkpoint, tenant_id=t.tenant_id,
                                           agent_id=t.agent_id, session_id=t.session_id)
        self._step_deadline = step_deadline_monotonic
        self._native_result = None
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
                from dataclasses import asdict

                payload.update({"success": result.success, "data": result.data, "error": result.error, "receipt": asdict(host.current_receipt) if host.current_receipt else None, "citations": list(result.citations or ())})
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
        host.results = list(context.capability_results)
        host.citations = list(context.citations)
        host.evidence = list(context.evidence)
        restored_results = len(host.results)
        execution_host = ExecutionHost(host, requirement, max_actions=max_actions)
        self._execution_host = execution_host
        state = PipelineState(snapshot=t.snapshot, memory_context=list(t.memory_context), session_slots=dict(requirement.known_slots), active_sop_id=sop_id, active_node_id=step_id)
        if t.module_registry is not None:
            from staffdeck_harness.composition.compiler import compile_hooks

            reg = t.module_registry
            host.registry = reg
            self.pipeline = InteractionPipelineHost(
                compile_hooks(reg.hooks(t.snapshot, sop_id=sop_id)),
                handlers=reg.hook_handlers(t.snapshot, sop_id=sop_id), trace=self.trace_sink,
            )

        def inv_ctx(trace_id: str) -> InvocationContext:
            return InvocationContext(
                tenant_id=t.tenant_id, agent_id=t.agent_id, user_id=t.user_id or "anonymous", session_id=t.session_id,
                turn_id=t.turn_id, channel=t.channel, task_frame_id=t.task_frame_id, step_id=step_id, run_id=run_id,
                snapshot_id=t.snapshot.snapshot_id, trace_id=trace_id,
            )

        model = str(getattr(model_config, "model", "") or "")
        thinking, effort = _model_thinking(model_config)
        cfg = HarnessV3WorkerConfig(
            harness_v3_root=self.runtime.worker_config.harness_v3_root, harness_v3_home=self.runtime.worker_config.harness_v3_home / t.tenant_id,
            node_bin=self.runtime.worker_config.node_bin, model=model, model_base_url=self.runtime.model_base_url,
            thinking=thinking, reasoning_effort=effort,
            permission_mode=self.runtime.worker_config.permission_mode,
            initialize_timeout_seconds=self.runtime.worker_config.initialize_timeout_seconds,
            request_timeout_seconds=(step_timeout_seconds or self.runtime.worker_config.request_timeout_seconds),
        )
        workspace = host._workspace_root(inv_ctx("ws"))
        actions = 0
        pooled = self._pooled
        engine_session = ""

        def finish(result):
            result.capability_results = result.capability_results or list(host.results)
            result.citations = result.citations or list(host.citations)
            result.evidence_results = result.evidence_results or list(host.evidence)
            artifacts = [*context.artifacts, *result.artifacts, *host.discover_artifacts(inv_ctx("checkpoint-artifacts"))]
            result.artifacts = list({json.dumps(item, sort_keys=True, default=str): item for item in artifacts}.values())[-20:]
            result.loop_checkpoint = context.complete(pooled if engine_session else None, engine_session,
                                                      result, host.results[restored_results:])
            return result

        try:
            # 1. pre_step
            ctx1 = HookContext(point="pre_step", tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload={"requirement": requirement.model_dump(mode="json")}, generation=t.generation)
            pre = self.pipeline.run("pre_step", ctx1, state)
            if pre.kind == "deny":
                return finish(self._failed(requirement, "PRE_STEP_DENIED", pre.reason or "pre-step denied", actions=0))
            prompt = _step_prompt(requirement, state, list(pre.contexts), t.attachments_text,
                                  system_tools=execution_host.model_tool_names())
            notice = _attachment_notice(list(image_payloads or []) or list(t.image_payloads))
            content_blocks = [{"type": "text", "text": prompt + ("\n\n" + notice if notice else "")}]

            # 2. Engine process. Pooled per turn (boot ≈1s is paid once per warm slot, not per frame);
            #    the activation token *is* the process's model-gateway API key, so provider credentials
            #    never leave StaffDeck. This frame's CapabilityHost is bound to the token for the phase.
            if pooled is None:
                pooled = self.runtime.acquire_process(cfg, t.tenant_id, cwd=workspace)
                self._pooled = pooled
            self.runtime.registry.rebind(pooled.token, execution_host, inv_ctx)
            self._activation_token = pooled.token
            proc = pooled.process
            self._process = proc
            trace("harness_v3_frame_bound", {"model": model, "model_config_id": getattr(model_config, "id", None), "via": "staffdeck-model-gateway", "thinking": thinking or "provider_default", "reasoning_effort": effort or "provider_default", "workspace": str(workspace), "process_uses": pooled.uses})

            # 3. run turn (+ optional single steer)
            engine_session, recovery = context.prepare(pooled)
            if recovery:
                content_blocks.insert(0, {"type": "text", "text": recovery})
            trace("harness_v3_context_bound", {"execution_loop_id": context.logical_id,
                  "engine_session_id": engine_session, "restored": bool(recovery),
                  "history_entries": len(context.history), "loop_kind": requirement.kind})
            def stop_requested():
                return cancelled() or execution_host.recovery_blocked is not None or execution_host.exhausted

            events, final_text, finish_reason = self._run_engine_turn(proc, engine_session, content_blocks, stop_requested, trace)
            if execution_host.recovery_blocked:
                error = execution_host.recovery_blocked
                return finish(TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="awaiting_user",
                              reply_fragment=error["message"], error=error, action_count=execution_host.actions,
                              task_summary="能力调用未取得进展，暂停当前步骤"))
            if execution_host.exhausted:
                raise ExecutionBudgetExceeded("action budget exhausted")
            if requirement.kind == "conversation":
                from staffdeck_harness.runtime.completion import native_result

                self._native_result = native_result(final_text)
                if self._native_result is not None:
                    final_text = self._native_result["reply_fragment"]
            actions = sum(1 for e in events if e.get("type") == "tool/call")
            for supervision_attempt in range(2):
                final_text = str((slot.finish or {}).get("reply_fragment") or final_text)
                ctx_stop = HookContext(point="turn_stopping", tenant_id=t.tenant_id, agent_id=t.agent_id, session_id=t.session_id, turn_id=t.turn_id, step=1, snapshot_id=t.snapshot.snapshot_id, payload={"final_text": final_text, "finish_reason": finish_reason}, generation=t.generation)
                stop = self.pipeline.run("turn_stopping", ctx_stop, state)
                if stop.kind == "deny":
                    return finish(self._failed(requirement, "OUTPUT_DENIED", stop.reason or "output denied", actions=actions))
                if requirement.kind == "sop" and slot.finish is None and stop.kind == "pass":
                    if supervision_attempt == 1:
                        return finish(self._failed(requirement, "HARNESS_ACTION_INVALID", "SOP 步骤未提交有效的结构化结果。", actions=actions))
                    stop = HookDecision(kind="steer", steer_message="请使用 mcp__staffdeck__submit_step_result 提交当前步骤结果。")
                if stop.kind == "steer" and supervision_attempt == 1:
                    return finish(self._failed(requirement, "OUTPUT_DENIED", "output still requires revision after supervision", actions=actions))
                if stop.kind == "steer" and stop.steer_message and not cancelled():
                    slot.finish = None
                    slot.closed = False
                    trace("harness_v3_turn_steered", {"reason": stop.reason, "message": stop.steer_message[:200]})
                    suffix = "\n完成后提交 SOP 步骤结果。" if requirement.kind == "sop" else "\n请直接返回修正后的回复。"
                    events2, final_text2, finish_reason = self._run_engine_turn(proc, engine_session, [{"type": "text", "text": stop.steer_message + suffix}], stop_requested, trace)
                    events.extend(events2)
                    actions += sum(1 for e in events2 if e.get("type") == "tool/call")
                    if final_text2.strip():
                        final_text = final_text2
                        if requirement.kind == "conversation":
                            self._native_result = native_result(final_text)
                            if self._native_result is not None:
                                final_text = self._native_result["reply_fragment"]
                    continue
                if stop.handoff:
                    slot.finish = {"status": "handoff", "reply_fragment": final_text, "slot_updates": {}, "next_step_id": None, "task_summary": "SOP 步骤请求转人工", "structured_result": None}
                break

            # 4. assemble result
            artifacts = host.discover_artifacts(inv_ctx("artifacts"))
            actions = execution_host.actions + (0 if slot.finish is not None else 1)
            return finish(self._result(requirement, slot, state, final_text, finish_reason, actions, artifacts, events))
        except ExecutionBudgetExceeded:
            return finish(TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="action_budget",
                          reply_fragment="本次执行预算已用完，已保存执行上下文。", action_count=execution_host.actions,
                          task_summary="等待后续推进"))
        except TimeoutError as exc:
            return finish(self._failed(requirement, "HARNESS_STEP_TIMEOUT", str(exc), actions=execution_host.actions))
        except ValidationError as exc:
            return finish(self._failed(requirement, "HARNESS_ACTION_INVALID", str(exc), actions=execution_host.actions))
        except HarnessExecutionCancelled:
            if not cancelled() and execution_host.recovery_blocked:
                error = execution_host.recovery_blocked
                return finish(TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="awaiting_user",
                              reply_fragment=error["message"], error=error, action_count=execution_host.actions,
                              task_summary="能力调用未取得进展，暂停当前步骤"))
            if not cancelled() and execution_host.exhausted:
                return finish(TaskExecutionResult(task_frame_id=requirement.task_frame_id, status="action_budget",
                              reply_fragment="本次执行预算已用完，已保存执行上下文。", action_count=execution_host.actions))
            raise
        except Exception as exc:
            if cancelled():
                raise HarnessExecutionCancelled("Harness v3 turn cancelled") from exc
            logger.exception("Harness v3 task agent failed")
            trace("harness_v3_turn_failed", {"error": str(exc)})
            return finish(self._failed(requirement, "HARNESS_V3_ENGINE_ERROR", str(exc), actions=actions))
        finally:
            slot.closed = True
            if pooled is not None:
                try:
                    self.runtime.registry.rebind(pooled.token, IdlePhaseHost(), None)
                except KeyError:
                    pass
            try:
                host_db.close()
            except Exception:  # pragma: no cover
                pass
            self._flush_trace(trace)
            if pooled is not None and self._owns_process:
                self.runtime.release_process(pooled)
                self._pooled = None
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

    def _run_engine_turn(self, proc, session_id, content_blocks, cancelled, trace, *, finished=None):
        from staffdeck_harness.bridge.session_runner import run_session
        from staffdeck_harness.bridge.control import ExecutionBudgetExceeded
        import time

        def check():
            if cancelled():
                return True
            if self._execution_host.exhausted:
                raise ExecutionBudgetExceeded("action budget exhausted")
            if self._step_deadline is not None and time.monotonic() >= self._step_deadline:
                raise TimeoutError("SOP step deadline expired")
            return False

        return run_session(proc, session_id, content_blocks, check, trace,
                           tenant_id=self.turn.tenant_id, host_session_id=self.turn.session_id,
                           timeout_seconds=self.runtime.worker_config.request_timeout_seconds or 600)

    def _result(self, requirement: TaskRequirement, slot: ActivationSlot, state: PipelineState, final_text: str, finish_reason: str | None, actions: int, artifacts: list[dict[str, Any]], events: list[dict[str, Any]]) -> TaskExecutionResult:
        from app.core.harness_agent import HarnessAction, finish_execution_result
        from staffdeck_harness.runtime.completion import native_result

        fin = slot.finish or (getattr(self, "_native_result", None) or native_result(final_text) if requirement.kind == "conversation" else None)
        if fin is None:
            if requirement.kind == "sop" or not final_text.strip():
                return self._failed(requirement, "HARNESS_ACTION_INVALID", "缺少有效的执行结果。", actions=actions)
            merged = {**requirement.known_slots}
            missing = [f for f in requirement.required_slots if merged.get(f) in (None, "", [], {})]
            status = "awaiting_user" if missing else (requirement.default_result_status if finish_reason in (None, "completed", "end_turn", "stop") else "failed")
            fin = {"status": status, "reply_fragment": final_text.strip(), "slot_updates": {}, "next_step_id": None, "task_summary": "Harness v3 turn ended without finish_task", "structured_result": None}
        reply = str(fin.get("reply_fragment") or "").strip() or final_text.strip()
        host = self._host
        results = list(host.results) if host is not None else []
        if fin.get("status") in {None, "completed"}:
            succeeded = {r.get("tool_name") for r in results if r.get("success")}
            missing = [name for name in requirement.required_capability_names if name not in succeeded]
            kb_ids = {str(c.get("knowledge_base_id")) for c in (host.citations if host else []) if c.get("knowledge_base_id")}
            for evidence in host.evidence if host else []:
                for item in [*(evidence.get("chunks") or []), *(evidence.get("evidence_pack") or [])]:
                    if isinstance(item, dict) and item.get("knowledge_base_id"):
                        kb_ids.add(str(item["knowledge_base_id"]))
            missing.extend(f"knowledge_search:{rid}" for rid in requirement.required_knowledge_base_ids if rid not in kb_ids)
            if missing:
                return self._failed(requirement, "REQUIRED_CAPABILITY_MISSING", "未成功完成必需能力：" + "、".join(missing), actions=actions)
        action = HarnessAction.model_validate({**fin, "action": "finish", "reply_fragment": reply})
        result = finish_execution_result(requirement, action,
            list(host.citations) if host is not None else self._citations(events, state),
            list(host.evidence) if host is not None else self._evidence(events),
            results, list(artifacts), action_count=max(1, actions))
        streamed = "".join(getattr(self, "_streamed_parts", []) or [])
        if streamed:
            result.streamed_reply = streamed
        return result

    def _client_stream_trace(self, base_trace: TraceSink, requirement: TaskRequirement) -> TraceSink:
        """Wrap the audit trace so engine text deltas become a *client* stream.

        Assistant text deltas relayed from the engine are forwarded to the stream sink as they
        arrive (ordinary conversation, unsupervised) or dropped; they are never persisted as
        public stream_delta audit rows here. The coordinator reconciles the final reply using
        append/replace semantics, and never appends a second full answer after a rewrite.
        """

        t = self.turn
        self._streamed_parts: list[str] = []
        live = bool(getattr(t, "live_stream", False) and getattr(t, "stream_sink", None) is not None and requirement.kind == "conversation")

        from app.core.reply_stream import PublicTextProjection

        def emit(text: str) -> None:
            try:
                t.stream_sink.on_delta(text)
                self._streamed_parts.append(text)
            except Exception:  # a failing transport must not fail execution
                logger.exception("stream sink rejected a delta")

        projection = PublicTextProjection(emit)

        def trace(event: str, payload: dict[str, Any]) -> None:
            if event == "model_text_delta":
                text = str(payload.get("content") or "")
                if live and text:
                    projection.feed(text)
                return
            base_trace(event, payload)

        return trace

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
