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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlmodel import Session

from app.agents.branching import get_agent, visible_knowledge_base_versions
from app.core.capability_manifest import general_skill_snapshot_digest, tool_snapshot_digest
from app.db.models import GeneralSkill, ModelConfig, Tool
from staffdeck_dsh.capabilities.facade import FacadeDeps, GeneralSkillFacade, KnowledgeFacade, SandboxFacade, ToolFacade, workspace_for
from staffdeck_dsh.capabilities.ledger import InvocationLedger, _Replayed
from staffdeck_dsh.composition.compiler import CapabilityGrant, CompositionSnapshot
from staffdeck_dsh.contracts.errors import ActivationFenced, AuthorizationUnavailable, ModuleSdkError, OutcomeUnknown, PermissionDenied
from staffdeck_dsh.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult, Receipt
from staffdeck_dsh.contracts.security import SecurityContext
from staffdeck_dsh.security.profile import Guard

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
        key_fields: tuple[str, ...] = ()
        if op == "tool.invoke/v1":
            binding_id = str(args.get("tool_id") or "")
            inner = dict(args.get("arguments") or {})
            row = self.db.get(Tool, binding_id) if binding_id else None
            if row is not None:
                side_effecting = str(row.method or "").upper() in SIDE_EFFECTING_METHODS
                cfg = row.config_json if isinstance(row.config_json, dict) else {}
                idem = cfg.get("idempotency") if isinstance(cfg.get("idempotency"), dict) else {}
                if idem.get("enabled") is False:
                    side_effecting = False
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
            invocation_id=ctx.trace_id or f"dshcall_{int(time.time()*1000)}",
            module_id=op.split(".", 1)[0],
            operation=op,
            arguments=args,
            context=ctx,
            binding_id=binding_id,
            side_effecting=side_effecting,
            idempotency_key_fields=key_fields,
        )
        return self.invoke(inv)

    def invoke(self, inv: ModuleInvocation) -> tuple[ModuleResult, Receipt | None]:
        # 1. activation fence (local, monotonic)
        try:
            self.fence.check(self.slot)
            self._fence_resource(inv)
        except ActivationFenced as exc:
            return ModuleResult.fail(exc.code, exc.message), None

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

        # 3. facade (PEP + live re-validation + legacy service)
        try:
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
        self._emit("dsh_task_finished", {"status": status, "next_step_id": next_step})
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
        op = inv.operation
        allowed = self.slot.allowed()
        if op == "knowledge.search/v1":
            versions = visible_knowledge_base_versions(self.db, inv.context.tenant_id, None if inv.context.agent_id.endswith(":overall") else inv.context.agent_id)
            version_by_base = {kb_id: v.id for kb_id, v in versions.items()}
            return KnowledgeFacade(self._deps()).search(inv, allowed_ids=set(allowed.get("knowledge_base", set())), version_by_base=version_by_base)
        if op == "general_skill.consume/v1":
            row = self.db.get(GeneralSkill, inv.binding_id) if inv.binding_id else None
            digest = general_skill_snapshot_digest(row) if row is not None else None
            return GeneralSkillFacade(self._deps(), self._workspace_root(inv.context)).consume(inv, expected_digest=digest)
        if op in {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}:
            row = self.db.get(Tool, inv.binding_id) if inv.binding_id else None
            digest = tool_snapshot_digest(self.db, row) if row is not None else None
            return ToolFacade(self._deps()).invoke(inv, expected_digest=digest, active_skill_id=self.slot.active_sop_id)
        if op == "sandbox.execute/v1":
            return self.sandbox(inv.context).execute(inv)
        return ModuleResult.fail("UNSUPPORTED_CAPABILITY", f"不支持的能力操作 {op}")

    def discover_artifacts(self, ctx: InvocationContext) -> list[dict[str, Any]]:
        if self._sandbox is None:
            return []
        return self._sandbox.discover_artifacts(ctx.task_frame_id or ctx.turn_id)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.trace:
            self.trace(event, {**payload, "snapshot_id": self.slot.snapshot.snapshot_id, "execution_engine": "dsh"})
