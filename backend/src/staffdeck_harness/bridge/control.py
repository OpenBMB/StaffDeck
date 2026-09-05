"""Fixed execution controls. These are NOT capability providers or Staff-installed modules."""

from __future__ import annotations
from contextlib import nullcontext

from staffdeck_harness.contracts.errors import ActivationFenced
from staffdeck_harness.contracts.invocation import ModuleResult

CONTROL_TOOLS = {
    "submit_step_result": {
        "operation": "step.submit/v1",
        "description": (
            "向 StaffDeck 运行控制层提交当前 SOP 步骤的结构化结果。完成本步骤、需要用户补充信息、"
            "需要转人工或任务失败时都必须调用一次本工具，然后停止。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "awaiting_user", "handoff", "failed"],
                },
                "reply_fragment": {
                    "type": "string",
                    "description": "给用户的回复正文（可含 [N] 引用）",
                },
                "slot_updates": {"type": "object", "description": "本步骤收集到的槽位值"},
                "next_step_id": {
                    "type": ["string", "null"],
                    "description": "SOP 下一步 node_id（仅在允许的转移中选择）",
                },
                "task_summary": {"type": "string", "description": "一句话总结本步骤做了什么"},
                "structured_result": {"description": "可选的结构化结果"},
            },
            "required": ["status", "reply_fragment"],
        },
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


class StepCompletionPort:
    def __init__(self, capabilities, requirement):
        self.capabilities = capabilities
        self.requirement = requirement

    def submit(self, arguments) -> ModuleResult:
        from jsonschema import validate, ValidationError

        host, slot = self.capabilities, self.capabilities.slot
        host.fence.check(slot)
        if self.requirement.kind != "sop":
            return ModuleResult.fail(
                "CONTROL_UNAVAILABLE", "普通对话直接返回最终文本，不提交 SOP 步骤结果。"
            )
        try:
            validate(arguments, CONTROL_TOOLS["submit_step_result"]["parameters"])
        except ValidationError as exc:
            return ModuleResult.fail("INVALID_ARGUMENTS", exc.message)
        next_step = str(arguments.get("next_step_id") or "").strip() or None
        if next_step and next_step not in slot.allowed_next_steps:
            return ModuleResult.fail("INVALID_TRANSITION", "下一步不在当前 SOP 的允许转移中。")
        if arguments["status"] == "completed":
            from staffdeck_harness.runtime.completion import missing_capabilities

            missing = missing_capabilities(
                self.requirement, host.results, host.citations, host.evidence
            )
            if missing:
                return ModuleResult.fail(
                    "REQUIRED_CAPABILITY_NOT_INVOKED", "必需能力尚未成功执行：" + "、".join(missing)
                )
        slot.finish = {**arguments, "next_step_id": next_step}
        slot.closed = True
        host._emit(
            "harness_v3_task_finished",
            {
                "status": arguments["status"],
                "next_step_id": next_step,
                "control": "submit_step_result",
            },
        )
        return ModuleResult.ok({"accepted": True, "notice": "SOP 步骤结果已提交，请停止生成。"})


class ExecutionHost:
    """Transport multiplexer: public capabilities and fixed controls are separate ports."""

    def __init__(self, capabilities, requirement, *, max_actions=32):
        self.capabilities = capabilities
        self.completion = StepCompletionPort(capabilities, requirement)
        self.requirement = requirement
        self.max_actions = max(1, min(int(max_actions), 100))
        self.actions = 0
        self.exhausted = False

    def __getattr__(self, name):
        return getattr(self.capabilities, name)

    def tool_schemas(self):
        return all_tool_schemas()

    def model_tool_names(self):
        names = {s["name"] for s in self.capabilities.tool_schemas()}
        if self.requirement.kind == "sop":
            names.add("submit_step_result")
        return names

    def invoke_proxy(self, name, arguments, ctx):
        host = self.capabilities
        with host._invoke_lock:
            try:
                host.fence.check(host.slot)
                if self.exhausted:
                    return ModuleResult.fail(
                        "ACTION_BUDGET_EXCEEDED", "本次动作预算耗尽，等待下一次推进。"
                    ), None
                if is_control_tool(name):
                    with host.registry.work_lease() if host.registry else nullcontext():
                        result, receipt = self.completion.submit(dict(arguments)), None
                else:
                    result, receipt = host.invoke_proxy(name, arguments, ctx)
                self.actions += 1
                self.exhausted = self.actions >= self.max_actions and host.slot.finish is None
                return result, receipt
            except ActivationFenced as exc:
                return ModuleResult.fail(exc.code, exc.message), None
