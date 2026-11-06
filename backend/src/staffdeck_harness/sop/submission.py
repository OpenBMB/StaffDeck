"""SOP-owned submission contract and semantic checks; no engine or channel dependency."""
from staffdeck_harness.contracts.invocation import ModuleResult

STEP_RESULT_SCHEMA = {
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
}

class SopResultValidator:
    def __init__(self, requirement):
        self.requirement = requirement

    def submit(self, arguments, *, allowed_next_steps, capability_results, citations, evidence) -> ModuleResult:
        if self.requirement.kind != "sop":
            return ModuleResult.fail(
                "CONTROL_UNAVAILABLE", "普通对话直接返回最终文本，不提交 SOP 步骤结果。"
            )
        next_step = str(arguments.get("next_step_id") or "").strip() or None
        if next_step and next_step not in allowed_next_steps:
            return ModuleResult.fail("INVALID_TRANSITION", "下一步不在当前 SOP 的允许转移中。")
        if arguments["status"] == "completed":
            from app.session.slot_policy import missing_step_slots

            missing_slots = missing_step_slots(self.requirement, arguments.get("slot_updates"))
            if missing_slots:
                return ModuleResult.fail("REQUIRED_SLOT_MISSING", "当前步骤必填字段未齐：" + "、".join(missing_slots),
                                         extensions={"details": {"missing_slots": missing_slots}})
            from staffdeck_harness.runtime.completion import missing_capabilities

            missing = missing_capabilities(
                self.requirement, capability_results, citations, evidence
            )
            if missing:
                return ModuleResult.fail(
                    "REQUIRED_CAPABILITY_NOT_INVOKED", "必需能力尚未成功执行：" + "、".join(missing)
                )
        return ModuleResult.ok({**arguments, "next_step_id": next_step})
