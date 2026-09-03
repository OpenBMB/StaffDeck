"""CapabilityHost: the single door through which DSH (or anything else) reaches a capability.

Order of checks for every ``ModuleInvocation`` (matches the architecture's
"Snapshot Guard is not a PEP" rule):

1. **Activation fence** — the operation/resource must be in the frozen
   ``CompositionSnapshot`` for this turn and the generation must match. This
   only *narrows*; it never grants.
2. **Ledger replay-or-block** — a prior ``completed`` side effect with the same
   key is replayed; an ``outcome_unknown`` one blocks.
3. **Facade** — the Guarded Facade runs the PEP against the live row (so a
   revoked resource that is still model-visible is refused here) and executes
   through the legacy service.
4. **Ledger finish** — ``completed`` / ``failed`` / ``outcome_unknown`` /
   ``denied`` / ``cancelled`` and a ``Receipt``.

The host also exposes ``tool_schemas()``: the stable proxy schemas the Bridge
registers in DSH. The proxy never executes — every call comes back here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlmodel import Session

from app.agents.branching import get_agent
from app.db.models import GeneralSkill, KnowledgeBase, MCPServer, ModelConfig, Tool
from staffdeck_harness.capabilities.facade import FacadeDeps, SandboxFacade, workspace_for
from staffdeck_harness.capabilities.ledger import InvocationLedger, _Replayed
from staffdeck_harness.composition.compiler import CapabilityGrant, CompositionSnapshot
from staffdeck_harness.contracts.errors import ActivationFenced, AuthorizationUnavailable, ModuleSdkError, OutcomeUnknown, PermissionDenied
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult, Receipt
from staffdeck_harness.contracts.security import ResourceRef, SecurityContext
from staffdeck_harness.security.profile import Guard

logger = logging.getLogger(__name__)

SIDE_EFFECTING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Stable model-facing proxy tools. Names are wire-stable; DSH sees only these.
PROXY_TOOLS: dict[str, dict[str, Any]] = {
    "knowledge_search": {
        "operation": "knowledge.search/v1",
        "description": "在当前员工被授权的知识库中检索。返回带引用编号的证据片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题"},
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}, "description": "限定知识库（可选）"},
                "max_chunks": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["query"],
        },
    },
    "general_skill_read": {
        "operation": "general_skill.consume/v1",
        "description": "加载一个通用技能包（SKILL.md 与附件）的说明到当前上下文。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "query": {"type": "string", "description": "你打算用该技能解决的问题"},
            },
            "required": ["skill_id", "query"],
        },
    },
    "tool_invoke": {
        "operation": "tool.invoke/v1",
        "description": "调用一个已授权的业务工具（HTTP/MCP/A2A）。arguments 必须符合该工具的 input_schema。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["tool_id", "arguments"],
        },
    },
    "sandbox_execute": {
        "operation": "sandbox.execute/v1",
        "description": "在受控工作区内执行文件或命令工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "受控工具名（如 read_file/write_file/exec_command）"},
                "arguments": {"type": "object"},
            },
            "required": ["tool", "arguments"],
        },
    },
    "capability_describe": {
        "operation": "capability.describe/v1",
        "description": "列出当前可用的知识库、技能与工具及其 schema。",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["knowledge_base", "general_skill", "tool", "all"]}}},
    },
    "finish_task": {
        "operation": "task.finish/v1",
        "description": (
            "结束当前任务步骤并向 StaffDeck 提交结构化结果。完成本步骤、需要用户补充信息、"
            "需要转人工或任务失败时都必须调用一次本工具，然后停止。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "awaiting_user", "handoff", "failed"]},
                "reply_fragment": {"type": "string", "description": "给用户的回复正文（可含 [N] 引用）"},
                "slot_updates": {"type": "object", "description": "本步骤收集到的槽位值"},
                "next_step_id": {"type": "string", "description": "SOP 下一步 node_id（仅在允许的转移中选择）"},
                "task_summary": {"type": "string", "description": "一句话总结本步骤做了什么"},
                "structured_result": {"description": "可选的结构化结果"},
            },
            "required": ["status", "reply_fragment"],
        },
    },
}


@dataclass
class ActivationSlot:
    """The immutable per-turn activation: snapshot + generation + active SOP position."""

    snapshot: CompositionSnapshot
    generation: int
    turn_id: str
    session_id: str | None = None
    active_sop_id: str | None = None
    active_node_id: str | None = None
    deadline_monotonic: float | None = None
    closed: bool = False
    # The model's terminal envelope for this step, captured by ``finish_task``.
    finish: dict[str, Any] | None = None
    allowed_next_steps: frozenset[str] = frozenset()

    def grants(self) -> tuple[CapabilityGrant, ...]:
        return self.snapshot.grants_for(sop_id=self.active_sop_id, node_id=self.active_node_id)

    def allowed(self) -> dict[str, set[str]]:
        return self.snapshot.allowed_resource_ids(sop_id=self.active_sop_id, node_id=self.active_node_id)

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())


@dataclass
class LifecycleFence:
    """Local monotonic narrowing: idle/generation/deadline/cancel. Not a PEP."""

    expected_generation: int
    is_cancelled: Callable[[], bool] = lambda: False

    def check(self, slot: ActivationSlot) -> None:
        if slot.closed:
            raise ActivationFenced("turn is closed")
        if slot.generation != self.expected_generation:
            raise ActivationFenced(f"stale generation {slot.generation} != {self.expected_generation}")
        if self.is_cancelled():
            raise ActivationFenced("turn cancelled")
        remaining = slot.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ActivationFenced("step deadline expired")


@dataclass
class CapabilityHost:
    db: Session
    guard: Guard
    security_context: SecurityContext
    slot: ActivationSlot
    fence: LifecycleFence
    model_config: ModelConfig | None = None
    trace: Callable[[str, dict[str, Any]], None] | None = None
    run_id: str | None = None
    _ledger: InvocationLedger = field(init=False)
    _sandbox: SandboxFacade | None = field(init=False, default=None)
    _workspace: Path | None = field(init=False, default=None)
    # Collected on the host (worker threads) so the turn result never depends on
    # re-parsing the engine's transcript, which may have spilled/previewed large results.
    citations: list[dict[str, Any]] = field(init=False, default_factory=list)
    evidence: list[dict[str, Any]] = field(init=False, default_factory=list)
    # Optional hook point for ``pre_tool``/``post_tool`` decisions, wired by the task agent to
    # its InteractionPipelineHost. When unset, the host runs without a hook plan (unit tests).
    hooks: Callable[[str, ModuleInvocation, ModuleResult | None], HookDecision] | None = None
    _hook_trace: Callable[[str, dict[str, Any]], None] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._ledger = InvocationLedger(self.db)
        self._agent_row = get_agent(self.db, self.slot.snapshot.tenant_id, None if self.slot.snapshot.staff_id.endswith(":overall") else self.slot.snapshot.staff_id)

    # -- schemas the Bridge registers in DSH -----------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        allowed = self.slot.allowed()
        out: list[dict[str, Any]] = []
        for name, spec in PROXY_TOOLS.items():
            op = spec["operation"]
            if op == "knowledge.search/v1" and not allowed.get("knowledge_base"):
                continue
            if op == "general_skill.consume/v1" and not allowed.get("general_skill"):
                continue
            if op == "tool.invoke/v1" and not allowed.get("tool"):
                continue
            if op == "sandbox.execute/v1" and not self.slot.snapshot.session_policy.get("sandbox_enabled") and not allowed.get("tool"):
                # sandbox is always available to a Staff for file work; keep it.
                pass
            out.append({"name": name, "description": spec["description"], "parameters": spec["parameters"]})
        return out

    def describe(self, kind: str = "all") -> dict[str, Any]:
        grants = self.slot.grants()
        items: list[dict[str, Any]] = []
        for g in grants:
            if kind != "all" and g.resource_type != kind:
                continue
            item: dict[str, Any] = {"kind": g.resource_type, "id": g.resource_id, "name": g.name, "scope": g.scope, "operation": g.operation}
            if g.resource_type == "tool":
                row = self.db.get(Tool, g.resource_id)
                if row is not None:
                    item.update({"description": row.description, "tool_type": row.tool_type, "input_schema": dict(row.input_schema or {})})
            elif g.resource_type == "general_skill":
                row = self.db.get(GeneralSkill, g.resource_id)
                if row is not None:
                    item.update({"slug": row.slug, "description": (row.metadata_json or {}).get("description")})
            items.append(item)
        if self._sandbox is not None and kind in {"all", "sandbox"}:
            items.extend({"kind": "sandbox", "id": s["name"], "name": s["name"], "description": s["description"], "input_schema": s["input_schema"]} for s in self._sandbox.schemas())
        return {"items": items, "snapshot_id": self.slot.snapshot.snapshot_id}

    # -- the funnel -------------------------------------------------------------

    def invoke_proxy(self, proxy_name: str, arguments: Mapping[str, Any], ctx: InvocationContext) -> tuple[ModuleResult, Receipt | None]:
        spec = PROXY_TOOLS.get(proxy_name)
        if spec is None:
            return ModuleResult.fail("TOOL_NOT_AVAILABLE", f"未知能力代理 {proxy_name}"), None
        op = spec["operation"]
        if op == "capability.describe/v1":
            return ModuleResult.ok(self.describe(str(arguments.get("kind") or "all"))), None
        if op == "task.finish/v1":
            return self._finish_task(dict(arguments)), None
        args = dict(arguments)
        binding_id: str | None = None
        side_effecting = False
        replayable = True
        key_fields: tuple[str, ...] = ()
        if op == "tool.invoke/v1":
            binding_id = str(args.get("tool_id") or "")
            inner = dict(args.get("arguments") or {})
            row = self.db.get(Tool, binding_id) if binding_id else None
            if row is not None:
                side_effecting = str(row.method or "").upper() in SIDE_EFFECTING_METHODS
                cfg = row.config_json if isinstance(row.config_json, dict) else {}
                idem = cfg.get("idempotency") if isinstance(cfg.get("idempotency"), dict) else {}
                # Disabling idempotency turns off replay/dedupe, not side-effect tracking: a POST
                # that fails with an ambiguous outcome must still be recorded ``outcome_unknown``.
                if idem.get("enabled") is False:
                    replayable = False
                key_fields = tuple(str(k) for k in (idem.get("key_fields") or ()))
                # A2A is a durable task; the A2A client dedupes on invocation_id itself.
                if row.tool_type == "a2a":
                    side_effecting = False
            args = {"tool_id": binding_id, **inner}
        elif op == "general_skill.consume/v1":
            binding_id = str(args.get("skill_id") or "")
        elif op == "sandbox.execute/v1":
            side_effecting = str(args.get("tool") or "") in {"write_file", "exec_command", "run_skill_script", "apply_patch", "delete_file"}
        inv = ModuleInvocation(
            invocation_id=ctx.trace_id or f"hcall_{int(time.time()*1000)}",
            module_id=op.split(".", 1)[0],
            operation=op,
            arguments=args,
            context=ctx,
            binding_id=binding_id,
            side_effecting=side_effecting,
            idempotency_key_fields=key_fields,
            replayable=replayable,
            metadata={"proxy_name": proxy_name},
        )
        return self.invoke(inv)

    def invoke(self, inv: ModuleInvocation) -> tuple[ModuleResult, Receipt | None]:
        # 1. activation fence (local, monotonic)
        try:
            self.fence.check(self.slot)
            self._fence_resource(inv)
        except ActivationFenced as exc:
            return ModuleResult.fail(exc.code, exc.message), None

        # 1b. pre_tool hooks. The default ``activation.allowlist`` narrows against the frozen
        #     snapshot; a ``capability.pep`` handler marks intent. Hooks can deny a call before it
        #     touches the ledger — nothing is recorded for a refused call, it is only traced.
        pre = self._hooks("pre_tool", inv) if self._hooks else None
        if pre is not None and pre.kind == "deny":
            self._emit("capability_denied", {"operation": inv.operation, "resource": inv.binding_id, "reason": pre.reason, "point": "pre_tool"})
            return ModuleResult.fail("PRE_TOOL_DENIED", pre.reason or "capability call refused by policy"), None

        # 2. ledger: replay or block
        try:
            replay = self._ledger.replay_or_block(inv)
        except OutcomeUnknown as exc:
            return ModuleResult.fail(exc.code, exc.message, extensions={"details": exc.details}), None
        if replay is not None:
            return replay
        try:
            entry = self._ledger.start(inv)
        except _Replayed as r:
            return r.replay

        # 3. facade (PEP + live re-validation + legacy service).
        #    The host itself performs the policy check first, so a provider that only sees
        #    ``host.guard`` (and skips the Guarded Facade) can never bypass the PEP.
        try:
            self._pep(inv)
            result = self._dispatch(inv)
        except PermissionDenied as exc:
            receipt = self._ledger.deny(entry, exc.to_dict())
            self._emit("capability_denied", {"operation": inv.operation, "resource": inv.binding_id, "reason": exc.message})
            return ModuleResult.fail(exc.code, exc.message, extensions={"details": exc.details}), receipt
        except AuthorizationUnavailable as exc:
            receipt = self._ledger.deny(entry, exc.to_dict())
            return ModuleResult.fail(exc.code, exc.message, extensions={"details": exc.details}), receipt
        except ActivationFenced as exc:
            receipt = self._ledger.cancel(entry)
            return ModuleResult.fail(exc.code, exc.message), receipt
        except ModuleSdkError as exc:
            result = ModuleResult.fail(exc.code, exc.message, extensions={"details": exc.details})
        except Exception as exc:  # provider blew up: maybe sent
            result = ModuleResult.fail("HARNESS_TOOL_ERROR", str(exc))

        # 4. ledger finish
        receipt = self._ledger.finish(entry, result)
        self._emit("capability_invoked", {"operation": inv.operation, "resource": inv.binding_id, "status": receipt.status, "invocation_id": inv.invocation_id})

        # 5. post_tool hooks (ledger.record collects the receipt, citations.collect the citations).
        if self.hooks is not None:
            post = self._hooks("post_tool", inv, result)
            if post.replacement is not None:
                # A handler may rewrite the result the model sees (redact, annotate, mask secrets).
                result = post.replacement
        if result.success:
            for c in result.citations or ():
                if isinstance(c, Mapping):
                    self.citations.append(dict(c))
            ev = (result.extensions or {}).get("evidence") if isinstance(result.extensions, Mapping) else None
            if inv.operation == "knowledge.search/v1" and isinstance(ev, Mapping):
                self.evidence.append(dict(ev))
                # Same trace vocabulary as Harness v2 so the chat UI renders 读取业务资料 with counts.
                self._emit("knowledge_result", {
                    "query": {"query": str(inv.arguments.get("query") or "")},
                    "chunks": list(ev.get("chunks") or [])[:12],
                    "selected_buckets": [{k: b.get(k) for k in ("id", "title", "knowledge_base_id")} for b in (ev.get("selected_buckets") or []) if isinstance(b, Mapping)],
                    "selected_concepts": list(ev.get("selected_concepts") or [])[:12],
                    "evidence_pack": list(ev.get("evidence_pack") or [])[:12],
                    "invocation_id": inv.invocation_id,
                })
        return result, receipt

    # -- internals ----------------------------------------------------------------

    def _finish_task(self, args: dict[str, Any]) -> ModuleResult:
        status = str(args.get("status") or "completed")
        if status not in {"completed", "awaiting_user", "handoff", "failed"}:
            return ModuleResult.fail("INVALID_ARGUMENTS", "status 必须是 completed/awaiting_user/handoff/failed 之一。")
        next_step = str(args.get("next_step_id") or "").strip() or None
        if next_step and self.slot.allowed_next_steps and next_step not in self.slot.allowed_next_steps:
            next_step = None
        envelope = {
            "status": status,
            "reply_fragment": str(args.get("reply_fragment") or "").strip(),
            "slot_updates": dict(args.get("slot_updates") or {}),
            "next_step_id": next_step,
            "task_summary": str(args.get("task_summary") or "").strip(),
            "structured_result": args.get("structured_result"),
        }
        self.slot.finish = envelope
        # A finished step is a closed step: later tool calls must fail. Closing now (rather than in
        # the agent's finally) means the *next* model action in the same turn is refused instead of
        # being executed, and capability calls after this one return ACTIVATION_FENCED.
        self.slot.closed = True
        self._emit("harness_v3_task_finished", {"status": status, "next_step_id": next_step})
        return ModuleResult.ok({"accepted": True, "status": status, "notice": "已记录本步骤结果，请立即停止，不要再调用其他工具。"})

    def _fence_resource(self, inv: ModuleInvocation) -> None:
        allowed = self.slot.allowed()
        rtype = {
            "knowledge.search/v1": "knowledge_base",
            "general_skill.consume/v1": "general_skill",
            "tool.invoke/v1": "tool",
            "mcp.invoke/v1": "tool",
            "a2a.invoke/v1": "tool",
        }.get(inv.operation)
        if rtype is None:
            return
        if rtype == "knowledge_base":
            # knowledge picks from the allowlist inside the facade; only require non-empty
            if not allowed.get("knowledge_base"):
                raise ActivationFenced("no knowledge base is activated for this turn")
            return
        if not inv.binding_id or inv.binding_id not in allowed.get(rtype, set()):
            raise ActivationFenced(f"{rtype} {inv.binding_id!r} is not in the activated set for this turn")

    def _pep(self, inv: ModuleInvocation) -> None:
        """Host-side policy check, run before ``_dispatch``.

        The PEP is a *host* obligation, not a provider courtesy: without this a remote/external
        provider that never calls ``self.d.guard.require`` would bypass it entirely. The Guarded
        Facade still re-checks against the live row (defence in depth, and it owns the live-ref
        attributes), so this only closes the bypass, it never grants anything.
        """

        from staffdeck_harness.composition.projection import live_resource_ref

        def _ref(row: Any, rtype: str) -> ResourceRef:
            # live_resource_ref resolves the acting agent's binding row, so ``binding_status`` /
            # ``private_to_agent`` reflect the real binding — the same projection the facade uses.
            return live_resource_ref(self.db, inv.context.tenant_id, rtype, row, agent=self._agent_row)

        op = inv.operation
        ctx = self.security_context

        if op in {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}:
            row = self.db.get(Tool, inv.binding_id) if inv.binding_id else None
            if row is not None and row.tenant_id == inv.context.tenant_id:
                tool_op = {"http": "tool.invoke/v1", "mcp": "mcp.invoke/v1", "a2a": "a2a.invoke/v1"}.get(row.tool_type, "tool.invoke/v1")
                self.guard.require(ctx, tool_op, _ref(row, "tool"))
                if row.tool_type == "mcp" and row.mcp_server_id:
                    server = self.db.get(MCPServer, row.mcp_server_id)
                    if server is not None:
                        self.guard.require(ctx, "mcp.invoke/v1", _ref(server, "mcp_server"))
            return
        if op == "general_skill.consume/v1":
            row = self.db.get(GeneralSkill, inv.binding_id) if inv.binding_id else None
            if row is not None and row.tenant_id == inv.context.tenant_id:
                self.guard.require(ctx, op, _ref(row, "general_skill"))
            return
        if op == "knowledge.search/v1":
            allowed = self.slot.allowed().get("knowledge_base", set())
            requested = {str(i) for i in (inv.arguments.get("knowledge_base_ids") or []) if str(i).strip()}
            # Knowledge selects resources inside the facade by intersecting requested & allowed;
            # here we narrow with the PEP so the selection is already policy-filtered.
            candidates = sorted(requested & allowed) if requested else sorted(allowed)
            for kb_id in candidates:
                row = self.db.get(KnowledgeBase, kb_id)
                if row is not None and row.tenant_id == inv.context.tenant_id:
                    self.guard.require(ctx, op, _ref(row, "knowledge_base"))
            return
        if op == "sandbox.execute/v1":
            self.guard.require(ctx, op, ResourceRef(type="capability", id=f"sandbox:{inv.arguments.get('tool') or inv.operation}", tenant_id=inv.context.tenant_id, attributes={"binding_status": "active", "private_to_agent": True}))
            return
        # Anything else (e.g. an external ``staff.capability`` family) has no default policy
        # action in DEFAULT_ACTION_MAP; fail closed rather than silently allowing a provider
        # the host cannot classify.
        raise PermissionDenied(f"no policy action mapped for operation {op!r}", details={"operation": op})

    def _hooks(self, point: str, inv: ModuleInvocation, result: ModuleResult | None = None) -> HookDecision:
        """Invoke the wired pipeline hook for ``pre_tool``/``post_tool``.

        ``point`` is passed as the payload so handlers can distinguish the two phases. A broken
        hook handler must never fail the capability call — it is logged and treated as pass.
        """

        if self.hooks is None:
            return HookDecision.passthrough()
        try:
            return self.hooks(point, inv, result)
        except Exception:  # a hook must never take the capability call down
            logger.exception("hook %s failed on %s", point, inv.operation)
            return HookDecision.passthrough()

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(
            db=self.db,
            guard=self.guard,
            security_context=self.security_context,
            model_config=self.model_config,
            agent_row=self._agent_row,
            trace=self.trace,
            remaining_seconds=self.slot.remaining_seconds,
        )

    def _workspace_root(self, ctx: InvocationContext) -> Path:
        if self._workspace is None:
            self._workspace = workspace_for(ctx, self.db)
            self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def sandbox(self, ctx: InvocationContext) -> SandboxFacade:
        if self._sandbox is None:
            policy = self.slot.snapshot.session_policy
            self._sandbox = SandboxFacade(
                self._deps(),
                workspace_root=self._workspace_root(ctx),
                run_id=self.run_id or ctx.run_id or ctx.turn_id,
                task_frame_id=ctx.task_frame_id or ctx.turn_id,
                sandbox_enabled=bool(policy.get("sandbox_enabled", False)),
                network_mode=str(policy.get("sandbox_network_mode", "all")),
                allowed_domains=tuple(policy.get("sandbox_allowed_domains", ())),
            )
        return self._sandbox

    def _dispatch(self, inv: ModuleInvocation) -> ModuleResult:
        """Resolve the provider for ``inv.operation`` from the Module Registry and run it.

        Providers are ``A``/``T`` modules installed under ``staff.capability``; the host
        stays ignorant of which implementation serves an operation. A deployment
        swaps a provider by installing a different module for the same operation.

        The frozen ``CompositionSnapshot`` may pin a provider module id per grant (a Staff or SOP
        binding chose a non-default provider). When it does, that pin is honoured: a pinned module
        that is disabled or no longer provides the operation fails closed (``PROVIDER_UNAVAILABLE``)
        instead of silently switching to the first enabled module under a running turn.
        """

        from staffdeck_harness.modules.registry import peek_registry

        reg = peek_registry()
        if reg is None:
            return ModuleResult.fail("ENGINE_UNAVAILABLE", "运行时正在重启，请稍后重试")
        pin = self._provider_pin(inv)
        installed = reg.resolve_operation_provider(inv.operation, pin)
        if installed is None:
            if pin:
                return ModuleResult.fail("PROVIDER_UNAVAILABLE", f"本步骤绑定的能力提供模块 {pin} 未启用或不提供 {inv.operation}", extensions={"pinned": pin, "operation": inv.operation})
            return ModuleResult.fail("UNSUPPORTED_CAPABILITY", f"没有模块提供能力操作 {inv.operation}")
        provider = installed.provider
        invoke = getattr(provider, "invoke", None)
        if not callable(invoke):
            return ModuleResult.fail("PROVIDER_INVALID", f"模块 {installed.manifest.module_id} 未实现 invoke()")
        self._emit("capability_provider_selected", {"operation": inv.operation, "module_id": installed.manifest.module_id, "module_version": installed.manifest.version, "pinned": bool(pin)})
        return invoke(self, inv)

    def _provider_pin(self, inv: ModuleInvocation) -> str | None:
        """The ``provider_module_id`` frozen on the grant that covers this invocation, if any.

        The grant is matched by operation and (when present) resource id / binding id, so a SOP slot
        that resolved to a specific tool carries that tool's provider pin, not a general one.
        """

        for g in self.slot.grants():
            if g.operation != inv.operation:
                continue
            if g.resource_id and inv.binding_id and g.resource_id != inv.binding_id:
                if g.resource_type == "knowledge_base":
                    continue  # knowledge may span several bases; no single pin applies
                continue
            return g.provider_module_id
        return None

    def discover_artifacts(self, ctx: InvocationContext) -> list[dict[str, Any]]:
        if self._sandbox is None:
            return []
        return self._sandbox.discover_artifacts(ctx.task_frame_id or ctx.turn_id)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        full = {**payload, "snapshot_id": self.slot.snapshot.snapshot_id, "execution_engine": "harness_v3"}
        if self.trace:
            self.trace(event, full)
        try:
            from staffdeck_harness.events.relay import fanout_event

            fanout_event(self.security_context.tenant_id, str(getattr(self.slot, "session_id", "") or self.slot.snapshot.staff_id), event, full)
        except Exception:  # observers never break a capability call
            logger.exception("observer fan-out failed for %s", event)
