"""CapabilityHost: the single door through which the Harness v3 engine (or anything else) reaches a capability.

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
registers in the Harness v3 engine. The proxy never executes — every call comes back here.
"""

from __future__ import annotations

import logging
import time
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlmodel import Session

from app.db.models import ModelConfig
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


def _invalid_arguments(name: str, error: Any) -> ModuleResult:
    path = list(error.absolute_path)
    location = ".".join(str(part) for part in path) or "arguments"
    return ModuleResult(success=False, error={
        "code": "INVALID_ARGUMENTS", "retryable": False, "executed": False,
        "message": f"{name} 的参数在 {location} 未通过 {error.validator} 校验，本次未执行业务操作。请对照初始工具定义或 capability_describe 的 schema 修正字段、类型和嵌套层级。",
        "next_action": "correct_arguments",
        "details": {"path": path, "rule": error.validator, "expected": error.validator_value},
    })

# Stable model-facing proxy tools. Names are wire-stable; the engine sees only these.
PROXY_TOOLS: dict[str, dict[str, Any]] = {
    "capability_invoke": {
        "operation": "capability.invoke/v1",
        "description": "按 capability_describe 返回的版本化操作和资源标识调用扩展能力。",
        "parameters": {"type": "object", "properties": {"operation": {"type": "string"}, "resource_id": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["operation", "resource_id", "arguments"]},
    },
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
}


def proxy_tool_schemas() -> list[dict[str, Any]]:
    """Every proxy tool, unfiltered.

    The engine's MCP client fetches ``tools/list`` once when the process connects and only
    re-syncs on a server notification (which a stateless Streamable HTTP server cannot send).
    A pooled process therefore registers a *stable* tool set for its whole life; what a given
    phase or frame may actually call is decided by the host it is bound to at invoke time
    (activation fence, PEP), never by hiding schemas.
    """

    return [{"name": name, "description": spec["description"], "parameters": spec["parameters"]} for name, spec in PROXY_TOOLS.items()]


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
    execution_engine: str = "harness_v3"
    _ledger: InvocationLedger = field(init=False)
    _sandbox: SandboxFacade | None = field(init=False, default=None)
    _workspace: Path | None = field(init=False, default=None)
    # Collected on the host (worker threads) so the turn result never depends on
    # re-parsing the engine's transcript, which may have spilled/previewed large results.
    citations: list[dict[str, Any]] = field(init=False, default_factory=list)
    evidence: list[dict[str, Any]] = field(init=False, default_factory=list)
    results: list[dict[str, Any]] = field(init=False, default_factory=list)
    # Optional hook point for ``pre_tool``/``post_tool`` decisions, wired by the task agent to
    # its InteractionPipelineHost. When unset, the host runs without a hook plan (unit tests).
    hooks: Callable[[str, ModuleInvocation, ModuleResult | None], HookDecision] | None = None
    _hook_trace: Callable[[str, dict[str, Any]], None] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        from staffdeck_harness.modules.registry import peek_registry

        self.registry = peek_registry()  # pinned for this turn, not a later generation
        self.current_receipt: Receipt | None = None
        self._invoke_lock = threading.RLock()
        self._ledger = InvocationLedger(self.db)
        self._catalogs = {}
        self._descriptors = {}

    # -- schemas the Bridge registers in the engine -----------------------------------

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
            try:
                descriptor = self._descriptor(g)
                self._authorize_descriptor(g.operation, descriptor)
                item.update({"description": descriptor.description, "input_schema": dict(descriptor.input_schema),
                             **dict(descriptor.metadata)})
            except (ModuleSdkError, PermissionDenied, AuthorizationUnavailable):
                continue
            if self.registry and g.operation in self.registry.operations:
                contract = self.registry.operations[g.operation]
                item.update({"input_schema": dict(contract.parameters), "description": contract.description, "proxy": "capability_invoke"})
            items.append(item)
        if self._sandbox is not None and kind in {"all", "sandbox"}:
            items.extend({"kind": "sandbox", "id": s["name"], "name": s["name"], "description": s["description"], "input_schema": s["input_schema"]} for s in self._sandbox.schemas())
        return {"items": items, "snapshot_id": self.slot.snapshot.snapshot_id}

    # -- the funnel -------------------------------------------------------------

    def invoke_proxy(self, proxy_name: str, arguments: Mapping[str, Any], ctx: InvocationContext) -> tuple[ModuleResult, Receipt | None]:
        with self._invoke_lock:
            return self._invoke_proxy(proxy_name, arguments, ctx)

    def _invoke_proxy(self, proxy_name: str, arguments: Mapping[str, Any], ctx: InvocationContext) -> tuple[ModuleResult, Receipt | None]:
        try:
            self.fence.check(self.slot)
        except ActivationFenced as exc:
            return ModuleResult(success=False, error={"code": exc.code,
                "message": "当前执行上下文已关闭或失效，本次未执行业务操作。停止使用旧上下文，等待重新装配。",
                "retryable": False, "executed": False, "next_action": "replan"}), None
        spec = PROXY_TOOLS.get(proxy_name)
        if spec is None:
            return ModuleResult(success=False, error={"code": "TOOL_NOT_AVAILABLE",
                "message": f"未知系统工具 {proxy_name}，本次未执行业务操作。请使用初始 prompt 中列出的 mcp__staffdeck__ 系统接口；业务名称不能当作代理函数名。",
                "retryable": False, "executed": False, "next_action": "check_system_tool_guide"}), None
        from jsonschema import ValidationError, validate

        try:
            validate(dict(arguments), spec["parameters"])
        except ValidationError as exc:
            return _invalid_arguments(proxy_name, exc), None
        op = spec["operation"]
        if op == "capability.invoke/v1":
            operation = str(arguments.get("operation") or "")
            contract = self.registry.operations.get(operation) if self.registry else None
            if contract is None:
                return ModuleResult(success=False, error={"code": "UNSUPPORTED_CAPABILITY",
                    "message": "该扩展操作未注册，本次未执行业务操作。先调用 capability_describe，使用返回的版本化 operation 和资源 id，不要猜测操作名。",
                    "retryable": False, "executed": False, "next_action": "capability_describe"}), None
            from jsonschema import ValidationError, validate

            args = dict(arguments.get("arguments") or {})
            try:
                validate(args, dict(contract.parameters))
            except ValidationError as exc:
                return _invalid_arguments(operation, exc), None
            return self.invoke(ModuleInvocation(invocation_id=ctx.trace_id or f"hcall_{time.time_ns()}", module_id=operation.split(".")[0], operation=operation, arguments=args, context=ctx, binding_id=str(arguments.get("resource_id") or ""), side_effecting=contract.side_effecting))
        if op == "capability.describe/v1":
            return ModuleResult.ok(self.describe(str(arguments.get("kind") or "all"))), None
        args = dict(arguments)
        binding_id: str | None = None
        side_effecting = False
        replayable = True
        key_fields: tuple[str, ...] = ()
        if op == "tool.invoke/v1":
            binding_id = str(args.get("tool_id") or "")
            inner = dict(args.get("arguments") or {})
            args = {**inner, "tool_id": binding_id}
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
            metadata={"proxy_name": proxy_name, "execution_engine": self.execution_engine},
        )
        return self.invoke(inv)

    def invoke(self, inv: ModuleInvocation) -> tuple[ModuleResult, Receipt | None]:
        with self._invoke_lock:
            try:
                with self.registry.work_lease() if self.registry else nullcontext():
                    return self._invoke(inv)
            except ModuleSdkError as exc:
                return ModuleResult.fail(exc.code, exc.message), None

    def _invoke(self, inv: ModuleInvocation) -> tuple[ModuleResult, Receipt | None]:
        if self.slot.snapshot.session_policy.get("read_only"):
            readable = inv.operation == "general_skill.consume/v1" or (
                inv.operation == "sandbox.execute/v1" and inv.arguments.get("tool") in
                {"read_file", "list_directory", "glob", "grep", "file_info", "extract_document_text"}
                and not (inv.arguments.get("arguments") or {}).get("output_path"))
            if not readable:
                return ModuleResult.fail("READ_ONLY_TEST", "本次为只读测试，不能执行命令、写文件或调用业务工具。"), None
        # 1. activation fence (local, monotonic)
        try:
            self.fence.check(self.slot)
            if (inv.context.tenant_id != self.slot.snapshot.tenant_id
                    or inv.context.agent_id != self.slot.snapshot.staff_id):
                raise ActivationFenced("调用身份与当前装配不一致")
            self._fence_resource(inv)
        except ActivationFenced as exc:
            return ModuleResult(success=False, error={"code": exc.code, "message": exc.message,
                "retryable": False, "executed": False,
                "next_action": "check_binding" if exc.details.get("reason") == "RESOURCE_NOT_BOUND" else "replan",
                "details": dict(exc.details)}), None

        # 1b. pre_tool hooks. The default ``activation.allowlist`` narrows against the frozen
        #     snapshot; a ``capability.pep`` handler marks intent. Hooks can deny a call before it
        #     touches the ledger — nothing is recorded for a refused call, it is only traced.
        pre = self._hooks("pre_tool", inv) if self._hooks else None
        if pre is not None and pre.kind == "deny":
            self._emit("capability_denied", {"operation": inv.operation, "resource": inv.binding_id, "reason": pre.reason, "point": "pre_tool"})
            return ModuleResult.fail("PRE_TOOL_DENIED", pre.reason or "capability call refused by policy"), None

        # Replays are reads too: authorization must precede cache access.
        try:
            descriptors = self._pep(inv)
            from dataclasses import replace
            if len(descriptors) == 1:
                descriptor = descriptors[0]
                inv = replace(inv, side_effecting=descriptor.side_effecting or inv.side_effecting,
                              replayable=descriptor.replayable, idempotency_key_fields=descriptor.idempotency_key_fields)
                if descriptor.input_schema and inv.operation in {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}:
                    from jsonschema import ValidationError, validate
                    args = {k: value for k, value in inv.arguments.items() if k != "tool_id"}
                    try:
                        validate(args, dict(descriptor.input_schema))
                    except ValidationError as exc:
                        return _invalid_arguments(descriptor.name, exc), None
        except (PermissionDenied, AuthorizationUnavailable) as exc:
            try:
                entry = self._ledger.start(inv)
            except (_Replayed, OutcomeUnknown):
                return ModuleResult.fail(exc.code, exc.message), None
            receipt = self._ledger.deny(entry, exc.to_dict())
            return ModuleResult.fail(exc.code, exc.message), receipt

        # 2. ledger: replay or block
        try:
            replay = self._ledger.replay_or_block(inv)
        except OutcomeUnknown as exc:
            return ModuleResult.fail(exc.code, exc.message, extensions={"details": exc.details}), None
        if replay is not None:
            return self._postprocess(inv, *replay)
        try:
            entry = self._ledger.start(inv)
        except _Replayed as r:
            return self._postprocess(inv, *r.replay)

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
        receipt = self._ledger.finish(entry, result, cache_result=False)
        self._emit("capability_invoked", {"operation": inv.operation, "resource": inv.binding_id, "status": receipt.status, "invocation_id": inv.invocation_id})

        return self._postprocess(inv, result, receipt)

    def _postprocess(self, inv: ModuleInvocation, result: ModuleResult, receipt: Receipt) -> tuple[ModuleResult, Receipt]:
        # Output denial must not release the claim of an already-sent write.
        self.current_receipt = receipt
        result = self.project_result(inv, result)
        self._ledger.cache_projection(receipt, result)
        name = inv.operation
        descriptor = self._descriptors.get((inv.operation, inv.binding_id))
        if descriptor is not None:
            name = descriptor.name
            if inv.operation == "general_skill.consume/v1":
                name = f"general_skill.{descriptor.metadata.get('slug', descriptor.name)}"
        elif inv.operation == "knowledge.search/v1":
            name = "knowledge_search"
        self.results.append({"tool_name": name, "operation": inv.operation, "resource_id": inv.binding_id,
                             "success": result.success, "data": result.data, "error": result.error,
                             "citations": list(result.citations), "receipt": receipt.to_json()})
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


    def _fence_resource(self, inv: ModuleInvocation) -> None:
        allowed = self.slot.allowed()
        rtype = {
            "knowledge.search/v1": "knowledge_base",
            "general_skill.consume/v1": "general_skill",
            "tool.invoke/v1": "tool",
            "mcp.invoke/v1": "tool",
            "a2a.invoke/v1": "tool",
        }.get(inv.operation)
        if self.registry and inv.operation in self.registry.operations:
            rtype = self.registry.operations[inv.operation].resource_type
        if rtype is None:
            return
        tool_ops = {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}
        operation_ids = {g.resource_id for g in self.slot.grants() if g.operation == inv.operation or (inv.operation in tool_ops and g.operation in tool_ops)}
        if not operation_ids or (inv.binding_id and inv.binding_id not in operation_ids):
            raise ActivationFenced("该资源未绑定到当前步骤的此项操作，本次未执行业务操作。先用 capability_describe 核对资源 id 与 operation；仍未列出时需修正 SOP/员工绑定，不要直接重试。",
                                   details={"reason": "RESOURCE_NOT_BOUND", "operation": inv.operation, "resource_id": inv.binding_id})
        if rtype == "knowledge_base":
            # knowledge picks from the allowlist inside the facade; only require non-empty
            if not allowed.get("knowledge_base"):
                raise ActivationFenced("当前步骤没有绑定知识库。请检查 SOP/员工的知识库绑定；capability_describe 不会新增授权。",
                                       details={"reason": "RESOURCE_NOT_BOUND", "resource_type": "knowledge_base"})
            return
        if not inv.binding_id or inv.binding_id not in allowed.get(rtype, set()):
            raise ActivationFenced("资源 id 为空或不在当前步骤的绑定集合中，本次未执行业务操作。请从 capability_describe 返回的 id 选择资源，不使用展示名称代替 id。",
                                   details={"reason": "RESOURCE_NOT_BOUND", "resource_type": rtype, "resource_id": inv.binding_id})

    def _source_context(self):
        from staffdeck_harness.contracts.sources import SourceContext
        return SourceContext(self.slot.snapshot.tenant_id, self.slot.snapshot.staff_id,
                             self.slot.session_id, self.security_context.channel or "web",
                             self.security_context.actor_user_id or self.security_context.principal_id,
                             self.security_context.execution)

    def _descriptor(self, grant):
        from staffdeck_harness.composition.sources import resolve_source
        from staffdeck_harness.contracts.manifest import SlotName
        from staffdeck_harness.contracts.sources import ResourceDescriptor

        installed = self.registry.resolve_operation_provider(grant.operation, grant.provider_module_id) if self.registry else None
        if installed is None:
            raise ModuleSdkError("绑定的能力模块不可用", code="PROVIDER_UNAVAILABLE")
        if grant.provider_version and grant.provider_version != installed.manifest.version:
            raise ModuleSdkError("模块版本与执行快照不一致", code="PROVIDER_VERSION_CHANGED")
        catalog_id = self.registry.resolve_catalog_provider(installed).manifest.module_id
        if catalog_id not in self._catalogs:
            self._catalogs[catalog_id] = resolve_source(self.registry, SlotName.RESOURCE_CATALOG, self.db, module_id=catalog_id)
        from dataclasses import replace
        d = self._catalogs[catalog_id].resolve(replace(self._source_context(), resource_config=grant.provider_config), grant.resource_type,
                                              grant.resource_id, grant.operation)
        if not isinstance(d, ResourceDescriptor) or (d.ref.type, d.ref.id, d.ref.tenant_id) != (
                grant.resource_type, grant.resource_id, self.slot.snapshot.tenant_id):
            raise ModuleSdkError("目录返回了不匹配的资源身份", code="RESOURCE_CONTRACT_INVALID")
        tool_ops = {"tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"}
        if d.operation != grant.operation and not {d.operation, grant.operation} <= tool_ops:
            raise ModuleSdkError("资源操作与绑定不一致", code="RESOURCE_CONTRACT_INVALID")
        if not d.available:
            raise ModuleSdkError("资源已停用或不可用", code="RESOURCE_UNAVAILABLE")
        if grant.resource_digest and grant.resource_digest != d.digest:
            raise ModuleSdkError("资源版本与当前执行快照不一致", code="CAPABILITY_SNAPSHOT_CHANGED")
        previous = self._descriptors.get((grant.operation, grant.resource_id))
        if previous is not None and previous.digest and previous.digest != d.digest:
            raise ModuleSdkError("资源在本次执行中发生变化，请重新装配", code="CAPABILITY_SNAPSHOT_CHANGED")
        self._descriptors[(grant.operation, grant.resource_id)] = d
        return d

    def _pep(self, inv: ModuleInvocation):
        """Unskippable authorization uses catalog descriptors, never resource ORM rows."""
        grants = [g for g in self.slot.grants() if g.operation == inv.operation
                  and (not inv.binding_id or g.resource_id == inv.binding_id)]
        if inv.operation == "knowledge.search/v1":
            requested = set(inv.arguments.get("knowledge_base_ids") or ())
            grants = [g for g in grants if not requested or g.resource_id in requested]
        if inv.operation == "sandbox.execute/v1":
            self.guard.require(self.security_context, inv.operation, ResourceRef(
                type="capability", id=f"sandbox:{inv.arguments.get('tool') or inv.operation}",
                tenant_id=inv.context.tenant_id, attributes={"binding_status": "active", "private_to_agent": True}))
            return []
        if not grants:
            raise PermissionDenied("no resource bound for this operation", details={"operation": inv.operation})
        descriptors = []
        for g in grants:
            d = self._descriptor(g)
            self._authorize_descriptor(inv.operation, d)
            descriptors.append(d)
        return descriptors

    def _authorize_descriptor(self, operation, descriptor):
        guard = self.guard
        if self.registry and operation in self.registry.operations:
            from staffdeck_harness.contracts.security import PolicyActionMapper
            contract = self.registry.operations[operation]
            guard = Guard(self.guard.module_id, self.guard.profile,
                          PolicyActionMapper({operation: (contract.action, contract.resource_type)}))
        guard.require(self._resource_security_context(operation, descriptor.ref), descriptor.operation, descriptor.ref)
        for related in descriptor.related_resources:
            if related.tenant_id != self.slot.snapshot.tenant_id:
                raise PermissionDenied("related resource crossed tenant boundary")
            guard.require(self._resource_security_context(operation, related), descriptor.operation, related)

    def _resource_security_context(self, operation, resource):
        """Execution location is not authority provenance.

        A SOP slot may override the provider/version of a Staff-owned capability
        without turning its authorization into a SOP delegation. Only capabilities
        absent from the direct Staff grant set use the active SOP's authority.
        The snapshot, not model arguments or descriptor metadata, selects the path.
        """
        from dataclasses import replace
        staff_owned = resource.tenant_id == self.slot.snapshot.tenant_id and any(g.scope == 'general' and g.sop_id is None
            and (g.operation, g.resource_type, g.resource_id) == (operation, resource.type, resource.id)
            for g in self.slot.snapshot.grants)
        plan = self.slot.snapshot.sop(self.slot.active_sop_id) if self.slot.active_sop_id else None
        ref = dict(plan.authorization_ref) if not staff_owned and plan and plan.authorization_ref else None
        # Clear inherited SOP context for Staff-owned resources, including async
        # resumption. Never retry a denied SOP delegation as a Staff permission.
        return replace(self.security_context, sop_authorization_ref=ref,
            attributes={**self.security_context.attributes,
                        'capability_authority': 'staff' if staff_owned else 'sop' if plan else 'staff'})

    def project_result(self, inv: ModuleInvocation, result: ModuleResult) -> ModuleResult:
        """Project an already executed result; never invokes a provider or releases its claim."""
        from staffdeck_harness.runtime.result_policy import project_result
        return project_result(self._hooks if self.hooks is not None else None, inv, result)

    def _hooks(self, point: str, inv: ModuleInvocation, result: ModuleResult | None = None) -> HookDecision:
        """Invoke the wired pipeline hook for ``pre_tool``/``post_tool``.

        ``point`` is passed as the payload so handlers can distinguish the two phases. A broken
        hook handler must never fail the capability call — it is logged and treated as pass.
        """

        if self.hooks is None:
            return HookDecision.passthrough()
        try:
            return self.hooks(point, inv, result)
        except Exception:
            logger.exception("hook %s failed on %s", point, inv.operation)
            return HookDecision.deny(f"required {point} pipeline failed")

    def _local_agent(self):
        from staffdeck_harness.composition.local_sources import local_agent
        return local_agent(self.db, self._source_context())

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(
            db=self.db,
            guard=self.guard,
            security_context=self.security_context,
            model_config=self.model_config,
            agent_row=self._local_agent(),
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
            from staffdeck_harness.contracts.manifest import SlotName
            from staffdeck_harness.contracts.workspace import WorkspaceActivation
            item = self.registry.provider(SlotName.RUNTIME_WORKSPACE) if self.registry else None
            if item is not None:
                self._sandbox = item.provider.build(WorkspaceActivation(
                    ctx, str(self._workspace_root(ctx)), self.slot.snapshot.session_policy,
                    lambda op, ref: self.guard.require(self.security_context, op, ref),
                    self._emit, self.slot.remaining_seconds))
                self._emit("workspace_provider_selected", {"module_id": item.manifest.module_id})
                return self._sandbox
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

    def authorize_http_task(self, inv: ModuleInvocation):
        """Public revalidation port for a deferred task; never executes the HTTP request."""
        self._fence_resource(inv)
        self._pep(inv)
        result = self._dispatch(inv, definition_only=True)
        from staffdeck_harness.contracts.http_task import AuthorizedHttpTask
        if not isinstance(result, AuthorizedHttpTask):
            error = result.error if isinstance(result, ModuleResult) else None
            raise ModuleSdkError((error or {}).get('message') or 'HTTP 定义授权未完成',
                code=(error or {}).get('code') or 'ASYNC_PROVIDER_UNAVAILABLE')
        return result

    def _dispatch(self, inv: ModuleInvocation, *, definition_only=False):
        """Resolve the provider for ``inv.operation`` from the Module Registry and run it.

        Providers are ``A``/``T`` modules installed under ``staff.capability``; the host
        stays ignorant of which implementation serves an operation. A deployment
        swaps a provider by installing a different module for the same operation.

        The frozen ``CompositionSnapshot`` may pin a provider module id per grant (a Staff or SOP
        binding chose a non-default provider). When it does, that pin is honoured: a pinned module
        that is disabled or no longer provides the operation fails closed (``PROVIDER_UNAVAILABLE``)
        instead of silently switching to the first enabled module under a running turn.
        """


        reg = self.registry
        if reg is None:
            return ModuleResult.fail("ENGINE_UNAVAILABLE", "运行时正在重启，请稍后重试")
        pin = self._provider_pin(inv)
        installed = reg.resolve_operation_provider(inv.operation, pin)
        if installed is None:
            if pin:
                return ModuleResult.fail("PROVIDER_UNAVAILABLE", f"本步骤绑定的能力提供模块 {pin} 未启用或不提供 {inv.operation}", extensions={"pinned": pin, "operation": inv.operation})
            return ModuleResult.fail("UNSUPPORTED_CAPABILITY", f"没有模块提供能力操作 {inv.operation}")
        provider = installed.provider
        versions = {g.provider_version for g in self.slot.grants()
                    if g.operation == inv.operation and g.provider_module_id == pin
                    and (not inv.binding_id or g.resource_id == inv.binding_id)
                    and g.provider_version is not None}
        if versions and versions != {installed.manifest.version}:
            return ModuleResult.fail("PROVIDER_VERSION_CHANGED", "能力模块版本与本轮绑定不一致")
        invoke = getattr(provider, "invoke", None)
        if not callable(invoke):
            return ModuleResult.fail("PROVIDER_INVALID", f"模块 {installed.manifest.module_id} 未实现 invoke()")
        self._emit("capability_provider_selected", {"operation": inv.operation, "module_id": installed.manifest.module_id, "module_version": installed.manifest.version, "pinned": bool(pin)})
        from types import MappingProxyType
        from staffdeck_harness.capabilities.local_services import invoke_local
        from staffdeck_harness.contracts.provider import ProviderContext
        from staffdeck_harness.runtime.authorized_transport import selected_transport

        grants = [g for g in self.slot.grants() if g.operation == inv.operation]
        context = ProviderContext(
            module_id=installed.manifest.module_id, module_version=installed.manifest.version,
            config=MappingProxyType({**dict(installed.config), **next((dict(g.provider_config) for g in grants if not inv.binding_id or g.resource_id == inv.binding_id), {})}),
            resource_ids=tuple(g.resource_id for g in grants),
            resource_digests=MappingProxyType({g.resource_id: (self._descriptors.get((g.operation, g.resource_id)).digest
                if self._descriptors.get((g.operation, g.resource_id)) else g.resource_digest)
                for g in grants if g.resource_digest or self._descriptors.get((g.operation, g.resource_id))}),
            remaining_seconds=self.slot.remaining_seconds, emit=self._emit,
            call_local=lambda: invoke_local(self, inv),
            workspace=self.sandbox(inv.context) if inv.operation == "general_skill.consume/v1" else None,
            resource_configs=MappingProxyType({g.resource_id: MappingProxyType(dict(g.provider_config)) for g in grants}),
            transport=selected_transport(self.db, self.registry),
            prepared_http_definition=getattr(self._descriptors.get((inv.operation, inv.binding_id)), 'prepared_http_definition', None),
            local_http_definition=lambda: __import__(
                "staffdeck_harness.runtime.external_tasks", fromlist=["local_definition"]
            ).local_definition(self.db, inv, active_sop_id=self.slot.active_sop_id),
        )
        definition_port = getattr(provider, "http_definition", None)
        definition = definition_port(context, inv) if callable(definition_port) and inv.operation == "tool.invoke/v1" else None
        if definition_only:
            if definition is None:
                raise ModuleSdkError("该模块未提供 HTTP 任务定义", code="ASYNC_PROVIDER_UNAVAILABLE")
        if definition is not None:
            from staffdeck_harness.contracts.http_task import AuthorizedHttpTask
            descriptor = self._descriptors.get((inv.operation, inv.binding_id))
            authorized = AuthorizedHttpTask(definition, installed.manifest.module_id,
                installed.manifest.version, descriptor.digest if descriptor else "")
            if definition_only:
                return authorized
            from staffdeck_harness.runtime.external_tasks import execute_http
            return execute_http(inv, authorized, db=self.db,
                active_sop_id=self.slot.active_sop_id, active_node_id=self.slot.active_node_id,
                timeout_seconds=self.slot.remaining_seconds())
        return invoke(context, inv)

    def _provider_pin(self, inv: ModuleInvocation) -> str | None:
        """The ``provider_module_id`` frozen on the grant(s) that cover this invocation, if any.

        Bound resources (tool/skill) match by binding id. Knowledge search spans every base in
        the activated set (or the requested subset), so its pin is the one shared by *all* those
        bases; bases pinned to different providers cannot be served by one call and fail closed
        (``PROVIDER_CONFLICT``) rather than silently picking one.
        """

        grants = [g for g in self.slot.grants() if g.operation == inv.operation]
        if inv.operation == "knowledge.search/v1":
            requested = {str(i) for i in (inv.arguments.get("knowledge_base_ids") or []) if str(i).strip()}
            covered = [g for g in grants if not requested or g.resource_id in requested]
            pins = {g.provider_module_id for g in covered}
            if len(pins) > 1:
                raise ModuleSdkError("本次检索涉及的知识库绑定了不同的检索模块，请分开检索或统一模块", code="PROVIDER_CONFLICT", details={"pins": sorted(str(p) for p in pins)})
            return next(iter(pins), None)
        for g in grants:
            if g.resource_id and inv.binding_id and g.resource_id != inv.binding_id:
                continue
            return g.provider_module_id
        return None

    def discover_artifacts(self, ctx: InvocationContext) -> list[dict[str, Any]]:
        if self._sandbox is None:
            return []
        return self._sandbox.discover_artifacts(ctx.task_frame_id or ctx.turn_id)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        full = {**payload, "snapshot_id": self.slot.snapshot.snapshot_id, "execution_engine": self.execution_engine}
        if self.trace:
            self.trace(event, full)
        try:
            from staffdeck_harness.events.relay import fanout_event

            fanout_event(self.security_context.tenant_id, str(getattr(self.slot, "session_id", "") or self.slot.snapshot.staff_id), event, full)
        except Exception:  # observers never break a capability call
            logger.exception("observer fan-out failed for %s", event)
