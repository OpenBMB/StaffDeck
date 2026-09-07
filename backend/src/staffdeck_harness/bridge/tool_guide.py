"""Initial, engine-specific instructions derived from the actual registered system tools."""

import json


def system_tool_guide(kind: str, names: set[str] | None = None) -> str:
    from staffdeck_harness.capabilities.host import PROXY_TOOLS

    enabled = set(PROXY_TOOLS) if names is None else set(names)
    lines = [
        "# 平台系统工具：先了解使用方式，再执行任务",
        "以下是本次执行实际提供的系统接口。SOP 正文中的名称不是可直接调用的函数名；不得猜测名称、资源 ID 或参数。",
        "1. 若上下文已给出当前资源的完整 schema 和 ID，可直接按定义调用；否则先查看定义，不要先试调用再等待报错。",
        '2. 本运行时用 mcp__staffdeck__capability_describe({"kind":"all"}) 发现当前资源并取得定义；也可按 tool、general_skill、knowledge_base 筛选。它只读取当前绑定，不会扩大授权。',
        "3. describe 的 items 返回 kind、id、name、operation、input_schema。展示名称不能代替 id，SOP 指令不能代替参数 schema。",
        "4. 业务工具使用 tool_invoke，扩展模块使用 capability_invoke；必须按对应入口封装参数。执行后根据真实结果继续，不得反复查询已在 known_slots、用户输入或先前结果中明确的信息。",
        "本运行时没有独立的 capability_search；不要套用 v2 的 capabilities 参数或 action/tool_name JSON 协议来调用这些函数。",
        "## 可调用接口",
    ]
    for name, spec in PROXY_TOOLS.items():
        if name in enabled:
            lines.append(f"- mcp__staffdeck__{name}：{spec['description']} 参数 schema：{json.dumps(spec['parameters'], ensure_ascii=False, separators=(',', ':'))}")
    if "tool_invoke" in enabled:
        lines.append('业务调用格式示例：mcp__staffdeck__tool_invoke({"tool_id":"<describe 返回的 id>","arguments":{...}})。示例占位符不可原样提交。')
    lines.extend([
        '扩展模块格式：mcp__staffdeck__capability_invoke({"operation":"<返回的版本化 operation>","resource_id":"<返回的 id>","arguments":{...}})。',
        "general_skill_read 只加载技能说明，并不执行脚本；需要实际操作时，再使用已提供的业务或 sandbox_execute 接口。",
        "## 报错后的处理",
        "未找到代理/操作：检查本节函数清单和 describe 返回的 operation，不要猜测或原样重试。",
        "资源未绑定：查看 describe 的当前绑定；仍无所需资源时暂停并说明配置问题，describe 不会授予新权限。",
        "权限拒绝或撤销：停止该操作，不得改用其他工具绕过权限。",
        "参数无效：对照 schema 和 known_slots 修正字段、类型与嵌套层级；缺少事实就询问用户，不填占位符。",
        "执行上下文失效：停止使用旧上下文，等待新的执行；写入结果不确定（OUTCOME_UNKNOWN）时先核对回执，不重新提交。",
        "只有错误明确可重试，且状态或参数已有有效变化时才继续；连续同类受阻时保留上下文并暂停。",
    ])
    if kind == "sop" and (names is None or "submit_step_result" in enabled):
        lines.append("SOP 运行控制：用 mcp__staffdeck__submit_step_result 提交当前步骤结果；它不是业务能力。必填槽位齐全且强制能力真实成功后才能提交 completed。")
    else:
        lines.append("普通对话直接返回用户可见正文，不调用任何结束工具或 SOP 步骤提交接口。")
    return "\n".join(lines)
