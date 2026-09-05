# DSH 执行适配：保留 v2 的循环周期与状态语义

本修订补充 `design-harness-v3-modularity-repair.md`。模块仍可解耦，但共享的
TurnCoordinator、TaskFrameStore、SOP 状态推进与结果规范化继续使用 v2 的处理逻辑。

后续实现归属修订见 `design-harness-v3-sop-runtime.md`：SOP 状态实现已迁入独立模块，
v2 与 DSH 共用该模块。这里的“使用 v2 的处理逻辑”指行为兼容，不再指调用旧 AgentLoop 方法。

## 周期与上下文归属

- 通用循环：沿用 `general:<chat session id>` 的逻辑循环，一次用户消息只是一次推进。
- SOP：每个 TaskFrame 对应的 SOP 执行实例拥有独立循环；不是按 SOP 定义 ID 共用上下文。
- 等待用户挂起，继续时恢复；节点完成不销毁整个 SOP；预算耗尽保留 checkpoint 并交还调度器。
- 用户轮次和权限快照不是上下文的身份。新的权限仍在每次能力调用时复核，旧历史不产生新授权。

检查点保存公开输入/输出、调用回执、引用、证据与产物，使用与 v2 相同的有界历史投影规则。
它不是无限历史存储，也不保存模型私有推理。
相同节点恢复其已完成调用；进入新节点只保留历史，不继承上一节点的“必需能力已完成”状态。

## DSH 进程与会话

当前 DSH SDK 只暴露 `session/prompt`，没有公开的历史导入/恢复请求：

1. 同一温进程、同一逻辑循环且 checkpoint revision 一致时，复用原 DSH 会话。
2. 换进程、重启或原生历史领先于已提交 checkpoint 时，建立新的传输会话，并注入同一逻辑循环的公开 checkpoint。
3. 原生 DSH 会话 ID 可以在冷恢复时变化；业务循环 ID 和上下文归属不变。
4. 旧 DSH 标记式 checkpoint 缺少 transcript 时，按该循环自己的持久化 Run/Invocation 记录恢复可用公开历史；不混入其他 SOP 的记录，也不声称恢复从未持久化的内容。

## 结果提交不是业务能力

`CapabilityHost` 不再实现 `finish_task`。固定的 `StepCompletionPort` 属于 Bridge 的运行控制层：

- 普通对话：模型工具列表中不提供步骤提交接口，直接接收 DSH 最终输出并执行后处理。
- 需要表达等待、人工介入、失败或槽位更新时，普通对话可在原生最终输出返回 v2 的 `action=finish` 结构化报文，不需要额外 MCP 调用。
- SOP：使用固定 `submit_step_result` 控制协议提交结果，验证必需能力及允许的下一节点；该接口不是员工可装卸的能力插件。
- 两条路径最终都调用 v2 的 `finish_execution_result`，共享状态、槽位过滤、下一步和专用人工节点的规范化规则。
- `finish_task` 仅作为 SOP 控制入口的旧协议别名兼容，不再向模型发布。普通对话不能用它绕过处理流程。
- 界面显示“提交步骤结果”，包括历史记录，不再把结束控制展示为“调用能力”。

## 可执行验收

- `test_execution_context_lifecycle.py`：作用域隔离、跨轮/冷恢复、不同节点回执隔离、JSON checkpoint。
- `test_runtime_completion_control.py`：普通对话无控制工具、结果状态与 v2 一致、无效转移拒绝、旧 checkpoint 按实例恢复。
- `test_sealed_e2e_engine.py`：真实 DSH + 本机假模型，覆盖普通对话无结束工具、通用循环跨 TaskFrame、SOP 温/冷恢复、不同 SOP 隔离、完整调度器挂起/推进/完成、预算退出保留回执。

这些测试使用临时 SQLite 和虚构数据，不向真实渠道发送消息，也不执行业务写操作。

## 本次验证记录（2026-09-05）

- 后端全量回归（不含真实引擎组）：2610 passed、11 failed、3 skipped、2 xfailed。
  11 个失败均为改动前已确认的渠道基线问题：9 个 SQLite `team_id` 迁移用例，
  2 个飞书目标字段预期用例；本次未扩大范围修改渠道代码。
- 真实 DSH 端到端组：6 passed；前端：249 passed；生产构建、i18n、配置检查通过。
- 5173 使用已授权管理员与“测试”员工进行两轮纯对话：第二轮不重复提供测试标记，
  仍正确返回上一轮标记。两个新 TaskFrame 归属同一个通用循环，checkpoint 包含公开历史，
  两轮业务能力调用数为 0。
- 普通对话未产生结束工具调用；历史 `finish_task` 展示文本已兼容为“提交步骤结果”，
  原始审计记录不做改写。SOP 冷恢复/节点推进使用上述隔离端到端测试验证，未运行真实业务 SOP。
