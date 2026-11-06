"""Fixed execution controls. These are NOT capability providers or Staff-installed modules."""

from __future__ import annotations
from contextlib import nullcontext

from staffdeck_harness.contracts.errors import ActivationFenced
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.sop.submission import SopResultValidator, STEP_RESULT_SCHEMA

CONTROL_TOOLS = {
    "external_task_status": {
        "operation": "task.status/v1",
        "description": "查询先前异步工具回执中的 StaffDeck 任务号。只能查询当前用户的任务；accepted/working 不是完成，不要重新提交原工具。",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"], "additionalProperties": False},
    },
    "submit_step_result": {
        "operation": "step.submit/v1",
        "description": (
            "向 StaffDeck 运行控制层提交当前 SOP 步骤的结构化结果。完成本步骤、需要用户补充信息、"
            "需要转人工或任务失败时都必须调用一次本工具，然后停止。"
        ),
        "parameters": STEP_RESULT_SCHEMA,
    },
}


def is_control_tool(name: str) -> bool:
    return name.removeprefix("mcp__staffdeck__") in {"submit_step_result", "finish_task"}


def all_tool_schemas():
    from staffdeck_harness.capabilities.host import proxy_tool_schemas

    return proxy_tool_schemas() + [
        {"name": name, "description": spec["description"], "parameters": spec["parameters"]}
        for name, spec in CONTROL_TOOLS.items()
    ]


class ExecutionBudgetExceeded(RuntimeError):
    pass


class EngineToolFailure(RuntimeError):
    pass


class StepCompletionPort:
    """Bridge commits an accepted domain result; it owns no validation rules."""
    def __init__(self, capabilities, requirement):
        self.capabilities = capabilities
        from staffdeck_harness.contracts.manifest import SlotName
        registry = getattr(capabilities, 'registry', None)
        installed = registry.provider(SlotName.RUNTIME_SOP) if registry else None
        factory = getattr(installed.provider, 'submission_validator', None) if installed else None
        self.validator = factory(requirement) if callable(factory) else SopResultValidator(requirement)

    def validate(self, arguments):
        host, slot = self.capabilities, self.capabilities.slot
        host.fence.check(slot)
        from staffdeck_harness.runtime.structured_output import validate_result
        shape = validate_result(arguments, STEP_RESULT_SCHEMA)
        if not shape.success:
            return shape
        result = self.validator.submit(arguments, allowed_next_steps=slot.allowed_next_steps,
            capability_results=host.results, citations=host.citations, evidence=host.evidence)
        if not result.success:
            return result
        shape = validate_result(result.data, STEP_RESULT_SCHEMA)
        if not shape.success:
            return shape
        return result

    def commit(self, result):
        host, slot = self.capabilities, self.capabilities.slot
        host.fence.check(slot)
        slot.finish = dict(result.data)
        slot.finish.setdefault('next_step_id', None)
        slot.closed = True
        host._emit('harness_v3_task_finished', {'status':slot.finish['status'],
            'next_step_id':slot.finish['next_step_id'], 'control':'submit_step_result'})
        return ModuleResult.ok({'accepted':True, 'notice':'SOP 步骤结果已提交，请停止生成。'})

    def submit(self, arguments):
        result = self.validate(arguments)
        return self.commit(result) if result.success else result


class ExecutionHost:
    """Transport multiplexer: public capabilities and fixed controls are separate ports."""

    def __init__(self, capabilities, requirement, *, max_actions=32):
        self.capabilities = capabilities
        self.completion = StepCompletionPort(capabilities, requirement)
        self.requirement = requirement
        self.max_actions = max(1, min(int(max_actions), 100))
        self.actions = 0
        self.engine_actions = 0
        self.engine_calls = {}
        self.engine_results = set()
        self.engine_failure = None
        self.exhausted = False
        from app.core.capability_recovery import CapabilityRecovery

        self.recovery = CapabilityRecovery()
        self.recovery_blocked = None
        self.waiting_external_task = None
        self.argument_repair = None
        from staffdeck_harness.runtime.structured_output import SubmissionRecovery
        self.submission_recovery = SubmissionRecovery()
        self.control_blocked = None
        self.repair_only = False

    def __getattr__(self, name):
        return getattr(self.capabilities, name)

    @property
    def total_actions(self):
        # Engine failures before MCP dispatch are actions too; successful calls
        # seen at both boundaries must not be counted twice.
        return max(self.actions, self.engine_actions)

    def observe_engine_event(self, event):
        data = event.get('data') or {}
        if event.get('type') == 'tool/call':
            call_id = str(data.get('callId') or '')
            if call_id not in self.engine_calls:
                self.engine_calls[call_id] = data
                self.engine_actions += 1
        elif event.get('type') == 'tool/result':
            error = data.get('error') or {}
            for block in (data.get('message') or {}).get('content') or []:
                if not isinstance(block, dict) or block.get('type') != 'tool-result':
                    continue
                call_id = str(block.get('toolCallId') or '')
                if call_id in self.engine_results:
                    continue
                self.engine_results.add(call_id)
                if error.get('code') == 'UNKNOWN_TOOL' and block.get('isError'):
                    call = self.engine_calls.get(call_id, {})
                    blocked = self.recovery.observe('engine:' + str(call.get('name') or ''),
                        call.get('arguments'), {'success': False, 'error': error})
                    if blocked:
                        self.engine_failure = {**blocked,
                            'message': '工具解析连续失败（UNKNOWN_TOOL），已停止自动重试；这些调用没有执行业务操作。'}
            self.exhausted = self.total_actions >= self.max_actions and self.capabilities.slot.finish is None

    def tool_schemas(self):
        return all_tool_schemas()

    def model_tool_names(self):
        names = {s["name"] for s in self.capabilities.tool_schemas()}
        names.add("external_task_status")
        if self.requirement.kind == "sop":
            names.add("submit_step_result")
        return names

    def invoke_proxy(self, name, arguments, ctx):
        host = self.capabilities
        with host._invoke_lock:
            try:
                host.fence.check(host.slot)
                if self.repair_only:
                    return ModuleResult.fail('RESULT_REPAIR_ONLY', '当前仅修正结果，不允许调用业务能力。'), None
                if self.control_blocked:
                    return ModuleResult.fail(self.control_blocked['code'],self.control_blocked['message']), None
                if self.waiting_external_task:
                    return ModuleResult.fail("EXTERNAL_TASK_PENDING", "异步请求已受理，本轮停止执行，请勿重复提交。"), None
                if self.exhausted:
                    return ModuleResult.fail(
                        "ACTION_BUDGET_EXCEEDED", "本次动作预算耗尽，等待下一次推进。"
                    ), None
                if self.recovery_blocked:
                    return ModuleResult.fail(self.recovery_blocked["code"], self.recovery_blocked["message"]), None
                if name.removeprefix("mcp__staffdeck__") == "external_task_status":
                    from app.db.models import ExternalBusinessTask
                    task = host.db.get(ExternalBusinessTask, str(arguments.get("task_id") or ""))
                    if task is None or (task.tenant_id, task.user_id) != (ctx.tenant_id, ctx.user_id):
                        result = ModuleResult.fail("NOT_FOUND", "任务不存在或无权查看")
                    else:
                        result = ModuleResult.ok({"task_id": task.id, "status": task.status,
                            "provider_task_id": task.external_task_id,
                            "result": task.result_json, "error": task.error_json})
                    receipt = None
                elif is_control_tool(name):
                    with host.registry.work_lease() if host.registry else nullcontext():
                        result, receipt = self.completion.submit(dict(arguments)), None
                else:
                    result, receipt = host.invoke_proxy(name, arguments, ctx)
                self.actions += 1
                control = is_control_tool(name)
                if control:
                    self.control_blocked = self.submission_recovery.observe(result,
                        schema=STEP_RESULT_SCHEMA, allowed_next_steps=host.slot.allowed_next_steps)
                from staffdeck_harness.runtime.structured_output import repair_candidate
                self.argument_repair = repair_candidate(result, receipt, tool=name,
                    current=self.argument_repair,
                    context=self.submission_recovery.pending if control else None)
                if (result.success and isinstance(result.data, dict)
                        and result.data.get("detached") is True and result.data.get("accepted") is True):
                    self.waiting_external_task = dict(result.data)
                if not result.success:
                    host._emit("harness_action_failed", {
                        "iteration": self.actions, "tool_name": "mcp__staffdeck__" + name,
                        "error": dict(result.error or {}),
                    })
                resource = str(arguments.get("tool_id") or arguments.get("resource_id") or arguments.get("skill_id") or "")
                self.recovery_blocked = None if control else self.recovery.observe(
                    f"{name}:{resource}", arguments,
                    {"success": result.success, "error": dict(result.error or {})},
                )
                self.exhausted = self.total_actions >= self.max_actions and host.slot.finish is None
                return result, receipt
            except ActivationFenced as exc:
                return ModuleResult.fail(exc.code, exc.message), None
